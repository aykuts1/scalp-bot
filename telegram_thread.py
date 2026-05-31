"""
📱 TELEGRAM THREAD

- Anlık bildirimler (6 thread: 🔴🔵🟡⚪️🟣🟠)
- 3 rapor: hourly, 12h Z, 24h X
- Komut polling (long polling)
- Flag bildirimleri ATILMAZ — sadece raporlarda görünür
"""
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from utils import fmt_money, fmt_pct, now_ts, utc_now

log = logging.getLogger("TelegramThread")

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LEN = 3800

# 6 thread ikonları ve isimleri
THREAD_ICONS = {
    "RED": "🔴", "BLUE": "🔵", "YELLOW": "🟡",
    "WHITE": "⚪️", "PURPLE": "🟣", "ORANGE": "🟠",
}
THREAD_NAMES = {
    "RED": "KIRMIZI", "BLUE": "MAVİ", "YELLOW": "SARI",
    "WHITE": "BEYAZ", "PURPLE": "MOR", "ORANGE": "TURUNCU",
}
THREAD_ORDER = ["RED", "BLUE", "YELLOW", "WHITE", "PURPLE", "ORANGE"]


# ------------------------------------------------------------------
# HTTP yardımcıları
# ------------------------------------------------------------------
def _tg_request(token, method, payload=None, timeout=35):
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = str(e)
        log.warning(f"Telegram HTTP {e.code}: {err_body}")
        return {"ok": False, "error_code": e.code, "description": err_body}
    except Exception as e:
        log.warning(f"Telegram bağlantı hatası: {e}")
        return {"ok": False, "description": str(e)}


