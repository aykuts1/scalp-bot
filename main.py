"""
Main bot loop.

Schedules:
  - Entry scan: every 5-min candle close (a few seconds after to ensure fresh data)
  - Exit scan: every 60 seconds (manages CE, stage transitions, external SL detection)
  - Daily summary: at UTC midnight

Stage transitions (use extreme/peak price, not current):
  Stage 0 → 1: peak profit >= +1.2%  →  move SL to +1% profit
  Stage 1 → 2: peak profit >= +2 ATR →  move SL to +0.2 ATR profit, CE 2 ATR starts
  Stage 2 → 3: peak profit >= +6 ATR →  CE narrows to 1 ATR, SL unchanged

CE active only in Stage 2 and Stage 3.
"""
import time
import traceback
from datetime import datetime, timezone
from typing import List

import config
import strategy
import indicators
import telegram_bot as tg
from bybit_client import BybitClient
from position_manager import (
    PositionManager,
    Position,
    STAGE_ENTRY,
    STAGE_1_ATR,
    STAGE_2_ATR,
)


# ============================================================
# GLOBALS
# ============================================================
STAKE_USDT = 0.0
DAILY_STATS = {"date": None, "pnl": 0.0, "trades": 0, "wins": 0}
EXTERNAL_POSITIONS = set()  # Symbols with positions not managed by the bot (manual / pre-existing)


# ============================================================
# HELPERS
# ============================================================
def now_ts() -> float:
    return time.time()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def compute_initial_sl(side: str, entry_price: float, sl_pct: float) -> float:
    """Loss-side SL at entry (%1 below entry for long, above for short)."""
    if side == "Buy":
        return entry_price * (1 - sl_pct)
    else:
        return entry_price * (1 + sl_pct)


def compute_pct_profit_sl(side: str, entry_price: float, profit_pct: float) -> float:
    """SL at +profit_pct% profit (Stage 1)."""
    if side == "Buy":
        return entry_price * (1 + profit_pct)
    else:
        return entry_price * (1 - profit_pct)


def compute_atr_profit_sl(side: str, entry_price: float, sl_atr: float, atr: float) -> float:
    """SL at +sl_atr ATR profit (Stage 2)."""
    if side == "Buy":
        return entry_price + sl_atr * atr
    else:
        return entry_price - sl_atr * atr


def get_closed_pnl(client: BybitClient, symbol: str) -> tuple:
    """Fetch most recent closed PnL. Returns (exit_price, pnl_usdt) or (None, 0)."""
    try:
        resp = client.session.get_closed_pnl(
            category=config.CATEGORY,
            symbol=symbol,
            limit=1,
        )
        if resp.get("retCode") == 0:
            items = resp["result"]["list"]
            if items:
                last = items[0]
                exit_price = float(last.get("avgExitPrice", 0) or 0)
                pnl = float(last.get("closedPnl", 0) or 0)
                return exit_price, pnl
    except Exception as e:
        print(f"[WARN] get_closed_pnl {symbol}: {e}")
    return None, 0.0


def record_trade(pnl: float) -> None:
    today = utc_now().date()
    if DAILY_STATS["date"] != today:
        DAILY_STATS["date"] = today
        DAILY_STATS["pnl"] = 0.0
        DAILY_STATS["trades"] = 0
        DAILY_STATS["wins"] = 0
    DAILY_STATS["pnl"] += pnl
    DAILY_STATS["trades"] += 1
    if pnl > 0:
        DAILY_STATS["wins"] += 1


