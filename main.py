"""
main.py - ATR BANDS TREND bot orkestratoru.

3 bagimsiz thread:
  1. entry_loop  - her SCAN_SECONDS'da tum coinleri tarar:
                   fiyat geçmişine ekler, bant hesaplar, flag yonetir,
                   giris sinyali tespit edip islem acar.

  2. exit_loop   - her SCAN_SECONDS'da acik pozisyonlari tarar:
                   bant gunceller, seviye gecislerini kontrol eder,
                   cikis tetigi varsa kapatir. Ayrica Bybit'ten acik
                   pozisyon listesini cekip stoploss tespiti yapar.

  3. report_loop - her dakika baslarinda saat kontrol eder, gerekli
                   raporlari uretir ve gonderir.
                   15dk: dakika in (0,15,30,45)
                   Saatlik: dakika == 0
                   8 saatlik: saat config'de listelenen, dakika == 0
                   Gunluk: saat config'deki gunluk saat, dakika == 0
"""

import logging
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import Dict, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%d.%m.%Y %H:%M:%S",
)
log = logging.getLogger("bot")

from bands import compute_bands, sort_klines_chronological
from bybit import BybitClient
from config import Config
from notifier import Notifier
from order import (
    submit_entry, submit_exit, calc_qty, fmt_price, fmt_qty, round_step,
)
from position import (
    Position, LEVEL_LABELS, LEVEL_ENTRY, LEVEL_BE,
    update_level_and_ce, check_exit,
)
from price_history import PriceHistory
from reports import (
    build_report_15min, build_report_hourly,
    build_report_8h, build_report_daily,
)
from state import StateManager
from strategy import (
    detect_flag_action, detect_entry,
    FLAG_OPEN_LONG, FLAG_OPEN_SHORT, FLAG_CLEAR_LONG, FLAG_CLEAR_SHORT,
)


# ---------------------------------------------------------------------------
# Global stop flag
# ---------------------------------------------------------------------------

_stop_event = threading.Event()


def _handle_signal(signum, frame):
    _stop_event.set()


signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.notifier = Notifier(cfg.telegram_token, cfg.telegram_chat_id)
        self.bybit    = BybitClient(cfg.bybit_api_key, cfg.bybit_api_secret)
        self.state    = StateManager()
        self.history  = PriceHistory(cfg.history_seconds)

        # Bot baslarken alinan bakiye -> sabit stake
        self.initial_balance: float = 0.0
        self.stake_per_trade: float = 0.0

        # Son tarama icin bant cache'i (exit thread'in tekrar fetch etmesini onler)
        self._bands_cache: Dict[str, dict] = {}    # symbol -> {bands, ts}
        self._bands_cache_lock = threading.Lock()
        self._bands_ttl_sec: int = 30   # 30 sn'den eskiyse yenile

        # Rapor tetik kontrolu (ayni dakikada iki kere gondermemek icin)
        self._last_report_minute: Optional[str] = None

    # ----------------------------------------------------------------------
    # Baslangic
    # ----------------------------------------------------------------------

    def startup(self) -> None:
        # 1. Bakiye oku ve stake belirle
        try:
            balance = self.bybit.fetch_balance_usdt()
        except Exception as e:
            self.notifier.api_connection_error(str(e))
            raise

        if balance <= 0:
            self.notifier.api_key_invalid()
            raise RuntimeError("Bakiye 0 veya API key gecersiz.")

        self.initial_balance = balance
        self.stake_per_trade = balance * (self.cfg.stake_percent / 100.0)

        # 2. Acik pozisyonlari (manuel acilmis olanlar) external olarak isaretle
        try:
            ext = self.bybit.fetch_positions()
            for p in ext:
                sym = p.get("symbol")
                if sym:
                    self.state.mark_external(sym)
        except Exception:
            pass  # baslarken kritik degil

        # 3. Telegram bildirimi
        self.notifier.bot_started(balance, self.stake_per_trade, self.cfg)
        log.info(f"BOT BASLATILDI | bakiye={balance:.2f} USDT | stake={self.stake_per_trade:.2f} USDT | {len(self.cfg.coins)} coin tarama")

    def shutdown(self, reason: str = "Manuel kapatma") -> None:
        log.info(f"BOT DURDURULUYOR | {reason}")
        try:
            self.notifier.bot_stopped(reason)
        except Exception:
            pass

    # ----------------------------------------------------------------------
    # Bant cache
    # ----------------------------------------------------------------------

    def _get_bands(self, symbol: str):
        """Son tarama bant degerlerini doner. Cache'ten yararlanir."""
        now = time.time()
        with self._bands_cache_lock:
            entry = self._bands_cache.get(symbol)
            if entry and now - entry["ts"] < self._bands_ttl_sec:
                return entry["bands"]

        # Yeniden hesapla
        interval = self.cfg.bybit_interval()
        limit    = max(self.cfg.ema_period, self.cfg.atr_period) + 50
        raw = self.bybit.fetch_kline(symbol, interval, limit=limit)
        klines = sort_klines_chronological(raw)
        bands  = compute_bands(
            klines,
            ema_period=self.cfg.ema_period,
            atr_period=self.cfg.atr_period,
            band_multiplier=self.cfg.band_multiplier,
            buffer_multiplier=self.cfg.buffer_multiplier,
        )
        with self._bands_cache_lock:
            self._bands_cache[symbol] = {"bands": bands, "ts": now}
        return bands

    # ======================================================================
    # GIRIS THREAD
    # ======================================================================

    def entry_loop(self) -> None:
        while not _stop_event.is_set():
            t0 = time.time()
            for symbol in self.cfg.coins:
                if _stop_event.is_set():
                    break
                try:
                    self._entry_scan_symbol(symbol)
                except Exception as e:
                    # Tek coin hatasi tum thread'i durdurmasin
                    self._safe_telegram_error(f"entry_scan {symbol}: {e}")
            elapsed = time.time() - t0
            sleep_t = max(0.0, self.cfg.scan_seconds - elapsed)
            _stop_event.wait(sleep_t)

    def _entry_scan_symbol(self, symbol: str) -> None:
        # 1. Anlik fiyat
        price = self.bybit.fetch_last_price(symbol)
        prev_price = self.history.last(symbol)
        self.history.add(symbol, price)

        # 2. Bant hesapla
        bands = self._get_bands(symbol)

        # 3. Flag aksiyon kontrolu
        current_flag = self.state.get_flag(symbol)
        action = detect_flag_action(prev_price, price, bands, current_flag)

        if action == FLAG_OPEN_LONG:
            self.state.set_flag(symbol, "LONG")
            self.notifier.flag_opened(symbol, "LONG", price)
            log.info(f"FLAG ACILDI | {symbol} LONG | fiyat={price} ic_tampon={bands.ust_ic_tampon:.6f}")
        elif action == FLAG_OPEN_SHORT:
            self.state.set_flag(symbol, "SHORT")
            self.notifier.flag_opened(symbol, "SHORT", price)
            log.info(f"FLAG ACILDI | {symbol} SHORT | fiyat={price} ic_tampon={bands.alt_ic_tampon:.6f}")
        elif action == FLAG_CLEAR_LONG:
            self.state.clear_flag(symbol)
            self.notifier.flag_deleted(symbol, "LONG", price)
            log.info(f"FLAG SILINDI | {symbol} LONG | fiyat={price} ic_tampon={bands.ust_ic_tampon:.6f}")
        elif action == FLAG_CLEAR_SHORT:
            self.state.clear_flag(symbol)
            self.notifier.flag_deleted(symbol, "SHORT", price)
            log.info(f"FLAG SILINDI | {symbol} SHORT | fiyat={price} ic_tampon={bands.alt_ic_tampon:.6f}")

        # 4. Giris sinyali tespiti
        current_flag = self.state.get_flag(symbol)   # action sonrasi yenile
        signal_ = detect_entry(price, bands, current_flag)
        if signal_ is None:
            return

        disbant_val = bands.ust_disbant if signal_.side == "LONG" else bands.alt_disbant
        log.info(f"GIRIS SINYALI | {symbol} {signal_.side} | fiyat={price} disbant={disbant_val:.6f}")

        # 5. Slot kontrolu
        if self.state.total_slots_used() >= self.cfg.max_open_trades:
            self.notifier.slot_full(
                symbol, signal_.side,
                self.state.total_slots_used(),
                self.cfg.max_open_trades,
            )
            log.info(f"SLOT DOLU | {symbol} {signal_.side} | {self.state.total_slots_used()}/{self.cfg.max_open_trades}")
            return

        # 6. Coinde zaten acik islem var mi
        if self.state.has_open_trade(symbol):
            log.info(f"ACIK ISLEM VAR | {symbol} atlandi")
            return

        # 7. Islemi ac
        log.info(f"ISLEM ACILIYOR | {symbol} {signal_.side} | fiyat={price}")
        self._open_trade(symbol, signal_.side, signal_.bands, price)

    # ----------------------------------------------------------------------
    # Islem acma
    # ----------------------------------------------------------------------

    def _open_trade(self, symbol: str, side: str, bands, price: float) -> None:
        # 1. Instrument bilgisi
        try:
            info = self.bybit.get_instrument_info(symbol)
        except Exception as e:
            self._safe_telegram_error(f"get_instrument_info {symbol}: {e}")
            return

        # 2. Kaldirac uygunlugu
        if info["max_leverage"] < self.cfg.leverage:
            return  # bu coin kaldiraci desteklemiyor

        # 3. Isolated mod + kaldirac
        try:
            self.bybit.switch_isolated(symbol, self.cfg.leverage)
            self.bybit.set_leverage(symbol, self.cfg.leverage)
        except Exception as e:
            self._safe_telegram_error(f"set_leverage {symbol}: {e}")
            return

        # 4. Notional ve miktar
        notional = self.stake_per_trade * self.cfg.leverage
        qty = calc_qty(notional, price, info["qty_step"], info["min_order_qty"])
        if qty <= 0:
            return  # min qty altinda

        # 5. Giris emrini gonder
        result = submit_entry(
            bybit_client=self.bybit,
            symbol=symbol,
            side=side,
            qty=qty,
            instrument=info,
            max_attempts=self.cfg.entry_attempts,
            wait_seconds=self.cfg.attempt_wait_sec,
        )

        if not result.filled:
            self.notifier.entry_order_failed(symbol, side, result.attempts)
            log.warning(f"GIRIS EMRI DOLMADI | {symbol} {side} | {result.attempts} deneme")
            return

        entry_price = result.avg_price
        filled_qty  = result.qty

        # 6. Borsa-tarafli SL emri yerlestir (emniyet kemeri)
        if side == "LONG":
            sl_price_val = entry_price * (1.0 - self.cfg.sl_percent / 100.0)
        else:
            sl_price_val = entry_price * (1.0 + self.cfg.sl_percent / 100.0)
        sl_price_str = fmt_price(sl_price_val, info["tick_size"])
        try:
            self.bybit.set_position_stop_loss(symbol, sl_price_str)
        except Exception as e:
            self._safe_telegram_error(f"set_stop_loss {symbol}: {e}")

        # 7. Pozisyonu state'e kaydet
        pos = Position(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            qty=filled_qty,
            stake=self.stake_per_trade,
            notional=entry_price * filled_qty,
            leverage=self.cfg.leverage,
            atr_at_entry=bands.atr,
            stop_loss_price=float(sl_price_str),
        )
        self.state.add_position(pos)

        # 8. Flag silinmis olmali (giris dis bandi gectiyse flag silinir)
        self.state.clear_flag(symbol)

        # 9. Telegram
        self.notifier.trade_opened(
            symbol, side, entry_price,
            self.stake_per_trade,
            entry_price * filled_qty,
            float(sl_price_str),
        )
        log.info(f"ISLEM ACILDI | {symbol} {side} | giris={entry_price} qty={filled_qty} sl={sl_price_str}")

    # ======================================================================
    # CIKIS THREAD
    # ======================================================================

    def exit_loop(self) -> None:
        while not _stop_event.is_set():
            t0 = time.time()
            try:
                self._exit_scan()
            except Exception as e:
                self._safe_telegram_error(f"exit_scan: {e}")
            elapsed = time.time() - t0
            sleep_t = max(0.0, self.cfg.scan_seconds - elapsed)
            _stop_event.wait(sleep_t)

    def _exit_scan(self) -> None:
        # 1. Bybit'ten acik pozisyonlari cek (SL tespiti icin)
        try:
            bybit_pos = self.bybit.fetch_positions()
            bybit_open_symbols = {p.get("symbol") for p in bybit_pos}
        except Exception as e:
            self._safe_telegram_error(f"fetch_positions: {e}")
            bybit_open_symbols = None

        # 2. Bot pozisyonlarini tara
        for pos in self.state.all_open():
            if _stop_event.is_set():
                break

            # SL tespiti: bot kaydinda var, Bybit'te yok -> SL tetiklenmis
            if bybit_open_symbols is not None and pos.symbol not in bybit_open_symbols:
                self._handle_stoploss_detected(pos)
                continue

            try:
                self._exit_check_position(pos)
            except Exception as e:
                self._safe_telegram_error(f"exit_check {pos.symbol}: {e}")

        # 3. External (manuel) sembolleri de senkronize et
        if bybit_open_symbols is not None:
            for sym in self.state.external_symbols():
                if sym not in bybit_open_symbols:
                    self.state.unmark_external(sym)

    def _exit_check_position(self, pos: Position) -> None:
        # 1. Anlik fiyat
        price = self.bybit.fetch_last_price(pos.symbol)

        # 2. Bant degerlerini al (dinamik BE icin disbant gerekli)
        bands = self._get_bands(pos.symbol)

        # 3. Seviye / CE / BE guncelle
        new_level = update_level_and_ce(
            pos,
            price=price,
            ust_dis_tampon=bands.ust_dis_tampon,
            alt_dis_tampon=bands.alt_dis_tampon,
            ust_disbant=bands.ust_disbant,
            alt_disbant=bands.alt_disbant,
            ce1_atr=self.cfg.ce1_atr, ce1_trail=self.cfg.ce1_trail,
            ce2_atr=self.cfg.ce2_atr, ce2_trail=self.cfg.ce2_trail,
            winrate_atr=self.cfg.winrate_atr, winrate_trail=self.cfg.winrate_trail,
        )

        if new_level is not None:
            self.notifier.level_changed(
                pos.symbol, pos.side, LEVEL_LABELS[new_level],
                price,
                pos.profit_usdt(price),
                pos.profit_pct_leveraged(price),
            )
            log.info(f"SEVIYE GECISI | {pos.symbol} {pos.side} | {LEVEL_LABELS[new_level]} | fiyat={price}")

        # 4. Cikis kontrolu
        exit_type = check_exit(
            pos, price,
            ust_ic_tampon=bands.ust_ic_tampon,
            alt_ic_tampon=bands.alt_ic_tampon,
        )

        if exit_type is None:
            return

        log.info(f"CIKIS TETIGI | {pos.symbol} {pos.side} | {exit_type} | fiyat={price}")

        # 5. Pozisyonu kapat
        self._close_position(pos, exit_type)

    def _close_position(self, pos: Position, exit_type: str) -> None:
        # 1. Cikis emrini gonder (limit -> market fallback)
        try:
            info = self.bybit.get_instrument_info(pos.symbol)
        except Exception as e:
            self._safe_telegram_error(f"get_instrument_info {pos.symbol}: {e}")
            return

        try:
            result = submit_exit(
                bybit_client=self.bybit,
                symbol=pos.symbol,
                side=pos.side,
                qty=pos.qty,
                instrument=info,
                max_attempts=self.cfg.exit_attempts,
                wait_seconds=self.cfg.attempt_wait_sec,
            )
        except Exception as e:
            self._safe_telegram_error(f"submit_exit {pos.symbol}: {e}")
            return

        if not result.filled:
            # Bu olmamali (market fallback var) ama defansif kontrol
            return

        exit_price = result.avg_price
        pnl_usdt = pos.profit_usdt(exit_price)
        pnl_pct  = pos.profit_pct_leveraged(exit_price)
        atr_profit = pos.profit_in_atr(exit_price)

        # 2. State'ten sil
        self.state.remove_position(pos.symbol)

        # 3. Kapanmis kaydi log'a yaz
        self.state.log_closed({
            "symbol":    pos.symbol,
            "side":      pos.side,
            "entry":     pos.entry_price,
            "exit":      exit_price,
            "pnl_usdt":  pnl_usdt,
            "pnl_pct":   pnl_pct,
            "exit_type": exit_type,
            "atr_profit": atr_profit,
            "close_time": time.time(),
        })

        # 4. Telegram bildirim
        self.notifier.trade_closed(
            pos.symbol, pos.side, pos.entry_price, exit_price,
            exit_type, pnl_usdt, pnl_pct, atr_profit,
            market_fallback=result.market_fallback,
        )
        market_tag = " (market fallback)" if result.market_fallback else ""
        log.info(f"ISLEM KAPANDI | {pos.symbol} {pos.side} | {exit_type}{market_tag} | giris={pos.entry_price} cikis={exit_price} pnl={pnl_usdt:.2f} USDT")

    def _handle_stoploss_detected(self, pos: Position) -> None:
        """Bot kaydinda var ama Bybit'te yok -> borsa SL'yi tetiklemis."""
        # Cikis fiyati olarak SL seviyesini varsayalim
        exit_price = pos.stop_loss_price
        pnl_usdt = pos.profit_usdt(exit_price)
        pnl_pct  = pos.profit_pct_leveraged(exit_price)
        atr_profit = pos.profit_in_atr(exit_price)

        self.state.remove_position(pos.symbol)
        self.state.log_closed({
            "symbol":    pos.symbol,
            "side":      pos.side,
            "entry":     pos.entry_price,
            "exit":      exit_price,
            "pnl_usdt":  pnl_usdt,
            "pnl_pct":   pnl_pct,
            "exit_type": "Stoploss Exit",
            "atr_profit": atr_profit,
            "close_time": time.time(),
        })
        self.notifier.stoploss_detected(
            pos.symbol, pos.side, pos.entry_price, exit_price,
            pnl_usdt, pnl_pct,
        )
        log.warning(f"STOPLOSS TESPIT | {pos.symbol} {pos.side} | giris={pos.entry_price} sl={exit_price} pnl={pnl_usdt:.2f} USDT")

    # ======================================================================
    # RAPOR THREAD
    # ======================================================================

    def report_loop(self) -> None:
        # Sadece dakika basinda kontrol et
        while not _stop_event.is_set():
            now = datetime.now()
            key = now.strftime("%Y-%m-%d %H:%M")
            if key != self._last_report_minute:
                self._last_report_minute = key
                try:
                    self._maybe_send_reports(now)
                except Exception as e:
                    self._safe_telegram_error(f"report: {e}")

            # Sonraki dakikaya kadar bekle
            secs_to_next = 60 - now.second
            _stop_event.wait(max(1, secs_to_next))

    def _maybe_send_reports(self, now: datetime) -> None:
        minute = now.minute
        hour   = now.hour

        # 15 dakikalik (her ceyrek saat basi)
        if self.cfg.report_15min and minute % 15 == 0:
            self._send_15min()

        # Saatlik (her saat basi)
        if self.cfg.report_hourly and minute == 0:
            self._send_hourly()

        # 8 saatlik (config'deki saatler, dakika 0)
        if minute == 0 and hour in self.cfg.report_8h_hours:
            self._send_8h()

        # Gunluk (config'deki saat, dakika 0)
        if minute == 0 and hour == self.cfg.report_daily_hour:
            self._send_daily()

    def _current_balance(self) -> float:
        try:
            return self.bybit.fetch_balance_usdt()
        except Exception:
            return self.initial_balance

    def _current_prices(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for pos in self.state.all_open():
            try:
                out[pos.symbol] = self.bybit.fetch_last_price(pos.symbol)
            except Exception:
                out[pos.symbol] = pos.entry_price
        return out

    def _send_15min(self) -> None:
        text = build_report_15min(
            balance=self._current_balance(),
            positions=self.state.all_open(),
            prices=self._current_prices(),
            max_slots=self.cfg.max_open_trades,
        )
        self.notifier.report(text)
        log.info("RAPOR | 15 dakikalik gonderildi")

    def _send_hourly(self) -> None:
        cutoff = time.time() - 3600
        text = build_report_hourly(
            balance=self._current_balance(),
            positions=self.state.all_open(),
            prices=self._current_prices(),
            closed=self.state.closed_since(cutoff),
            max_slots=self.cfg.max_open_trades,
        )
        self.notifier.report(text)
        log.info("RAPOR | saatlik gonderildi")

    def _send_8h(self) -> None:
        cutoff = time.time() - 8 * 3600
        text = build_report_8h(
            balance=self._current_balance(),
            positions=self.state.all_open(),
            prices=self._current_prices(),
            closed=self.state.closed_since(cutoff),
            max_slots=self.cfg.max_open_trades,
        )
        self.notifier.report(text)
        log.info("RAPOR | 8 saatlik gonderildi")

    def _send_daily(self) -> None:
        cutoff = time.time() - 24 * 3600
        text = build_report_daily(
            balance=self._current_balance(),
            positions=self.state.all_open(),
            prices=self._current_prices(),
            closed=self.state.closed_since(cutoff),
            max_slots=self.cfg.max_open_trades,
        )
        self.notifier.report(text)
        log.info("RAPOR | gunluk gonderildi")

    # ----------------------------------------------------------------------
    # Yardimcilar
    # ----------------------------------------------------------------------

    def _safe_telegram_error(self, msg: str) -> None:
        """Telegram'a hata bildirimi gonder, ama gonderim de hata verirse yutma."""
        try:
            self.notifier.api_connection_error(msg)
        except Exception:
            pass

    # ----------------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------------

    def run(self) -> None:
        self.startup()

        threads = [
            threading.Thread(target=self.entry_loop,  name="entry_loop",  daemon=True),
            threading.Thread(target=self.exit_loop,   name="exit_loop",   daemon=True),
            threading.Thread(target=self.report_loop, name="report_loop", daemon=True),
        ]
        for t in threads:
            t.start()

        # Ana thread sinyal bekler
        try:
            while not _stop_event.is_set():
                _stop_event.wait(1.0)
        except KeyboardInterrupt:
            _stop_event.set()

        # Kapanis
        self.shutdown("Sinyal alindi")

        # Thread'lerin bitmesini bekle
        for t in threads:
            t.join(timeout=10)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        cfg = Config("config.json")
    except Exception as e:
        print(f"Config yuklenemedi: {e}", file=sys.stderr)
        return 1

    bot = Bot(cfg)
    try:
        bot.run()
    except Exception as e:
        traceback.print_exc()
        try:
            bot.notifier.api_connection_error(f"Bot kritik hata: {e}")
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