# ------------------------------------------------------------------
# TELEGRAM THREAD
# ------------------------------------------------------------------
class TelegramThread(threading.Thread):
    def __init__(self, config, data_manager=None, trade_manager_ref=None, control_ref=None):
        super().__init__(name="TelegramThread", daemon=True)
        self.cfg = config
        self.token = config.telegram_bot_token
        self.chat_id = config.telegram_chat_id

        self.dm = data_manager
        self.tm = trade_manager_ref
        self.ctrl = control_ref

        self._stop = threading.Event()
        self._send_queue = queue.Queue()
        self._sender_thread = None

        # Rapor zamanlama
        self._last_hourly_key = None
        self._last_12h_key = None
        self._last_24h_key = None

        # Komut polling
        self._last_update_id = 0

        self.bot_start_ts = now_ts()

    def set_trade_manager(self, tm):
        self.tm = tm

    def set_control(self, ctrl):
        self.ctrl = ctrl

    def stop(self):
        self._stop.set()

    # ==================================================================
    # MESAJ GÖNDERME
    # ==================================================================
    def _enqueue(self, text):
        try:
            self._send_queue.put(text, timeout=1)
        except Exception:
            pass

    def _send_now(self, text):
        chunks = self._split_message(text)
        for chunk in chunks:
            payload = {
                "chat_id": self.chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            result = _tg_request(self.token, "sendMessage", payload)
            if not result.get("ok"):
                time.sleep(1.0)
                _tg_request(self.token, "sendMessage", payload)
            time.sleep(0.4)

    @staticmethod
    def _split_message(text):
        if len(text) <= MAX_MESSAGE_LEN:
            return [text]
        chunks = []
        cur = ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > MAX_MESSAGE_LEN:
                if cur:
                    chunks.append(cur)
                cur = line
            else:
                cur += ("\n" if cur else "") + line
        if cur:
            chunks.append(cur)
        return chunks

    def _sender_loop(self):
        while not self._stop.is_set():
            try:
                text = self._send_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._send_now(text)
            except Exception as e:
                log.error(f"Mesaj gönderim hatası: {e}")
            time.sleep(0.5)

    # ==================================================================
    # ANLIK BİLDİRİMLER
    # ==================================================================
    def notify_bot_started(self, config_dict):
        bal = self.dm.get_balance() if self.dm else 0.0
        stake = self.tm.get_stake() if self.tm else 0.0
        lines = ["🚀 <b>BOT BAŞLADI</b>", ""]
        lines.append(f"💰 Bakiye: <b>{fmt_money(bal)} USDT</b>")
        lines.append(f"🎯 Stake: <b>{fmt_money(stake)} USDT</b>")
        lines.append("")
        lines.append("⚙️ <b>Ayarlar</b>")
        for k, v in config_dict.items():
            lines.append(f"• {k}: <code>{v}</code>")
        self._enqueue("\n".join(lines))

    def notify_bot_stopped(self):
        self._enqueue("🛑 <b>BOT DURDU</b>")

    def notify_trade_open(self, trade, hard_sl=None):
        side_emoji = "🟢" if trade.side == "LONG" else "🔴"
        thread_icon = THREAD_ICONS.get(trade.thread, "")
        lines = [f"{thread_icon} <b>İŞLEM AÇILDI</b> {side_emoji}", ""]
        lines.append(f"📊 {trade.symbol} | {trade.side} | {trade.thread}")
        lines.append(f"💵 Giriş: <code>{trade.entry_price}</code>")
        lines.append(f"📦 Qty: <code>{trade.qty}</code>")
        if trade.current_level:
            lines.append(f"📍 Seviye: <b>{trade.current_level}</b>")
        if hard_sl is not None:
            lines.append(f"🛡 Hard SL: <code>{hard_sl}</code>")
        # Ana thread'ler (Kırmızı/Beyaz) için LOSE+WINRATE göster
        if trade.thread in ("RED", "WHITE"):
            if trade.lose_line is not None:
                lines.append(f"❌ LOSE: <code>{trade.lose_line:.6f}</code>")
            if trade.winrate_line is not None:
                lines.append(f"✅ WINRATE: <code>{trade.winrate_line:.6f}</code>")
        self._enqueue("\n".join(lines))

    def notify_trade_close(self, trade):
        win = trade.pnl_usdt >= 0
        emoji = "✅" if win else "❌"
        thread_icon = THREAD_ICONS.get(trade.thread, "")
        lines = [f"{thread_icon} <b>İŞLEM KAPANDI</b> {emoji}", ""]
        lines.append(f"📊 {trade.symbol} | {trade.side} | {trade.thread}")
        lines.append(f"💵 Giriş: <code>{trade.entry_price}</code>")
        lines.append(f"💵 Çıkış: <code>{trade.close_price}</code>")
        lines.append(f"💰 PnL: <b>{fmt_money(trade.pnl_usdt)} USDT</b> ({fmt_pct(trade.pnl_pct)})")
        lines.append(f"⏱ Süre: {self._fmt_duration(trade.duration_sec())}")
        lines.append(f"🏷 {trade.exit_name}")
        self._enqueue("\n".join(lines))

    def notify_level_change(self, trade, new_level):
        thread_icon = THREAD_ICONS.get(trade.thread, "")
        text = (f"{thread_icon} <b>SEVİYE DEĞİŞTİ</b>\n"
                f"📊 {trade.symbol} | {trade.side} | {trade.thread}\n"
                f"📍 Yeni Seviye: <b>{new_level}</b>")
        self._enqueue(text)

    def notify_thread_ready(self, parent_trade, thread, side, level_lines):
        thread_icon = THREAD_ICONS.get(thread, "")
        thread_name = THREAD_NAMES.get(thread, thread)
        parent_icon = THREAD_ICONS.get(parent_trade.thread, "")
        lines = [f"{thread_icon} <b>{thread_name} TABLO HAZIR</b>", ""]
        lines.append(f"📊 {parent_trade.symbol} | {side} | parent: {parent_icon} {parent_trade.side}")
        lines.append("")
        for name, val in level_lines.items():
            try:
                lines.append(f"• {name}: <code>{float(val):.6f}</code>")
            except Exception:
                lines.append(f"• {name}: <code>{val}</code>")
        self._enqueue("\n".join(lines))

    def notify_insufficient_balance(self, symbol, side, thread, entry_price):
        text = (f"⚠️ <b>YETERSİZ BAKİYE</b>\n"
                f"📊 {symbol} | {side} | {thread}\n"
                f"💵 Giriş fiyatı: <code>{entry_price}</code>\n"
                f"İşlem açılamadı.")
        self._enqueue(text)

    def notify_slot_full(self, symbol, side, thread, msg):
        text = (f"⛔️ <b>SLOT DOLU</b>\n"
                f"📊 {symbol} | {side} | {thread}\n"
                f"{msg}")
        self._enqueue(text)

    def notify_error(self, title, symbol, module, detail):
        text = (f"🆘 <b>HATA</b>\n"
                f"📌 {title}\n"
                f"📊 Sembol: {symbol}\n"
                f"⚙️ Modül: {module}\n"
                f"<pre>{self._truncate(str(detail), 600)}</pre>")
        self._enqueue(text)
        if self.tm is not None:
            self.tm.errors_history.append({
                "ts": now_ts(), "title": title, "symbol": symbol,
                "module": module, "detail": str(detail),
            })

    def notify_stake_update(self, new_stake, bal):
        text = (f"💱 <b>STAKE GÜNCELLENDİ</b>\n"
                f"💰 Bakiye: <b>{fmt_money(bal)} USDT</b>\n"
                f"🎯 Yeni Stake: <b>{fmt_money(new_stake)} USDT</b>")
        self._enqueue(text)

    def notify_critical(self, title, detail=""):
        text = f"🚨 <b>KRİTİK</b>\n{title}"
        if detail:
            text += f"\n<pre>{self._truncate(str(detail), 600)}</pre>"
        self._enqueue(text)

    @staticmethod
    def _truncate(s, n):
        if len(s) <= n:
            return s
        return s[:n] + "..."

    @staticmethod
    def _fmt_duration(seconds):
        seconds = int(seconds)
        if seconds < 60:
            return f"{seconds}s"
        m, s = divmod(seconds, 60)
        if m < 60:
            return f"{m}dk {s}s"
        h, m = divmod(m, 60)
        if h < 24:
            return f"{h}sa {m}dk"
        d, h = divmod(h, 24)
        return f"{d}g {h}sa"

    # ==================================================================
    # AÇIK İŞLEMLER & FLAGLER BLOĞU (raporlarda)
    # ==================================================================
    def _build_open_trades_block(self):
        if self.tm is None:
            return "—"
        open_trades = self.tm.slots.get_all_open()
        if not open_trades:
            return "Açık işlem yok."

        # Thread'e göre sırala (sabit sıra)
        def sort_key(t):
            thr_idx = THREAD_ORDER.index(t.thread) if t.thread in THREAD_ORDER else 99
            return (thr_idx, t.symbol)

        lines = []
        for t in sorted(open_trades, key=sort_key):
            icon = THREAD_ICONS.get(t.thread, "")
            curr = self.dm.get_last_price(t.symbol) if self.dm else None
            if curr and t.entry_price:
                if t.side == "LONG":
                    pnl_raw = (curr - t.entry_price) / t.entry_price
                else:
                    pnl_raw = (t.entry_price - curr) / t.entry_price
                pnl_pct = pnl_raw * self.cfg.leverage * 100
                pnl_str = f"{fmt_pct(pnl_pct)}"
            else:
                pnl_str = "—"
            dur = self._fmt_duration(t.duration_sec())
            lvl = t.current_level or "—"
            lines.append(f"{icon} {t.symbol} {t.side} | {lvl} | {pnl_str} | {dur}")
        return "\n".join(lines)

    def _build_open_flags_block(self):
        """6 thread'in açık flag'lerini birleştir."""
        flags = []
        if self.ctrl:
            # KIRMIZI grubu
            for thread_attr in ("red_thread", "blue_thread", "yellow_thread",
                                "white_thread", "purple_thread", "orange_thread"):
                try:
                    th = getattr(self.ctrl, thread_attr, None)
                    if th:
                        flags += th.get_open_flags()
                except Exception:
                    pass

        if not flags:
            return "Açık flag yok."

        def sort_key(f):
            thr_idx = THREAD_ORDER.index(f["thread"]) if f["thread"] in THREAD_ORDER else 99
            return (thr_idx, f["symbol"])

        lines = []
        for f in sorted(flags, key=sort_key):
            icon = THREAD_ICONS.get(f["thread"], "")
            lines.append(f"{icon} {f['symbol']} {f['side']}")
        return "\n".join(lines)

    # ==================================================================
    # RAPORLAR
    # ==================================================================
    def _build_stats(self, trades):
        if not trades:
            return None
        total = len(trades)
        winners = [t for t in trades if t.pnl_usdt > 0]
        losers = [t for t in trades if t.pnl_usdt < 0]
        zero = [t for t in trades if t.pnl_usdt == 0]
        wins = sum(t.pnl_usdt for t in winners)
        losses = sum(t.pnl_usdt for t in losers)
        net = sum(t.pnl_usdt for t in trades)
        winrate = (len(winners) / total * 100) if total > 0 else 0
        pf = (wins / abs(losses)) if losses < 0 else None
        avg_dur = sum(t.duration_sec() for t in trades) / total if total > 0 else 0
        return {
            "total": total,
            "winners": len(winners),
            "losers": len(losers),
            "zero": len(zero),
            "winrate": winrate,
            "wins_total": wins,
            "losses_total": losses,
            "net": net,
            "profit_factor": pf,
            "avg_duration_sec": avg_dur,
        }

    def _build_counts_line(self, counts):
        """Açık işlem sayıları satırı (6 thread)."""
        parts = []
        for thr in THREAD_ORDER:
            icon = THREAD_ICONS[thr]
            parts.append(f"{icon} {counts.get(thr, 0)}")
        return " | ".join(parts)

    def send_hourly_report(self):
        now = now_ts()
        start = now - 3600

        bal = self.dm.get_balance() if self.dm else 0.0
        stake = self.tm.get_stake() if self.tm else 0.0
        counts = self.tm.slots.count_by_thread() if self.tm else {}
        paused = self.dm.get_paused_coins() if self.dm else []
        running = self.ctrl.is_running() if self.ctrl else False

        closed_1h = self.tm.get_closed_trades_window(start, now) if self.tm else []
        stats_1h = self._build_stats(closed_1h)

        lines = ["📊 <b>SAATLİK DURUM RAPORU</b>", ""]
        lines.append("━━━ <b>Anlık Durum</b> ━━━")
        lines.append(f"⚙️ Bot: {'🟢 Çalışıyor' if running else '🔴 Durmuş'}")
        lines.append(f"💰 Bakiye: <b>{fmt_money(bal)} USDT</b>")
        lines.append(f"🎯 Stake: <b>{fmt_money(stake)} USDT</b>")
        lines.append(f"📌 Açık: {self._build_counts_line(counts)}")
        if paused:
            lines.append(f"⏸ Duraklatılmış: {', '.join(paused)}")
        lines.append("")
        lines.append("━━━ <b>Açık İşlemler</b> ━━━")
        lines.append(self._build_open_trades_block())
        lines.append("")
        lines.append("━━━ <b>Açık Flagler</b> ━━━")
        lines.append(self._build_open_flags_block())
        lines.append("")
        lines.append("━━━ <b>Son 1 Saat</b> ━━━")
        if stats_1h:
            lines.append(f"📈 İşlem: {stats_1h['total']} | ✅ {stats_1h['winners']} | ❌ {stats_1h['losers']}")
            lines.append(f"💰 Net: <b>{fmt_money(stats_1h['net'])} USDT</b>")
            best = max(closed_1h, key=lambda t: t.pnl_usdt)
            worst = min(closed_1h, key=lambda t: t.pnl_usdt)
            lines.append(f"⭐️ En iyi: {best.symbol} {fmt_money(best.pnl_usdt)}")
            lines.append(f"🥶 En kötü: {worst.symbol} {fmt_money(worst.pnl_usdt)}")
        else:
            lines.append("Son 1 saatte kapanan işlem yok.")
        self._enqueue("\n".join(lines))

    def send_12h_report(self):
        now = now_ts()
        start = now - 12 * 3600
        closed = self.tm.get_closed_trades_window(start, now) if self.tm else []
        bal = self.dm.get_balance() if self.dm else 0.0

        lines = ["📊 <b>12 SAATLİK Z RAPORU</b>", ""]
        lines.append("━━━ <b>Anlık Durum</b> ━━━")
        counts = self.tm.slots.count_by_thread() if self.tm else {}
        lines.append(f"💰 Bakiye: <b>{fmt_money(bal)} USDT</b>")
        lines.append(f"📌 Açık: {self._build_counts_line(counts)}")
        lines.append("")
        lines.append("━━━ <b>Açık İşlemler</b> ━━━")
        lines.append(self._build_open_trades_block())
        lines.append("")
        lines.append("━━━ <b>Açık Flagler</b> ━━━")
        lines.append(self._build_open_flags_block())
        lines.append("")

        if not closed:
            lines.append("Son 12 saatte kapanan işlem yok.")
            self._enqueue("\n".join(lines))
            return

        stats = self._build_stats(closed)
        lines.append("━━━ <b>Performans (Son 12s)</b> ━━━")
        lines.append(f"📈 İşlem: <b>{stats['total']}</b>")
        lines.append(f"✅ Kazanan: {stats['winners']} | ❌ Kaybeden: {stats['losers']}")
        lines.append(f"🎯 Winrate: <b>{stats['winrate']:.1f}%</b>")
        if stats['profit_factor'] is not None:
            lines.append(f"💎 Profit Factor: <b>{stats['profit_factor']:.2f}</b>")
        lines.append(f"💰 Net: <b>{fmt_money(stats['net'])} USDT</b>")
        lines.append(f"📊 Kazançlar: {fmt_money(stats['wins_total'])} | Kayıplar: {fmt_money(stats['losses_total'])}")
        lines.append(f"⏱ Ort. Süre: {self._fmt_duration(stats['avg_duration_sec'])}")
        lines.append("")

        # Thread bazında (6 thread)
        lines.append("━━━ <b>Thread Kırılımı</b> ━━━")
        for thr in THREAD_ORDER:
            tt = [t for t in closed if t.thread == thr]
            if not tt:
                continue
            s = self._build_stats(tt)
            icon = THREAD_ICONS[thr]
            lines.append(f"{icon} {THREAD_NAMES[thr]}: {s['total']} işlem | WR {s['winrate']:.0f}% | "
                         f"Net {fmt_money(s['net'])}")
        lines.append("")

        # Çıkış tipleri
        lines.append("━━━ <b>Çıkış Tipleri</b> ━━━")
        exit_stats = defaultdict(lambda: {"count": 0, "pnl": 0.0})
        for t in closed:
            cat = self._exit_category(t.exit_name)
            exit_stats[cat]["count"] += 1
            exit_stats[cat]["pnl"] += t.pnl_usdt
        for cat, d in exit_stats.items():
            lines.append(f"• {cat}: {d['count']} | {fmt_money(d['pnl'])} USDT")
        lines.append("")

        # Yön
        longs = [t for t in closed if t.side == "LONG"]
        shorts = [t for t in closed if t.side == "SHORT"]
        lines.append("━━━ <b>Yön</b> ━━━")
        if longs:
            sl = self._build_stats(longs)
            lines.append(f"🟢 LONG: {sl['total']} | WR {sl['winrate']:.0f}% | Net {fmt_money(sl['net'])}")
        if shorts:
            ss = self._build_stats(shorts)
            lines.append(f"🔴 SHORT: {ss['total']} | WR {ss['winrate']:.0f}% | Net {fmt_money(ss['net'])}")
        lines.append("")

        # Coin top/bottom
        per_coin = defaultdict(lambda: {"pnl": 0.0, "count": 0})
        for t in closed:
            per_coin[t.symbol]["pnl"] += t.pnl_usdt
            per_coin[t.symbol]["count"] += 1
        coins_sorted = sorted(per_coin.items(), key=lambda kv: kv[1]["pnl"], reverse=True)
        lines.append("━━━ <b>En Kârlı 3 Coin</b> ━━━")
        for sym, d in coins_sorted[:3]:
            lines.append(f"⭐️ {sym}: {fmt_money(d['pnl'])} ({d['count']} işlem)")
        lines.append("━━━ <b>En Zararlı 3 Coin</b> ━━━")
        for sym, d in coins_sorted[-3:][::-1]:
            if d["pnl"] >= 0:
                continue
            lines.append(f"🥶 {sym}: {fmt_money(d['pnl'])} ({d['count']} işlem)")

        self._enqueue("\n".join(lines))

    def send_24h_report(self):
        now = now_ts()
        start = now - 24 * 3600
        closed = self.tm.get_closed_trades_window(start, now) if self.tm else []
        flag_events = self.tm.get_flag_events_window(start, now) if self.tm else []
        errors = self.tm.get_errors_window(start, now) if self.tm else []

        lines = ["📊 <b>24 SAATLİK X RAPORU</b>", ""]
        bal = self.dm.get_balance() if self.dm else 0.0
        lines.append(f"💰 Bakiye: <b>{fmt_money(bal)} USDT</b>")
        lines.append("")

        if not closed:
            lines.append("Son 24 saatte kapanan işlem yok.")
            self._enqueue("\n".join(lines))
            return

        stats = self._build_stats(closed)
        lines.append("━━━ <b>Genel Performans</b> ━━━")
        lines.append(f"📈 İşlem: <b>{stats['total']}</b>")
        lines.append(f"✅ {stats['winners']} | ❌ {stats['losers']} | ⚖️ {stats['zero']}")
        lines.append(f"🎯 Winrate: <b>{stats['winrate']:.1f}%</b>")
        if stats['profit_factor'] is not None:
            lines.append(f"💎 Profit Factor: <b>{stats['profit_factor']:.2f}</b>")
        lines.append(f"💰 Net: <b>{fmt_money(stats['net'])} USDT</b>")
        lines.append("")

        # Streak
        sorted_closed = sorted(closed, key=lambda t: t.close_ts or 0)
        max_win_streak = 0
        max_loss_streak = 0
        cur_win = 0
        cur_loss = 0
        for t in sorted_closed:
            if t.pnl_usdt > 0:
                cur_win += 1
                cur_loss = 0
                if cur_win > max_win_streak:
                    max_win_streak = cur_win
            elif t.pnl_usdt < 0:
                cur_loss += 1
                cur_win = 0
                if cur_loss > max_loss_streak:
                    max_loss_streak = cur_loss
        lines.append(f"🔥 En uzun kazanma serisi: <b>{max_win_streak}</b>")
        lines.append(f"❄️ En uzun kaybetme serisi: <b>{max_loss_streak}</b>")
        lines.append("")

        # Saatlik dağılım
        hourly = defaultdict(lambda: {"count": 0, "pnl": 0.0})
        for t in sorted_closed:
            dt = datetime.fromtimestamp(t.close_ts, tz=timezone.utc)
            hourly[dt.hour]["count"] += 1
            hourly[dt.hour]["pnl"] += t.pnl_usdt
        if hourly:
            most_active = max(hourly.items(), key=lambda kv: kv[1]["count"])
            best_hour = max(hourly.items(), key=lambda kv: kv[1]["pnl"])
            worst_hour = min(hourly.items(), key=lambda kv: kv[1]["pnl"])
            lines.append("━━━ <b>Saat Analizi (UTC)</b> ━━━")
            lines.append(f"⚡ En aktif saat: <b>{most_active[0]:02d}:00</b> ({most_active[1]['count']} işlem)")
            lines.append(f"⭐️ En kârlı saat: <b>{best_hour[0]:02d}:00</b> ({fmt_money(best_hour[1]['pnl'])})")
            lines.append(f"🥶 En zararlı saat: <b>{worst_hour[0]:02d}:00</b> ({fmt_money(worst_hour[1]['pnl'])})")
            lines.append("")

        # Thread bazında (6 thread)
        lines.append("━━━ <b>Thread Detayı</b> ━━━")
        for thr in THREAD_ORDER:
            tt = [t for t in closed if t.thread == thr]
            if not tt:
                continue
            s = self._build_stats(tt)
            icon = THREAD_ICONS[thr]
            lines.append(f"{icon} {THREAD_NAMES[thr]}: {s['total']} | WR {s['winrate']:.0f}% | Net {fmt_money(s['net'])}")
        lines.append("")

        # Coin detayı
        per_coin = defaultdict(list)
        for t in closed:
            per_coin[t.symbol].append(t)
        coins_with_stats = []
        for sym, tt in per_coin.items():
            s = self._build_stats(tt)
            coins_with_stats.append((sym, s, tt))
        coins_with_stats.sort(key=lambda x: x[1]["net"], reverse=True)
        lines.append("━━━ <b>Coin Detayı</b> ━━━")
        for sym, s, tt in coins_with_stats[:8]:
            best = max(tt, key=lambda t: t.pnl_usdt)
            worst = min(tt, key=lambda t: t.pnl_usdt)
            lines.append(f"• <b>{sym}</b>: {s['total']} | WR {s['winrate']:.0f}% | "
                         f"Net {fmt_money(s['net'])}")
            lines.append(f"  ⭐️ {fmt_money(best.pnl_usdt)} / 🥶 {fmt_money(worst.pnl_usdt)}")
        lines.append("")

        # Çıkış tipleri
        exit_stats = defaultdict(lambda: {"count": 0, "pnl": 0.0})
        for t in closed:
            cat = self._exit_category(t.exit_name)
            exit_stats[cat]["count"] += 1
            exit_stats[cat]["pnl"] += t.pnl_usdt
        lines.append("━━━ <b>Çıkış Tipleri</b> ━━━")
        for cat, d in exit_stats.items():
            lines.append(f"• {cat}: {d['count']} | {fmt_money(d['pnl'])}")
        lines.append("")

        # Flag istatistikleri (6 thread)
        flag_opened = sum(1 for e in flag_events if e["event"] == "OPENED")
        flag_converted = sum(1 for e in flag_events if e["event"] == "CONVERTED")
        flag_deleted = sum(1 for e in flag_events if e["event"] == "DELETED")
        lines.append("━━━ <b>Flag İstatistikleri</b> ━━━")
        lines.append(f"🚩 Açılan: {flag_opened} | 🎯 İşleme dönüşen: {flag_converted} | 🗑 Silinen: {flag_deleted}")
        if flag_opened > 0:
            conv_rate = (flag_converted / flag_opened) * 100
            lines.append(f"📊 Konversiyon: <b>{conv_rate:.1f}%</b>")
        # Thread başına flag konversiyon (6 thread)
        for thr in THREAD_ORDER:
            t_events = [e for e in flag_events if e["thread"] == thr]
            if not t_events:
                continue
            t_open = sum(1 for e in t_events if e["event"] == "OPENED")
            t_conv = sum(1 for e in t_events if e["event"] == "CONVERTED")
            t_del = sum(1 for e in t_events if e["event"] == "DELETED")
            icon = THREAD_ICONS[thr]
            lines.append(f"  {icon} {THREAD_NAMES[thr]}: 🚩{t_open} 🎯{t_conv} 🗑{t_del}")
        lines.append("")

        # Chandelier özel (Sarı + Turuncu)
        chandelier_exits = [t for t in closed if t.exit_name and "CHANDELIER" in t.exit_name]
        if chandelier_exits:
            cs = self._build_stats(chandelier_exits)
            lines.append("━━━ <b>Chandelier (Sarı + Turuncu)</b> ━━━")
            lines.append(f"🕯 Chandelier çıkışları: {cs['total']} | Net {fmt_money(cs['net'])}")
            lines.append("")

        # Uyarılar
        if errors:
            lines.append("━━━ <b>Uyarılar (24s)</b> ━━━")
            lines.append(f"⚠️ Toplam hata: {len(errors)}")
            counters = self.tm.get_counters() if self.tm else {}
            if counters:
                lines.append(f"💸 Yetersiz bakiye: {counters.get('insufficient_balance', 0)}")
                lines.append(f"⛔️ Slot dolu: {counters.get('slot_full', 0)}")

        self._enqueue("\n".join(lines))

    @staticmethod
    def _exit_category(exit_name):
        if not exit_name:
            return "UNKNOWN"
        if "WINRATE" in exit_name:
            return "WINRATE"
        if "LOSE" in exit_name:
            return "LOSE"
        if "CHANDELIER" in exit_name:
            return "CHANDELIER"
        if "KIRMIZI KAPANDI" in exit_name or "BEYAZ KAPANDI" in exit_name:
            return "BAĞIMLI"
        if "KIRMIZI GİRİŞ" in exit_name or "BEYAZ GİRİŞ" in exit_name:
            return "GİRİŞ EXIT"
        return "DİĞER"

    # ==================================================================
    # KOMUTLAR
    # ==================================================================
    def _handle_command(self, text, chat_id):
        if str(chat_id) != str(self.chat_id):
            return

        text = text.strip()
        cmd = text.split()[0].lower() if text else ""
        args = text.split()[1:] if len(text.split()) > 1 else []

        if cmd == "/start":
            if self.ctrl:
                if self.ctrl.is_running():
                    self._enqueue("Bot zaten çalışıyor.")
                else:
                    self.ctrl.start_trading()
                    self._enqueue("🟢 Trading başlatıldı.")
        elif cmd == "/stop":
            if self.ctrl:
                self.ctrl.stop_trading()
                self._enqueue("🔴 Trading durduruldu.")
        elif cmd == "/status":
            self._send_status()
        elif cmd == "/report":
            self.send_hourly_report()
        elif cmd == "/pause":
            if not args:
                self._enqueue("Kullanım: /pause SEMBOL")
            else:
                sym = args[0].upper()
                if self.dm and sym in self.cfg.symbols:
                    self.dm.pause_coin(sym)
                    self._enqueue(f"⏸ {sym} duraklatıldı.")
                else:
                    self._enqueue(f"Bilinmeyen sembol: {sym}")
        elif cmd == "/resume":
            if not args:
                self._enqueue("Kullanım: /resume SEMBOL")
            else:
                sym = args[0].upper()
                if self.dm:
                    self.dm.resume_coin(sym)
                    self._enqueue(f"▶️ {sym} devam.")
        elif cmd == "/help":
            self._enqueue(
                "📖 <b>Komutlar</b>\n"
                "/start — Trading başlat\n"
                "/stop — Trading durdur\n"
                "/status — Anlık durum\n"
                "/report — Hourly raporu zorla\n"
                "/pause SEMBOL — Coin duraklat\n"
                "/resume SEMBOL — Coin devam\n"
                "/help — Bu liste"
            )

    def _send_status(self):
        bal = self.dm.get_balance() if self.dm else 0.0
        stake = self.tm.get_stake() if self.tm else 0.0
        running = self.ctrl.is_running() if self.ctrl else False
        counts = self.tm.slots.count_by_thread() if self.tm else {}
        paused = self.dm.get_paused_coins() if self.dm else []

        lines = ["📊 <b>DURUM</b>", ""]
        lines.append(f"⚙️ Bot: {'🟢 Çalışıyor' if running else '🔴 Durmuş'}")
        lines.append(f"💰 Bakiye: <b>{fmt_money(bal)} USDT</b>")
        lines.append(f"🎯 Stake: <b>{fmt_money(stake)} USDT</b>")
        lines.append(f"📌 Açık: {self._build_counts_line(counts)}")
        if paused:
            lines.append(f"⏸ Duraklatılmış: {', '.join(paused)}")
        lines.append("")
        lines.append("━━━ <b>Açık İşlemler</b> ━━━")
        lines.append(self._build_open_trades_block())
        lines.append("")
        lines.append("━━━ <b>Açık Flagler</b> ━━━")
        lines.append(self._build_open_flags_block())
        self._enqueue("\n".join(lines))

    # ==================================================================
    # POLLING
    # ==================================================================
    def _poll_commands(self):
        params = {
            "offset": self._last_update_id + 1,
            "timeout": 25,
            "allowed_updates": ["message"],
        }
        result = _tg_request(self.token, "getUpdates", params, timeout=35)
        if not result.get("ok"):
            return
        updates = result.get("result", [])
        for u in updates:
            self._last_update_id = max(self._last_update_id, u.get("update_id", 0))
            msg = u.get("message")
            if not msg:
                continue
            text = msg.get("text", "")
            chat = msg.get("chat", {})
            if text:
                try:
                    self._handle_command(text, chat.get("id"))
                except Exception as e:
                    log.exception(f"Komut işleme hatası: {e}")

    # ==================================================================
    # RAPOR ZAMANLAYICI
    # ==================================================================
    def _check_reports(self):
        now = utc_now()
        hour_key = now.strftime("%Y-%m-%d-%H")
        day_key = now.strftime("%Y-%m-%d")
        twelve_key = f"{day_key}-{'AM' if now.hour < 12 else 'PM'}"

        if now.minute < 2 and self._last_hourly_key != hour_key:
            self._last_hourly_key = hour_key
            try:
                self.send_hourly_report()
            except Exception as e:
                log.exception(f"Hourly rapor hatası: {e}")

        if now.minute < 2 and now.hour in (0, 12) and self._last_12h_key != twelve_key:
            self._last_12h_key = twelve_key
            try:
                self.send_12h_report()
            except Exception as e:
                log.exception(f"12h rapor hatası: {e}")

        if now.minute < 2 and now.hour == 0 and self._last_24h_key != day_key:
            self._last_24h_key = day_key
            try:
                self.send_24h_report()
            except Exception as e:
                log.exception(f"24h rapor hatası: {e}")

    # ==================================================================
    # RUN
    # ==================================================================
    def run(self):
        log.info("Telegram thread başladı.")
        self._sender_thread = threading.Thread(
            target=self._sender_loop, name="TgSender", daemon=True)
        self._sender_thread.start()

        while not self._stop.is_set():
            try:
                self._poll_commands()
            except Exception as e:
                log.exception(f"Polling hatası: {e}")
                time.sleep(2)
            try:
                self._check_reports()
            except Exception as e:
                log.exception(f"Rapor check hatası: {e}")
            time.sleep(1)
        log.info("Telegram thread durdu.")