# ============================================================
# POSITION OPENING
# ============================================================
def open_position(client: BybitClient, pm: PositionManager, signal: strategy.Signal) -> None:
    """Place market order with attached %1 SL. Route specific errors to info notifications."""
    symbol = signal.symbol
    side = signal.side
    entry_ref = signal.entry_price

    try:
        # Set isolated + leverage (may raise 110013 if leverage too high for this coin)
        client.set_isolated_margin(symbol, config.LEVERAGE)
        client.set_leverage(symbol, config.LEVERAGE)

        info = client.get_instrument_info(symbol)

        # Notional value = stake * leverage; qty = notional / price
        notional = STAKE_USDT * config.LEVERAGE
        raw_qty = notional / entry_ref
        qty = client.round_step(raw_qty, info["qty_step"])
        if qty < info["min_qty"]:
            print(f"[SKIP] {symbol} qty {qty} below min {info['min_qty']}")
            return

        sl_price = compute_initial_sl(side, entry_ref, config.INITIAL_SL_PERCENT)

        # Place market order with attached SL (may raise 110007 if insufficient balance)
        client.place_market_order(
            symbol=symbol,
            side=side,
            qty=qty,
            stop_loss_price=sl_price,
        )

        # Brief pause to let order fill, then read actual position
        time.sleep(1.5)
        pos = client.get_position(symbol)
        if pos is None:
            tg.send_error("İşlem açıldı ama pozisyon bulunamadı", f"{symbol} {side}")
            return

        actual_entry = float(pos.get("avgPrice", entry_ref) or entry_ref)
        actual_qty = float(pos.get("size", qty) or qty)

        # Recompute SL based on actual entry and update on exchange
        actual_sl = compute_initial_sl(side, actual_entry, config.INITIAL_SL_PERCENT)
        try:
            client.update_stop_loss(symbol, actual_sl)
        except Exception:
            # Initial SL from order placement is already in place
            pass

        position = Position(
            symbol=symbol,
            side=side,
            entry_price=actual_entry,
            qty=actual_qty,
            stake_usdt=STAKE_USDT,
            leverage=config.LEVERAGE,
            atr_at_entry=signal.atr,
            open_time=now_ts(),
            stage=STAGE_ENTRY,
            ce_level=None,
            current_sl=actual_sl,
            extreme_price=actual_entry,
        )
        pm.open(position)

        tg.send_entry(
            symbol=symbol,
            side=side,
            price=actual_entry,
            qty=actual_qty,
            stake=STAKE_USDT,
            leverage=config.LEVERAGE,
            sl_price=actual_sl,
            atr_value=signal.atr,
        )
        print(f"[OPEN] {symbol} {side} @ {actual_entry} qty={actual_qty} sl={actual_sl}")

    except Exception as e:
        msg = str(e)
        # Insufficient balance → info notification, not error
        if "110007" in msg:
            tg.send_insufficient_balance(symbol, side)
            print(f"[SKIP] {symbol} {side}: insufficient balance")
            return
        # Leverage limit exceeded → info notification, not error
        if "110013" in msg:
            tg.send_leverage_rejected(symbol, side, config.LEVERAGE)
            print(f"[SKIP] {symbol} {side}: leverage limit")
            return
        # Other failures → real error
        tb = traceback.format_exc()
        print(f"[ERR] open_position {symbol}: {e}\n{tb}")
        tg.send_error(f"İşlem açılamadı: {symbol} {side}", str(e))


# ============================================================
# POSITION CLOSING
# ============================================================
def close_position(client: BybitClient, pm: PositionManager, symbol: str, reason: str) -> None:
    """Close a tracked position with market reduceOnly order."""
    pos = pm.get(symbol)
    if pos is None:
        return

    try:
        ex_pos = client.get_position(symbol)
        if ex_pos is None:
            # Already closed externally
            exit_price, pnl = get_closed_pnl(client, symbol)
            if exit_price is None:
                exit_price = pos.entry_price
            pnl_pct = (pnl / pos.stake_usdt * 100) if pos.stake_usdt else 0
            tg.send_exit(
                symbol=symbol, side=pos.side,
                entry_price=pos.entry_price, exit_price=exit_price,
                pnl_usdt=pnl, pnl_pct=pnl_pct, reason=reason,
            )
            record_trade(pnl)
            pm.close(symbol)
            return

        actual_qty = float(ex_pos.get("size", pos.qty))
        client.close_position(symbol, pos.side, actual_qty)
        time.sleep(1.2)

        exit_price, pnl = get_closed_pnl(client, symbol)
        if exit_price is None:
            exit_price = client.get_last_price(symbol)
        pnl_pct = (pnl / pos.stake_usdt * 100) if pos.stake_usdt else 0

        tg.send_exit(
            symbol=symbol, side=pos.side,
            entry_price=pos.entry_price, exit_price=exit_price,
            pnl_usdt=pnl, pnl_pct=pnl_pct, reason=reason,
        )
        record_trade(pnl)
        pm.close(symbol)
        print(f"[CLOSE] {symbol} reason={reason} pnl={pnl:.2f}")

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[ERR] close_position {symbol}: {e}\n{tb}")
        tg.send_error(f"İşlem kapatılamadı: {symbol}", str(e))


