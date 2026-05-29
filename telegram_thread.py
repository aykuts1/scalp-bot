"""
📱 TELEGRAM THREAD

Sorumluluk:
- Anlık bildirimler (bot start/stop, işlem aç/kapa, seviye değiş, flag, hata, slot dolu)
- 1 saatlik durum raporu (saat başı)
- 12 saatlik Z raporu (00:00 ve 12:00)
- 24 saatlik X raporu (00:00)
- Komutlar: /status /stop /start /report /pause /resume

Tek thread içinde:
- Notifier (anlık)
- Reporter loop (saat takipli)
- Command poller (getUpdates ile)
"""
import threading
import time
import logging
import queue
import json
from datetime import datetime, timezone, timedelta

import urllib.request
import urllib.parse
import urllib.error

from utils import now_ts, fmt_money, fmt_pct, pct_diff

log = logging.getLogger("TelegramThread")


def _tg_request(token, method, payload=None, timeout=20):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise RuntimeError(f"Telegram HTTP {e.code}: {body}") from e
    except Exception as e:
        raise RuntimeError(f"Telegram request error: {e}") from e


def _send_message(token, chat_id, text):
    """Telegram mesajı gönder (uzunsa parçala)."""
    MAX_LEN = 3800  # 4096 limit, güvenli marj
    parts = []
    while len(text) > MAX_LEN:
        # \n üzerinden böl
        split_at = text.rfind("\n", 0, MAX_LEN)
        if split_at <= 0:
            split_at = MAX_LEN
        parts.append(text[:split_at])
        text = text[split_at:]
    parts.append(text)
    
    for p in parts:
        try:
            _tg_request(token, "sendMessage", {
                "chat_id": chat_id,
                "text": p,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except Exception as e:
            log.error(f"Telegram gönderme hatası: {e}")


class TelegramThread(threading.Thread):
    
    def __init__(self, config, data_manager, trade_manager_ref, control_ref):
        super().__init__(name="TelegramThread", daemon=True)
        self.cfg = config
        self.dm = data_manager
        # trade_manager ileride set edilir (Bot init sırasına bağlı)
        self.tm = trade_manager_ref
        # Bot kontrol referansı (start/stop için)
        self.ctrl = control_ref  # Bot main objesi
        
        self._stop = threading.Event()
        self._send_queue = queue.Queue()
        self._sender_thread = threading.Thread(target=self._sender_loop, daemon=True, name="TgSender")
        
        # Reports zamanlaması — UTC bazlı son tetik zamanı
        self._last_hourly = None
        self._last_12h = None
        self._last_24h = None
        
        # Boot anı PnL referans
        self._initial_balance_24h = None
        self._initial_balance_12h = None
        
        # Telegram update polling
        self._last_update_id = 0
    
    def set_trade_manager(self, tm):
        self.tm = tm
    
    def set_control(self, ctrl):
        self.ctrl = ctrl
    
    def stop(self):
        self._stop.set()
    
    # =================================================================
    # GÖNDERİM
    # =================================================================
    def _send(self, text):
        """Mesajı kuyruğa ekle (rate limit için)."""
        try:
            self._send_queue.put_nowait(text)
        except Exception:
            pass
    
    def _sender_loop(self):
        token = self.cfg.telegram_bot_token
        chat_id = self.cfg.telegram_chat_id
        while not self._stop.is_set():
            try:
                msg = self._send_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                _send_message(token, chat_id, msg)
            except Exception as e:
                log.error(f"Sender hata: {e}")
            time.sleep(0.5)  # rate limit
    
    # =================================================================
    # ANLIK BILDIRIMLER
    # =================================================================
    def notify_bot_started(self, config_dict):
        lines = ["🟢 <b>BOT BAŞLADI</b>",
                 f"🕒 {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
                 "",
                 "<b>📋 Ayarlar:</b>"]
        for k, v in config_dict.items():
            lines.append(f"• <b>{k}:</b> {v}")
        self._send("\n".join(lines))
    
    def notify_bot_stopped(self):
        self._send(f"🔴 <b>BOT DURDURULDU</b>\n🕒 {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    
    def notify_trade_open(self, trade):
        thread_emoji = {"RED": "🔴", "BLUE": "🔵", "YELLOW": "🟡"}.get(trade.thread, "⚪")
        side_emoji = "📈" if trade.side == "LONG" else "📉"
        lines = [
            f"{thread_emoji} <b>İŞLEM AÇILDI</b> {side_emoji}",
            f"<b>{trade.symbol} {trade.side}</b> ({trade.thread})",
            f"Giriş: <code>{trade.entry_price}</code>",
            f"Miktar: <code>{trade.qty}</code>",
        ]
        if trade.lose_line is not None:
            lines.append(f"LOSE: <code>{trade.lose_line}</code>")
        if trade.winrate_line is not None:
            lines.append(f"WINRATE: <code>{trade.winrate_line}</code>")
        self._send("\n".join(lines))
    
    def notify_trade_close(self, trade):
        thread_emoji = {"RED": "🔴", "BLUE": "🔵", "YELLOW": "🟡"}.get(trade.thread, "⚪")
        pnl_emoji = "✅" if trade.pnl_usdt >= 0 else "❌"
        lines = [
            f"{thread_emoji} <b>İŞLEM KAPANDI</b> {pnl_emoji}",
            f"<b>{trade.symbol} {trade.side}</b> ({trade.thread})",
            f"Giriş: <code>{trade.entry_price}</code>",
            f"Çıkış: <code>{trade.close_price}</code>",
            f"Çıkış: <b>{trade.exit_name}</b>",
            f"PnL: <b>{fmt_money(trade.pnl_usdt)} USDT ({fmt_pct(trade.pnl_pct)})</b>",
        ]
        self._send("\n".join(lines))
    
    def notify_level_change(self, trade, new_level):
        thread_emoji = {"RED": "🔴", "BLUE": "🔵", "YELLOW": "🟡"}.get(trade.thread, "⚪")
        self._send(
            f"{thread_emoji} <b>SEVİYE GEÇİŞİ</b>\n"
            f"{trade.symbol} {trade.side} ({trade.thread}) → <b>{new_level}</b>"
        )
    
    def notify_thread_ready(self, red_trade, thread, levels):
        emoji = "🔵" if thread == "BLUE" else "🟡"
        side = "LONG" if (thread == "BLUE" and red_trade.side == "SHORT") or (thread == "YELLOW" and red_trade.side == "LONG") else "SHORT"
        lines = [
            f"{emoji} <b>{thread} THREAD HAZIR</b>",
            f"{red_trade.symbol} {side}",
            f"Bağlı Kırmızı: #{red_trade.id} ({red_trade.side})",
            "<b>Seviyeler:</b>",
        ]
        for k, v in levels.items():
            lines.append(f"• {k}: <code>{v}</code>")
        self._send("\n".join(lines))
    
    def notify_flag(self, symbol, thread, side, event):
        emoji = {"RED": "🔴", "BLUE": "🔵", "YELLOW": "🟡"}.get(thread, "⚪")
        event_text = {"OPENED": "AÇILDI", "DELETED": "SİLİNDİ", "CONVERTED": "İŞLEME DÖNÜŞTÜ"}.get(event, event)
        self._send(f"🚩 {emoji} {symbol} {side} FLAG {event_text}")
    
    def notify_insufficient_balance(self, symbol, side, thread, qty):
        self._send(
            f"⚠️ <b>YETERSİZ BAKİYE</b>\n"
            f"{symbol} {side} ({thread}) — İşlem açılamadı (qty={qty})"
        )
    
    def notify_slot_full(self, symbol, side, thread):
        self._send(
            f"🚫 <b>COİNDE KIRMIZI VAR</b>\n"
            f"{symbol} {side} ({thread}) — Bu coinde zaten bir Kırmızı işlem açık, yeni işlem açılamaz"
        )
    
    def notify_error(self, error_type, symbol, thread, detail):
        self._send(
            f"❌ <b>HATA</b>\n"
            f"<b>Tip:</b> {error_type}\n"
            f"<b>Coin:</b> {symbol}\n"
            f"<b>Thread:</b> {thread}\n"
            f"<b>Detay:</b> <code>{detail[:300]}</code>"
        )
    
    def notify_stake_update(self, new_stake, balance):
        self._send(
            f"💰 <b>STAKE GÜNCELLENDİ</b>\n"
            f"Bakiye: <b>{fmt_money(balance)} USDT</b>\n"
            f"Yeni Stake: <b>{fmt_money(new_stake)} USDT</b>"
        )
    
    # =================================================================
    # RAPORLAR
    # =================================================================
    def _build_open_trades_block(self):
        if self.tm is None:
            return ["(trade manager yok)"]
        opens = self.tm.slots.get_all_open()
        if not opens:
            return ["(açık işlem yok)"]
        lines = []
        for t in opens:
            price = self.dm.get_last_price(t.symbol) or t.entry_price
            pnl_raw = pct_diff(t.entry_price, price, t.side)
            pnl_usdt = self.tm.stake_usdt * self.cfg.leverage * (pnl_raw / 100.0)
            emoji = {"RED": "🔴", "BLUE": "🔵", "YELLOW": "🟡"}.get(t.thread, "⚪")
            mins = (now_ts() - t.opened_ts) // 60
            lines.append(
                f"{emoji} {t.symbol} {t.side} ({t.thread}) | "
                f"Lv: {t.current_level} | "
                f"PnL: {fmt_money(pnl_usdt)} USDT ({fmt_pct(pnl_raw * self.cfg.leverage)}) | "
                f"{mins}dk"
            )
        return lines
    
    def _build_open_flags_block(self):
        if self.tm is None:
            return ["(yok)"]
        lines = []
        # Red flags
        red = self.ctrl.red_thread if self.ctrl else None
        if red:
            for sym, fl in red.flags.items():
                if fl["long_flag"]:
                    lines.append(f"🔴 {sym} LONG Flag")
                if fl["short_flag"]:
                    lines.append(f"🔴 {sym} SHORT Flag")
        # Blue flags
        blue = self.ctrl.blue_thread if self.ctrl else None
        if blue:
            with blue.tables_lock:
                for tbl in blue.tables.values():
                    if tbl.flag_open and tbl.active_trade is None:
                        lines.append(f"🔵 {tbl.symbol} {tbl.side} Flag")
        # Yellow flags
        yellow = self.ctrl.yellow_thread if self.ctrl else None
        if yellow:
            with yellow.tables_lock:
                for tbl in yellow.tables.values():
                    if tbl.flag_open and tbl.active_trade is None:
                        lines.append(f"🟡 {tbl.symbol} {tbl.side} Flag")
        if not lines:
            return ["(açık flag yok)"]
        return lines
    
    def send_hourly_report(self):
        """1 saatlik durum raporu."""
        if self.tm is None:
            return
        end_ts = now_ts()
        start_ts = end_ts - 3600
        closed = self.tm.get_closed_trades_window(start_ts, end_ts)
        opens = self.tm.slots.get_all_open()
        
        lines = [
            f"🕐 <b>1 SAATLİK DURUM RAPORU</b>",
            f"🕒 {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            f"<b>📌 AÇIK İŞLEMLER ({len(opens)})</b>",
        ]
        lines.extend(self._build_open_trades_block())
        lines.append("")
        lines.append(f"<b>📌 SON 1 SAATTE KAPANAN ({len(closed)})</b>")
        if not closed:
            lines.append("(yok)")
        else:
            for t in closed:
                emoji = {"RED": "🔴", "BLUE": "🔵", "YELLOW": "🟡"}.get(t.thread, "⚪")
                lines.append(
                    f"{emoji} {t.symbol} {t.side} | {t.exit_name} | "
                    f"PnL: {fmt_money(t.pnl_usdt)} USDT ({fmt_pct(t.pnl_pct)})"
                )
        lines.append("")
        lines.append("<b>📌 AÇIK FLAGLAR</b>")
        lines.extend(self._build_open_flags_block())
        lines.append("")
        # Özet
        total_pnl = sum(t.pnl_usdt for t in closed)
        opened_count = len([t for t in closed if t.opened_ts >= start_ts]) + len([t for t in opens if t.opened_ts >= start_ts])
        lines.append("<b>📊 ÖZET</b>")
        lines.append(f"Toplam açık: {len(opens)}")
        lines.append(f"Son 1 saat kapanan: {len(closed)}")
        lines.append(f"Son 1 saat PnL: <b>{fmt_money(total_pnl)} USDT</b>")
        lines.append(f"Bakiye: <b>{fmt_money(self.dm.get_balance())} USDT</b>")
        
        self._send("\n".join(lines))
    
    def send_12h_report(self):
        """12 saatlik Z raporu."""
        if self.tm is None:
            return
        end_ts = now_ts()
        start_ts = end_ts - 12 * 3600
        closed = self.tm.get_closed_trades_window(start_ts, end_ts)
        opens = self.tm.slots.get_all_open()
        
        # İstatistik
        total_pnl = sum(t.pnl_usdt for t in closed)
        winners = [t for t in closed if t.pnl_usdt > 0]
        losers = [t for t in closed if t.pnl_usdt < 0]
        winrate = (len(winners) / len(closed) * 100.0) if closed else 0.0
        total_win = sum(t.pnl_usdt for t in winners)
        total_loss = abs(sum(t.pnl_usdt for t in losers))
        profit_factor = (total_win / total_loss) if total_loss > 0 else (float("inf") if total_win > 0 else 0.0)
        
        best = max(closed, key=lambda t: t.pnl_usdt, default=None)
        worst = min(closed, key=lambda t: t.pnl_usdt, default=None)
        
        # Thread bazında
        thread_stats = {}
        for th in ("RED", "BLUE", "YELLOW"):
            t_list = [t for t in closed if t.thread == th]
            t_win = [t for t in t_list if t.pnl_usdt > 0]
            t_pnl = sum(t.pnl_usdt for t in t_list)
            t_wr = (len(t_win) / len(t_list) * 100.0) if t_list else 0.0
            thread_stats[th] = {"count": len(t_list), "pnl": t_pnl, "wr": t_wr}
        
        # Coin bazında
        coin_stats = {}
        for t in closed:
            d = coin_stats.setdefault(t.symbol, {"count": 0, "pnl": 0.0, "win": 0})
            d["count"] += 1
            d["pnl"] += t.pnl_usdt
            if t.pnl_usdt > 0:
                d["win"] += 1
        
        # Çıkış tipi
        exit_stats = {}
        for t in closed:
            d = exit_stats.setdefault(t.exit_name, {"count": 0, "pnl": 0.0})
            d["count"] += 1
            d["pnl"] += t.pnl_usdt
        
        # Seviye
        reached_winrate = len([t for t in closed if "WINRATE" in (t.exit_name or "")])
        no_st = len([t for t in closed if t.highest_level in ("ENTRY", "FLAG", "WAIT")])
        
        lines = [
            f"🕛 <b>12 SAATLİK Z RAPORU</b>",
            f"🕒 {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            "<b>💰 PNL ÖZET</b>",
            f"12s Toplam PnL: <b>{fmt_money(total_pnl)} USDT</b>",
            f"Bakiye: <b>{fmt_money(self.dm.get_balance())} USDT</b>",
            "",
            "<b>📊 İŞLEM İSTATİSTİĞİ</b>",
            f"Açık: {len(opens)} | Kapanan: {len(closed)}",
            f"Kazanan: {len(winners)} | Kaybeden: {len(losers)} | Winrate: %{winrate:.1f}",
            f"Profit Factor: {profit_factor:.2f}",
        ]
        if best:
            lines.append(f"En iyi: {best.symbol} {best.side} ({best.thread}) → {fmt_money(best.pnl_usdt)} USDT")
        if worst:
            lines.append(f"En kötü: {worst.symbol} {worst.side} ({worst.thread}) → {fmt_money(worst.pnl_usdt)} USDT")
        
        lines.append("")
        lines.append("<b>📌 THREAD BAZINDA</b>")
        for th in ("RED", "BLUE", "YELLOW"):
            s = thread_stats[th]
            emoji = {"RED": "🔴", "BLUE": "🔵", "YELLOW": "🟡"}[th]
            lines.append(f"{emoji} {th}: {s['count']} işlem | WR %{s['wr']:.1f} | PnL {fmt_money(s['pnl'])} USDT")
        
        lines.append("")
        lines.append("<b>📌 COİN BAZINDA</b>")
        if not coin_stats:
            lines.append("(yok)")
        else:
            for sym, s in sorted(coin_stats.items(), key=lambda x: -x[1]["pnl"]):
                wr = (s["win"] / s["count"] * 100.0) if s["count"] else 0.0
                lines.append(f"{sym}: {s['count']} işlem | WR %{wr:.1f} | PnL {fmt_money(s['pnl'])} USDT")
        
        lines.append("")
        lines.append("<b>📌 ÇIKIŞ TİPLERİ</b>")
        if not exit_stats:
            lines.append("(yok)")
        else:
            for name, s in sorted(exit_stats.items(), key=lambda x: -x[1]["count"]):
                lines.append(f"{name}: {s['count']} adet | PnL {fmt_money(s['pnl'])} USDT")
        
        lines.append("")
        lines.append("<b>📌 SEVİYE</b>")
        lines.append(f"WINRATE ulaşan: {reached_winrate}")
        lines.append(f"Hiç ST geçemeyen: {no_st}")
        
        lines.append("")
        lines.append(f"<b>📌 AÇIK İŞLEMLER ({len(opens)})</b>")
        lines.extend(self._build_open_trades_block())
        lines.append("")
        lines.append("<b>📌 AÇIK FLAGLAR</b>")
        lines.extend(self._build_open_flags_block())
        
        # Uyarılar
        lines.append("")
        lines.append("<b>📌 UYARILAR (genel)</b>")
        lines.append(f"Yetersiz bakiye: {self.tm._insufficient_balance_count}")
        lines.append(f"Slot dolu: {self.tm._slot_full_count}")
        lines.append(f"Hata: {self.tm._error_count}")
        
        self._send("\n".join(lines))
    
    def send_24h_report(self):
        """24 saatlik X raporu — tam detay."""
        if self.tm is None:
            return
        end_ts = now_ts()
        start_ts = end_ts - 24 * 3600
        closed = self.tm.get_closed_trades_window(start_ts, end_ts)
        opens = self.tm.slots.get_all_open()
        
        total_pnl = sum(t.pnl_usdt for t in closed)
        winners = [t for t in closed if t.pnl_usdt > 0]
        losers = [t for t in closed if t.pnl_usdt < 0]
        winrate = (len(winners) / len(closed) * 100.0) if closed else 0.0
        total_win = sum(t.pnl_usdt for t in winners)
        total_loss = abs(sum(t.pnl_usdt for t in losers))
        profit_factor = (total_win / total_loss) if total_loss > 0 else (float("inf") if total_win > 0 else 0.0)
        avg_win = (total_win / len(winners)) if winners else 0.0
        avg_loss = (total_loss / len(losers)) if losers else 0.0
        
        durations = [(t.close_ts - t.opened_ts) / 60.0 for t in closed if t.close_ts]
        avg_dur = sum(durations) / len(durations) if durations else 0.0
        max_dur = max(durations) if durations else 0.0
        min_dur = min(durations) if durations else 0.0
        
        best = max(closed, key=lambda t: t.pnl_usdt, default=None)
        worst = min(closed, key=lambda t: t.pnl_usdt, default=None)
        
        # Thread
        thread_stats = {}
        for th in ("RED", "BLUE", "YELLOW"):
            t_list = [t for t in closed if t.thread == th]
            t_open = [t for t in opens if t.thread == th]
            t_win = [t for t in t_list if t.pnl_usdt > 0]
            t_loss = [t for t in t_list if t.pnl_usdt < 0]
            t_long = [t for t in t_list if t.side == "LONG"]
            t_short = [t for t in t_list if t.side == "SHORT"]
            t_pnl = sum(t.pnl_usdt for t in t_list)
            t_wr = (len(t_win) / len(t_list) * 100.0) if t_list else 0.0
            best_th = max(t_list, key=lambda t: t.pnl_usdt, default=None)
            worst_th = min(t_list, key=lambda t: t.pnl_usdt, default=None)
            avg_d = (sum((t.close_ts - t.opened_ts) for t in t_list if t.close_ts) / len(t_list) / 60.0) if t_list else 0.0
            avg_w = (sum(t.pnl_usdt for t in t_win) / len(t_win)) if t_win else 0.0
            avg_l = (sum(t.pnl_usdt for t in t_loss) / len(t_loss)) if t_loss else 0.0
            tpf = (sum(t.pnl_usdt for t in t_win) / abs(sum(t.pnl_usdt for t in t_loss))) if t_loss and sum(t.pnl_usdt for t in t_loss) != 0 else 0.0
            thread_stats[th] = {
                "open": len(t_open),
                "closed": len(t_list),
                "long": len(t_long),
                "short": len(t_short),
                "win": len(t_win),
                "loss": len(t_loss),
                "pnl": t_pnl,
                "wr": t_wr,
                "avg_win": avg_w,
                "avg_loss": avg_l,
                "pf": tpf,
                "best": best_th,
                "worst": worst_th,
                "avg_dur": avg_d,
            }
        
        # Coin
        coin_stats = {}
        for t in closed:
            d = coin_stats.setdefault(t.symbol, {"count": 0, "open": 0, "long": 0, "short": 0,
                                                  "win": 0, "loss": 0, "pnl": 0.0, "best": None, "worst": None})
            d["count"] += 1
            if t.side == "LONG":
                d["long"] += 1
            else:
                d["short"] += 1
            if t.pnl_usdt > 0:
                d["win"] += 1
            elif t.pnl_usdt < 0:
                d["loss"] += 1
            d["pnl"] += t.pnl_usdt
            if d["best"] is None or t.pnl_usdt > d["best"].pnl_usdt:
                d["best"] = t
            if d["worst"] is None or t.pnl_usdt < d["worst"].pnl_usdt:
                d["worst"] = t
        for o in opens:
            d = coin_stats.setdefault(o.symbol, {"count": 0, "open": 0, "long": 0, "short": 0,
                                                  "win": 0, "loss": 0, "pnl": 0.0, "best": None, "worst": None})
            d["open"] += 1
        
        # Çıkış tipi
        exit_stats = {}
        for t in closed:
            d = exit_stats.setdefault(t.exit_name or "?", {"count": 0, "pnl": 0.0})
            d["count"] += 1
            d["pnl"] += t.pnl_usdt
        
        # Long/Short
        long_list = [t for t in closed if t.side == "LONG"]
        short_list = [t for t in closed if t.side == "SHORT"]
        long_wr = (len([t for t in long_list if t.pnl_usdt > 0]) / len(long_list) * 100.0) if long_list else 0.0
        short_wr = (len([t for t in short_list if t.pnl_usdt > 0]) / len(short_list) * 100.0) if short_list else 0.0
        long_pnl = sum(t.pnl_usdt for t in long_list)
        short_pnl = sum(t.pnl_usdt for t in short_list)
        
        # Zaman analizi
        hour_stats = {h: {"count": 0, "pnl": 0.0} for h in range(24)}
        for t in closed:
            if not t.close_ts:
                continue
            h = datetime.fromtimestamp(t.close_ts, tz=timezone.utc).hour
            hour_stats[h]["count"] += 1
            hour_stats[h]["pnl"] += t.pnl_usdt
        most_active_hour = max(hour_stats.keys(), key=lambda h: hour_stats[h]["count"])
        most_profit_hour = max(hour_stats.keys(), key=lambda h: hour_stats[h]["pnl"])
        most_loss_hour = min(hour_stats.keys(), key=lambda h: hour_stats[h]["pnl"])
        
        # Flag
        flag_24h = [f for f in self.tm.flag_history if f["ts"] >= start_ts]
        flag_open_count = len([f for f in flag_24h if f["event"] == "OPENED"])
        flag_conv_count = len([f for f in flag_24h if f["event"] == "CONVERTED"])
        flag_del_count = len([f for f in flag_24h if f["event"] == "DELETED"])
        flag_red = len([f for f in flag_24h if f["thread"] == "RED" and f["event"] == "OPENED"])
        flag_blue = len([f for f in flag_24h if f["thread"] == "BLUE" and f["event"] == "OPENED"])
        flag_yellow = len([f for f in flag_24h if f["thread"] == "YELLOW" and f["event"] == "OPENED"])
        
        # Seviye analizi
        reached_winrate = len([t for t in closed if "WINRATE" in (t.exit_name or "")])
        reached_lose = len([t for t in closed if "LOSE" in (t.exit_name or "")])
        no_st = len([t for t in closed if t.highest_level in ("ENTRY", "FLAG", "WAIT")])
        
        # Streak
        sorted_by_close = sorted([t for t in closed if t.close_ts], key=lambda x: x.close_ts)
        cur_win, cur_loss, max_win_streak, max_loss_streak = 0, 0, 0, 0
        for t in sorted_by_close:
            if t.pnl_usdt > 0:
                cur_win += 1
                cur_loss = 0
                max_win_streak = max(max_win_streak, cur_win)
            elif t.pnl_usdt < 0:
                cur_loss += 1
                cur_win = 0
                max_loss_streak = max(max_loss_streak, cur_loss)
        
        # Render
        lines = [
            "📅 <b>24 SAATLİK X RAPORU</b>",
            f"🕒 {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            "<b>💰 GENEL PNL</b>",
            f"24s Toplam PnL: <b>{fmt_money(total_pnl)} USDT</b>",
            f"Bakiye: <b>{fmt_money(self.dm.get_balance())} USDT</b>",
            "",
            "<b>📊 GENEL İSTATİSTİK</b>",
            f"Açılan (kapanan): {len(closed)} | Açık: {len(opens)}",
            f"Kazanan: {len(winners)} | Kaybeden: {len(losers)} | Winrate: %{winrate:.1f}",
            f"Profit Factor: {profit_factor:.2f}",
            f"Ortalama kazanç: {fmt_money(avg_win)} USDT",
            f"Ortalama kayıp: {fmt_money(avg_loss)} USDT",
            f"En uzun kazanç serisi: {max_win_streak}",
            f"En uzun kayıp serisi: {max_loss_streak}",
            f"Ort. işlem süresi: {avg_dur:.1f} dk",
            f"En kısa işlem: {min_dur:.1f} dk",
            f"En uzun işlem: {max_dur:.1f} dk",
            "",
            "<b>🏆 EN İYİ / EN KÖTÜ</b>",
        ]
        if best:
            lines.append(f"En iyi: {best.symbol} {best.side} ({best.thread}) → {fmt_money(best.pnl_usdt)} USDT")
        if worst:
            lines.append(f"En kötü: {worst.symbol} {worst.side} ({worst.thread}) → {fmt_money(worst.pnl_usdt)} USDT")
        if coin_stats:
            best_coin = max(coin_stats.items(), key=lambda x: x[1]["pnl"])
            worst_coin = min(coin_stats.items(), key=lambda x: x[1]["pnl"])
            lines.append(f"En çok kazandıran coin: {best_coin[0]} → {fmt_money(best_coin[1]['pnl'])} USDT")
            lines.append(f"En çok kaybettiren coin: {worst_coin[0]} → {fmt_money(worst_coin[1]['pnl'])} USDT")
        best_thread = max(thread_stats.items(), key=lambda x: x[1]["pnl"])
        worst_thread = min(thread_stats.items(), key=lambda x: x[1]["pnl"])
        lines.append(f"En çok kazandıran thread: {best_thread[0]} → {fmt_money(best_thread[1]['pnl'])} USDT")
        lines.append(f"En çok kaybettiren thread: {worst_thread[0]} → {fmt_money(worst_thread[1]['pnl'])} USDT")
        lines.append(f"Long PnL: {fmt_money(long_pnl)} | Short PnL: {fmt_money(short_pnl)}")
        
        # Thread bazında detay
        lines.append("")
        for th in ("RED", "BLUE", "YELLOW"):
            s = thread_stats[th]
            emoji = {"RED": "🔴", "BLUE": "🔵", "YELLOW": "🟡"}[th]
            lines.append(f"<b>{emoji} {th} THREAD</b>")
            lines.append(f"Açılan: {s['closed']} | Açık: {s['open']}")
            lines.append(f"Long: {s['long']} | Short: {s['short']}")
            lines.append(f"Kazanan: {s['win']} | Kaybeden: {s['loss']} | WR: %{s['wr']:.1f}")
            lines.append(f"PnL: {fmt_money(s['pnl'])} USDT | PF: {s['pf']:.2f}")
            lines.append(f"Ort. kazanç: {fmt_money(s['avg_win'])} | Ort. kayıp: {fmt_money(s['avg_loss'])}")
            if s["best"]:
                lines.append(f"En iyi: {s['best'].symbol} {s['best'].side} → {fmt_money(s['best'].pnl_usdt)}")
            if s["worst"]:
                lines.append(f"En kötü: {s['worst'].symbol} {s['worst'].side} → {fmt_money(s['worst'].pnl_usdt)}")
            lines.append(f"Ort. süre: {s['avg_dur']:.1f} dk")
            lines.append("")
        
        # Coin detay
        lines.append("<b>📌 COİN BAZINDA</b>")
        if not coin_stats:
            lines.append("(yok)")
        else:
            for sym, s in sorted(coin_stats.items(), key=lambda x: -x[1]["pnl"]):
                wr = (s["win"] / s["count"] * 100.0) if s["count"] else 0.0
                lines.append(
                    f"{sym}: {s['count']} işlem ({s['long']}L/{s['short']}S) | "
                    f"Açık: {s['open']} | WR %{wr:.1f} | PnL {fmt_money(s['pnl'])}"
                )
                if s["best"]:
                    lines.append(f"  └ En iyi: {s['best'].thread} {s['best'].side} → {fmt_money(s['best'].pnl_usdt)}")
                if s["worst"] and s["worst"] is not s["best"]:
                    lines.append(f"  └ En kötü: {s['worst'].thread} {s['worst'].side} → {fmt_money(s['worst'].pnl_usdt)}")
        
        # Çıkış tipi
        lines.append("")
        lines.append("<b>📌 ÇIKIŞ TİPLERİ</b>")
        if exit_stats:
            for name, s in sorted(exit_stats.items(), key=lambda x: -x[1]["count"]):
                lines.append(f"{name}: {s['count']} adet | PnL {fmt_money(s['pnl'])} USDT")
            best_exit = max(exit_stats.items(), key=lambda x: x[1]["pnl"])
            most_freq = max(exit_stats.items(), key=lambda x: x[1]["count"])
            lines.append(f"En karlı çıkış: {best_exit[0]}")
            lines.append(f"En sık çıkış: {most_freq[0]}")
        else:
            lines.append("(yok)")
        
        # Seviye
        lines.append("")
        lines.append("<b>📌 SEVİYE ANALİZİ</b>")
        lines.append(f"WINRATE çıkışı: {reached_winrate}")
        lines.append(f"LOSE çıkışı: {reached_lose}")
        lines.append(f"Hiç ST geçemeyen: {no_st}")
        
        # Long/Short
        lines.append("")
        lines.append("<b>📌 LONG / SHORT</b>")
        lines.append(f"LONG: {len(long_list)} işlem | WR %{long_wr:.1f} | PnL {fmt_money(long_pnl)}")
        lines.append(f"SHORT: {len(short_list)} işlem | WR %{short_wr:.1f} | PnL {fmt_money(short_pnl)}")
        
        # Zaman
        lines.append("")
        lines.append("<b>📌 ZAMAN ANALİZİ (UTC)</b>")
        lines.append(f"En aktif saat: {most_active_hour:02d}:00 ({hour_stats[most_active_hour]['count']} işlem)")
        lines.append(f"En karlı saat: {most_profit_hour:02d}:00 ({fmt_money(hour_stats[most_profit_hour]['pnl'])} USDT)")
        lines.append(f"En zararlı saat: {most_loss_hour:02d}:00 ({fmt_money(hour_stats[most_loss_hour]['pnl'])} USDT)")
        
        # Flag
        lines.append("")
        lines.append("<b>📌 FLAG İSTATİSTİĞİ</b>")
        lines.append(f"Açılan flag: {flag_open_count} (🔴{flag_red} 🔵{flag_blue} 🟡{flag_yellow})")
        lines.append(f"İşleme dönüşen: {flag_conv_count}")
        lines.append(f"Silinen: {flag_del_count}")
        conv_rate = (flag_conv_count / flag_open_count * 100.0) if flag_open_count else 0.0
        lines.append(f"Dönüşüm oranı: %{conv_rate:.1f}")
        
        # Açık işlemler
        lines.append("")
        lines.append(f"<b>📌 AÇIK İŞLEMLER ({len(opens)})</b>")
        lines.extend(self._build_open_trades_block())
        
        # Açık flaglar
        lines.append("")
        lines.append("<b>📌 AÇIK FLAGLAR</b>")
        lines.extend(self._build_open_flags_block())
        
        # Hata/uyarı
        lines.append("")
        lines.append("<b>📌 HATALAR / UYARILAR</b>")
        lines.append(f"Yetersiz bakiye: {self.tm._insufficient_balance_count}")
        lines.append(f"Slot dolu: {self.tm._slot_full_count}")
        lines.append(f"Hata: {self.tm._error_count}")
        
        self._send("\n".join(lines))
    
    # =================================================================
    # KOMUTLAR
    # =================================================================
    def _handle_command(self, text):
        text = (text or "").strip()
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:]
        
        if cmd in ("/start",):
            if self.ctrl is None:
                self._send("Bot referansı yok.")
                return
            if self.ctrl.is_running():
                self._send("Bot zaten çalışıyor.")
            else:
                self.ctrl.start_trading()
                self._send("✅ Bot başlatıldı.")
        
        elif cmd in ("/stop",):
            if self.ctrl is None:
                self._send("Bot referansı yok.")
                return
            self.ctrl.stop_trading()
            self._send("⛔ Bot durduruldu.")
        
        elif cmd in ("/status",):
            self._send_status()
        
        elif cmd in ("/report",):
            self.send_hourly_report()
        
        elif cmd in ("/pause",):
            if not args:
                self._send("Kullanım: /pause SYMBOL")
                return
            sym = args[0].upper()
            if sym not in self.cfg.symbols:
                self._send(f"{sym} listede yok.")
                return
            self.dm.pause_coin(sym)
            self._send(f"⏸ {sym} duraklatıldı.")
        
        elif cmd in ("/resume",):
            if not args:
                self._send("Kullanım: /resume SYMBOL")
                return
            sym = args[0].upper()
            self.dm.resume_coin(sym)
            self._send(f"▶ {sym} devam ettirildi.")
        
        elif cmd in ("/help",):
            self._send(
                "📖 <b>Komutlar:</b>\n"
                "/status - Anlık durum\n"
                "/report - 1 saatlik rapor\n"
                "/start - Botu başlat\n"
                "/stop - Botu durdur\n"
                "/pause SYMBOL - Coin durdur\n"
                "/resume SYMBOL - Coin devam ettir"
            )
        else:
            self._send(f"Bilinmeyen komut: {cmd}\n/help için bakabilirsin.")
    
    def _send_status(self):
        opens = self.tm.slots.get_all_open() if self.tm else []
        lines = [
            "<b>📊 DURUM</b>",
            f"Bakiye: <b>{fmt_money(self.dm.get_balance())} USDT</b>",
            f"Stake: <b>{fmt_money(self.tm.get_stake()) if self.tm else 0} USDT</b>",
            f"Çalışıyor mu: {'EVET' if (self.ctrl and self.ctrl.is_running()) else 'HAYIR'}",
            f"Açık işlem: {len(opens)}",
            "",
            "<b>Açık işlemler:</b>",
        ]
        lines.extend(self._build_open_trades_block())
        lines.append("")
        lines.append("<b>Açık flaglar:</b>")
        lines.extend(self._build_open_flags_block())
        self._send("\n".join(lines))
    
    def _poll_commands(self):
        token = self.cfg.telegram_bot_token
        try:
            r = _tg_request(token, "getUpdates", {
                "offset": self._last_update_id + 1,
                "timeout": 0,
                "allowed_updates": ["message"],
            }, timeout=10)
        except Exception as e:
            log.error(f"getUpdates hatası: {e}")
            return
        if not r.get("ok"):
            return
        for u in r.get("result", []):
            self._last_update_id = max(self._last_update_id, int(u.get("update_id", 0)))
            msg = u.get("message") or {}
            chat = msg.get("chat") or {}
            if str(chat.get("id")) != str(self.cfg.telegram_chat_id):
                continue  # başka chat ise yoksay
            text = msg.get("text", "")
            try:
                self._handle_command(text)
            except Exception as e:
                log.exception(f"Komut hatası: {e}")
                self._send(f"Komut hatası: {e}")
    
    # =================================================================
    # ZAMANLAMA
    # =================================================================
    def _check_reports(self):
        """Saat başı, 12 ve 24 saat raporlarını UTC'de tetikle."""
        now = datetime.now(tz=timezone.utc)
        # Hourly
        hour_key = (now.year, now.month, now.day, now.hour)
        if self._last_hourly != hour_key and now.minute == 0:
            self._last_hourly = hour_key
            try:
                self.send_hourly_report()
            except Exception as e:
                log.exception(f"Hourly rapor hatası: {e}")
        
        # 12h: 00:00 ve 12:00
        if now.hour in (0, 12) and now.minute == 0:
            twelve_key = (now.year, now.month, now.day, now.hour)
            if self._last_12h != twelve_key:
                self._last_12h = twelve_key
                try:
                    self.send_12h_report()
                except Exception as e:
                    log.exception(f"12h rapor hatası: {e}")
        
        # 24h: 00:00
        if now.hour == 0 and now.minute == 0:
            day_key = (now.year, now.month, now.day)
            if self._last_24h != day_key:
                self._last_24h = day_key
                try:
                    self.send_24h_report()
                except Exception as e:
                    log.exception(f"24h rapor hatası: {e}")
    
    # =================================================================
    # RUN
    # =================================================================
    def run(self):
        log.info("Telegram thread başladı.")
        self._sender_thread.start()
        while not self._stop.is_set():
            try:
                self._check_reports()
                self._poll_commands()
            except Exception as e:
                log.exception(f"Telegram döngü hatası: {e}")
            time.sleep(2.0)
        log.info("Telegram thread durdu.")