# ============================================================
# ENTRY SCAN (every 5min candle close)
# ============================================================
def entry_scan(client: BybitClient, pm: PositionManager) -> None:
    print(f"[SCAN] entry scan @ {utc_now().isoformat()}")
    signals_found: List[str] = []
    scanned = 0
    crossover_count = 0
    filtered_count = 0  # crossovers that failed the channel-width filter
    filtered_symbols: List[str] = []

    for symbol in config.SYMBOLS:
        try:
            time.sleep(0.25)  # Rate limit
            klines = client.get_klines(symbol, config.TIMEFRAME, config.KLINE_LIMIT)
            # Strip current open (unclosed) candle - signal must come from closed candle
            if len(klines) >= 2:
                klines = klines[:-1]
            scanned += 1
            if len(klines) < config.EMA_HIGH_PERIOD + config.CHANNEL_AVG_PERIOD:
                continue

            # Reverse signal check for bot-managed positions only
            if pm.has(symbol):
                pos = pm.get(symbol)
                last_candle_start = klines[-1]["start"]
                if pos.last_reverse_check_candle != last_candle_start:
                    pos.last_reverse_check_candle = last_candle_start
                    if strategy.check_reverse_signal(pos.side, klines):
                        close_position(client, pm, symbol, "Ters Sinyal (EMA7 kanalı ters yönde kesti)")

            # Skip if already have a position (bot-managed or external)
            if pm.has(symbol) or symbol in EXTERNAL_POSITIONS:
                continue

            # Detect crossover for statistics (long or short direction)
            had_crossover = False
            passed_filter = False
            highs = [k["high"] for k in klines]
            lows = [k["low"] for k in klines]
            closes = [k["close"] for k in klines]
            ema_h = indicators.ema(highs, config.EMA_HIGH_PERIOD)
            ema_l = indicators.ema(lows, config.EMA_LOW_PERIOD)
            ema_t = indicators.ema(closes, config.EMA_TRIGGER_PERIOD)
            cw = indicators.channel_widths(ema_h, ema_l)
            cw_avg = indicators.rolling_average(cw, config.CHANNEL_AVG_PERIOD)

            i = len(klines) - 1
            j = i - 1
            if all(v is not None for v in (
                ema_h[i], ema_l[i], ema_t[i],
                ema_h[j], ema_l[j], ema_t[j],
            )):
                long_cross = ema_t[j] <= ema_h[j] and ema_t[i] > ema_h[i]
                short_cross = ema_t[j] >= ema_l[j] and ema_t[i] < ema_l[i]
                if long_cross or short_cross:
                    had_crossover = True
                    crossover_count += 1
                    direction = "L" if long_cross else "S"
                    if cw[i] is not None and cw_avg[i] is not None and cw[i] > cw_avg[i]:
                        passed_filter = True
                    else:
                        filtered_count += 1
                        filtered_symbols.append(f"{symbol}({direction})")

            # Check slot capacity (bot-managed + external both count)
            total_active = pm.count() + len(EXTERNAL_POSITIONS)
            if total_active >= config.MAX_POSITIONS:
                continue

            # Only evaluate full signal if crossover happened and filter passed
            # (small optimization - evaluate_entry would return None anyway otherwise)
            if had_crossover and passed_filter:
                signal = strategy.evaluate_entry(symbol, klines)
                if signal is not None:
                    signals_found.append(f"{symbol}({'L' if signal.side == 'Buy' else 'S'})")
                    open_position(client, pm, signal)
                    time.sleep(0.3)

        except Exception as e:
            print(f"[ERR] entry_scan {symbol}: {e}")
            continue

    total_active = pm.count() + len(EXTERNAL_POSITIONS)
    tg.send_scan_summary(
        scanned, signals_found, total_active, config.MAX_POSITIONS,
        crossover_count=crossover_count, filtered_count=filtered_count,
        filtered_symbols=filtered_symbols,
    )


# ============================================================
# EXIT SCAN (every 60s)
# ============================================================
def exit_scan(client: BybitClient, pm: PositionManager) -> None:
    # Check external positions for manual close (free up slot)
    for symbol in list(EXTERNAL_POSITIONS):
        try:
            ex_pos = client.get_position(symbol)
            if ex_pos is None:
                EXTERNAL_POSITIONS.discard(symbol)
                free_slots = config.MAX_POSITIONS - pm.count() - len(EXTERNAL_POSITIONS)
                tg.send_info(
                    f"📤 <b>Manuel pozisyon kapandı</b>: <code>{symbol}</code>\n"
                    f"Slot serbest. Boş slot: {free_slots}/{config.MAX_POSITIONS}"
                )
                print(f"[EXT-CLOSE] {symbol}")
        except Exception as e:
            print(f"[ERR] external check {symbol}: {e}")

    if pm.count() == 0:
        return

    for symbol, pos in list(pm.all().items()):
        try:
            # Check if position still open on exchange
            ex_pos = client.get_position(symbol)
            if ex_pos is None:
                # External close (SL hit on exchange)
                exit_price, pnl = get_closed_pnl(client, symbol)
                if exit_price is None:
                    exit_price = pos.current_sl
                pnl_pct = (pnl / pos.stake_usdt * 100) if pos.stake_usdt else 0
                tg.send_exit(
                    symbol=symbol, side=pos.side,
                    entry_price=pos.entry_price, exit_price=exit_price,
                    pnl_usdt=pnl, pnl_pct=pnl_pct, reason="Stop Loss (Borsa)",
                )
                record_trade(pnl)
                pm.close(symbol)
                print(f"[EXIT-SL] {symbol} pnl={pnl:.2f}")
                continue

            # Get current price, update extreme
            price = client.get_last_price(symbol)
            pos.update_extreme(price)

            # Peak-based profit measurements for stage transitions
            peak_profit_pct = pos.profit_pct_at(pos.extreme_price)
            peak_profit_atr = pos.profit_atr_at(pos.extreme_price)

            # ----- Stage 0 → 1: +2 ATR peak → SL +0.5%, CE 4 ATR -----
            if pos.stage == STAGE_ENTRY and peak_profit_atr >= config.STAGE1_TRIGGER_ATR:
                new_sl = compute_pct_profit_sl(pos.side, pos.entry_price, config.STAGE1_SL_PCT)
                try:
                    client.update_stop_loss(symbol, new_sl)
                    pos.current_sl = new_sl
                    pos.stage = STAGE_1_ATR
                    pos.ce_level = pos.compute_ce()  # 4 ATR trail from extreme
                    tg.send_stage1(symbol, pos.side, price, new_sl, pos.ce_level, pos.atr_at_entry)
                    print(f"[STAGE1] {symbol} sl={new_sl} ce={pos.ce_level}")
                except Exception as e:
                    print(f"[ERR] Stage1 SL update {symbol}: {e}")
                    tg.send_error(f"Aşama 1 SL güncellenemedi: {symbol}", str(e))

            # ----- Stage 1 → 2: +6 ATR peak → SL +0.2 ATR, CE narrows to 3 ATR -----
            if pos.stage == STAGE_1_ATR and peak_profit_atr >= config.STAGE2_TRIGGER_ATR:
                new_sl = compute_atr_profit_sl(pos.side, pos.entry_price,
                                               config.STAGE2_SL_ATR, pos.atr_at_entry)
                try:
                    client.update_stop_loss(symbol, new_sl)
                    pos.current_sl = new_sl
                    pos.stage = STAGE_2_ATR
                    pos.ce_level = pos.compute_ce()  # 3 ATR trail from extreme
                    tg.send_stage2(symbol, pos.side, price, new_sl, pos.ce_level, pos.atr_at_entry)
                    print(f"[STAGE2] {symbol} sl={new_sl} ce={pos.ce_level}")
                except Exception as e:
                    print(f"[ERR] Stage2 SL update {symbol}: {e}")
                    tg.send_error(f"Aşama 2 SL güncellenemedi: {symbol}", str(e))

            # ----- CE recompute (Stage 1 or 2): trail from updated extreme -----
            if pos.stage >= STAGE_1_ATR:
                pos.ce_level = pos.compute_ce()
                if pos.ce_hit(price):
                    close_position(client, pm, symbol, "Chandelier Exit (CE)")

        except Exception as e:
            print(f"[ERR] exit_scan {symbol}: {e}")
            continue


# ============================================================
# DAILY SUMMARY
# ============================================================
def maybe_send_daily_summary(last_sent_date) -> object:
    today = utc_now().date()
    if last_sent_date == today:
        return last_sent_date
    if DAILY_STATS["date"] is not None and DAILY_STATS["date"] != today and DAILY_STATS["trades"] > 0:
        tg.send_daily_summary(
            total_pnl=DAILY_STATS["pnl"],
            trade_count=DAILY_STATS["trades"],
            win_count=DAILY_STATS["wins"],
        )
        DAILY_STATS["date"] = today
        DAILY_STATS["pnl"] = 0.0
        DAILY_STATS["trades"] = 0
        DAILY_STATS["wins"] = 0
    return today


# ============================================================
# RESTORE OPEN POSITIONS (after bot restart)
# ============================================================
def restore_open_positions(client: BybitClient, pm: PositionManager) -> None:
    """
    Read open positions from Bybit and register them as EXTERNAL.
    The bot does NOT manage these (no CE, no stage tracking, no SL updates,
    no reverse-signal closing). It only counts them toward MAX_POSITIONS so
    new bot-managed entries respect the slot limit.

    When the user closes these manually, exit_scan detects it and frees the slot.
    """
    try:
        open_positions = client.get_open_positions()
    except Exception as e:
        print(f"[ERR] get_open_positions: {e}")
        tg.send_error("Açık pozisyonlar okunamadı", str(e))
        return

    if not open_positions:
        print("[RESTORE] No open positions")
        return

    detected = []
    for ex_pos in open_positions:
        symbol = ex_pos.get("symbol", "?")
        side = ex_pos.get("side", "?")
        try:
            qty = float(ex_pos.get("size", 0) or 0)
            entry_price = float(ex_pos.get("avgPrice", 0) or 0)
            if qty <= 0 or entry_price <= 0:
                continue
            EXTERNAL_POSITIONS.add(symbol)
            detected.append(f"{symbol} ({'LONG' if side == 'Buy' else 'SHORT'}) @ {entry_price}")
            print(f"[EXTERNAL] {symbol} {side} qty={qty} entry={entry_price}")
        except Exception as e:
            print(f"[ERR] restore {symbol}: {e}")
            continue

    count = len(EXTERNAL_POSITIONS)
    if count > 0:
        free_slots = config.MAX_POSITIONS - count
        lines = "\n".join(f"• {d}" for d in detected)
        tg.send_info(
            f"🔄 <b>{count} mevcut işlem tespit edildi</b> (manuel yönetilecek)\n"
            f"{lines}\n"
            f"Bot bu işlemlere dokunmayacak.\n"
            f"Boş slot: {free_slots}/{config.MAX_POSITIONS}"
        )


# ============================================================
# STARTUP
# ============================================================
def startup(client: BybitClient) -> None:
    global STAKE_USDT
    config.validate_config()
    balance = client.get_total_balance_usdt()
    if balance <= 0:
        raise RuntimeError(f"Total balance is zero or negative: {balance}")
    STAKE_USDT = balance * config.STAKE_PERCENT
    tg.send_bot_start(
        balance=balance,
        stake=STAKE_USDT,
        leverage=config.LEVERAGE,
        symbols=config.SYMBOLS,
    )
    print(f"[START] balance={balance:.2f} stake={STAKE_USDT:.2f}")


# ============================================================
# MAIN LOOP
# ============================================================
def main():
    client = BybitClient()
    pm = PositionManager()

    try:
        startup(client)
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[FATAL] startup: {e}\n{tb}")
        try:
            tg.send_error("Bot başlatılamadı", str(e))
        except Exception:
            pass
        return

    # Restore any open positions from Bybit (e.g. after restart)
    try:
        restore_open_positions(client, pm)
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[ERR] restore_open_positions: {e}\n{tb}")
        try:
            tg.send_error("Pozisyon geri yükleme hatası", str(e))
        except Exception:
            pass

    last_entry_scan_slot = -1
    last_exit_scan = 0.0
    last_daily_summary_date = utc_now().date()
    DAILY_STATS["date"] = utc_now().date()

    print("[LOOP] entering main loop")

    while True:
        try:
            now = now_ts()

            # Entry scan: once per 5min candle, ~5s after close
            slot = int(now // 300)
            seconds_into_slot = now - (slot * 300)
            if slot > last_entry_scan_slot and seconds_into_slot >= 5:
                entry_scan(client, pm)
                last_entry_scan_slot = slot

            # Exit scan: every 60s
            if now - last_exit_scan >= config.EXIT_SCAN_INTERVAL:
                exit_scan(client, pm)
                last_exit_scan = now

            # Daily summary at UTC midnight
            last_daily_summary_date = maybe_send_daily_summary(last_daily_summary_date)

            time.sleep(2)

        except KeyboardInterrupt:
            print("[STOP] keyboard interrupt")
            break
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[ERR] main loop: {e}\n{tb}")
            try:
                tg.send_error("Ana döngü hatası", str(e))
            except Exception:
                pass
            time.sleep(10)


if __name__ == "__main__":
    main()
