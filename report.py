"""
SMARTBOT REDBLUE — report.py
Periyodik raporlar: 10 dakika, 1 saat, 8 saat, 24 saat.
"""

import threading
from datetime import datetime, timedelta
from collections import Counter

from state import state
from telegram_notifier import notifier, fmt_time, fmt_color_side, fmt_duration, fmt_pnl
from bybit_client import BybitClient


class ReportThread(threading.Thread):
    def __init__(self, config: dict, bybit: BybitClient, stop_event: threading.Event):
        super().__init__(daemon=True, name="Report_Thread")
        self.config = config
        self.bybit = bybit
        self.stop_event = stop_event
        self.max_slots = config["slot"]["max_open_positions"]

        report = config["report"]
        self.short_interval = report["short_interval_minutes"] * 60
        self.medium_interval = report["medium_interval_hours"] * 3600
        self.shift_interval = report["shift_interval_hours"] * 3600
        self.daily_interval = report["daily_interval_hours"] * 3600

        # Son rapor zamanlari
        now = datetime.now()
        self.last_short = now
        self.last_medium = now
        self.last_shift = now
        self.last_daily = now
        self.day_start = now  # gunluk toplam icin

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.check_reports()
            except Exception as e:
                print(f"[REPORT HATA] {e}")
            # Her 30 saniyede kontrol et
            self.stop_event.wait(30)

    def check_reports(self):
        now = datetime.now()
        if (now - self.last_short).total_seconds() >= self.short_interval:
            notifier.send(self.build_short_report())
            self.last_short = now
        if (now - self.last_medium).total_seconds() >= self.medium_interval:
            notifier.send(self.build_medium_report())
            self.last_medium = now
        if (now - self.last_shift).total_seconds() >= self.shift_interval:
            notifier.send(self.build_shift_report())
            self.last_shift = now
        if (now - self.last_daily).total_seconds() >= self.daily_interval:
            notifier.send(self.build_daily_report())
            self.last_daily = now

    # ──────────────────────────────────────────
    # YARDIMCI
    # ──────────────────────────────────────────

    def build_open_positions_block(self) -> str:
        positions = state.get_all_positions()
        if not positions:
            return "📈 AÇIK POZİSYONLAR\n━━━━━━━━━━━━━━━━━━━━\n(yok)"
        lines = ["📈 AÇIK POZİSYONLAR", "━━━━━━━━━━━━━━━━━━━━"]
        for pos in positions:
            try:
                price = self.bybit.get_price(pos["coin"])
                gross, _, price_diff = self.calculate_unrealized_pnl(pos, price)
            except Exception:
                gross = 0.0
                price_diff = 0.0
            volume = pos["volume"]
            atr = pos["atr_at_entry"]
            lines.append(f"📌 {pos['coin']}")
            lines.append(f"├ {fmt_color_side(pos['color'], pos['side'])}")
            lines.append(f"├ Seviye: {pos['level']}")
            lines.append(f"└ K/Z: {fmt_pnl(gross, volume, price_diff, atr)}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def calculate_unrealized_pnl(self, pos: dict, price: float):
        """Donus: (gross_pnl, net_pnl, price_diff)"""
        entry = pos["entry_price"]
        qty = pos["qty"]
        if pos["side"] == "long":
            price_diff = price - entry
        else:
            price_diff = entry - price
        gross = price_diff * qty
        commission = pos["volume"] * self.config["commission"]["rate"]
        net = gross - commission
        return gross, net, price_diff

    def build_closed_positions_block(self, since: datetime, label: str) -> str:
        trades = state.get_closed_trades_since(since)
        if not trades:
            return f"📉 KAPANAN POZİSYONLAR ({label})\n━━━━━━━━━━━━━━━━━━━━\n(yok)"
        lines = [f"📉 KAPANAN POZİSYONLAR ({label})", "━━━━━━━━━━━━━━━━━━━━"]
        for t in trades:
            volume = t["volume"]
            atr = t["atr_at_entry"]
            if t["side"] == "long":
                price_diff = t["exit_price"] - t["entry_price"]
            else:
                price_diff = t["entry_price"] - t["exit_price"]
            lines.append(f"📌 {t['coin']}")
            lines.append(f"├ {fmt_color_side(t['color'], t['side'])}")
            lines.append(f"├ Çıkış: {t['exit_type']}")
            lines.append(f"└ Net K/Z: {fmt_pnl(t['net_pnl'], volume, price_diff, atr)}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def build_flag_status_block(self) -> str:
        flags = state.get_all_open_flags()
        total = state.get_total_open_flags()
        kirmizi_list = ", ".join(flags["kirmizi"]) if flags["kirmizi"] else "(yok)"
        mavi_list = ", ".join(flags["mavi"]) if flags["mavi"] else "(yok)"
        return f"""🚦 FLAG DURUMU
├ Toplam Açık Flag: {total}
├ 🔴 Kırmızı: {kirmizi_list}
└ 🔵 Mavi: {mavi_list}"""

    def build_flag_stats_block(self, since: datetime, label: str) -> str:
        events = state.get_flag_events_since(since)
        opened = sum(1 for e in events if e["event"] == "opened")
        closed = sum(1 for e in events if e["event"] == "closed")
        to_trade = sum(1 for e in events if e["event"] == "to_trade")
        return f"""📊 FLAG İSTATİSTİKLERİ ({label})
├ Açılan Flag: {opened}
├ Silinen Flag: {closed}
└ İşleme Dönüşen: {to_trade}"""

    def build_period_summary(self, since: datetime, label: str, total_stake_base: float = None) -> str:
        opened_trades = state.get_opened_trades_since(since)
        closed_trades = state.get_closed_trades_since(since)
        won = sum(1 for t in closed_trades if t["net_pnl"] > 0)
        lost = sum(1 for t in closed_trades if t["net_pnl"] <= 0)
        total_pnl = sum(t["net_pnl"] for t in closed_trades)
        total_comm = sum(t["commission"] for t in closed_trades)

        # % bazli: toplam pnl / toplam islem hacmi
        total_volume = sum(t["volume"] for t in closed_trades)
        pct = (total_pnl / total_volume * 100) if total_volume > 0 else 0

        # ATR bazli: her islemin price_diff/atr toplami
        atr_total = 0.0
        for t in closed_trades:
            if t["side"] == "long":
                pd = t["exit_price"] - t["entry_price"]
            else:
                pd = t["entry_price"] - t["exit_price"]
            if t["atr_at_entry"] > 0:
                atr_total += pd / t["atr_at_entry"]

        sign = "+" if total_pnl >= 0 else ""
        pnl_str = f"{sign}{total_pnl:.2f} USDT / %{pct:.2f} / {atr_total:.2f} ATR"

        # Islem dagilimi
        red_long = sum(1 for t in closed_trades if t["color"] == "kirmizi" and t["side"] == "long")
        red_short = sum(1 for t in closed_trades if t["color"] == "kirmizi" and t["side"] == "short")
        blue_long = sum(1 for t in closed_trades if t["color"] == "mavi" and t["side"] == "long")
        blue_short = sum(1 for t in closed_trades if t["color"] == "mavi" and t["side"] == "short")

        lines = [
            f"💰 PERIYOT SONUCU ({label})",
            f"├ Açılan İşlem: {len(opened_trades)}",
            f"├ Kapanan İşlem: {len(closed_trades)}",
            f"├ Kazanan: {won} / Kaybeden: {lost}",
        ]
        if len(closed_trades) > 0:
            wr = won / len(closed_trades) * 100
            lines.append(f"├ Winrate: %{wr:.1f}")
            lines.append(f"├ Toplam Komisyon: -{total_comm:.2f} USDT")
        lines.append(f"├ Net K/Z: {pnl_str}")
        lines.append("│")
        lines.append("├ 📊 İŞLEM DAĞILIMI")
        lines.append(f"├ 🔴 Kırmızı Toplam: {red_long + red_short}")
        lines.append(f"│   ├ Long: {red_long}")
        lines.append(f"│   └ Short: {red_short}")
        lines.append(f"└ 🔵 Mavi Toplam: {blue_long + blue_short}")
        lines.append(f"    ├ Long: {blue_long}")
        lines.append(f"    └ Short: {blue_short}")
        return "\n".join(lines)

    def build_total_summary(self, since: datetime, label: str) -> str:
        """Bugun veya baslangictan beri toplam."""
        opened = state.get_opened_trades_since(since)
        closed = state.get_closed_trades_since(since)
        won = sum(1 for t in closed if t["net_pnl"] > 0)
        lost = sum(1 for t in closed if t["net_pnl"] <= 0)
        total_pnl = sum(t["net_pnl"] for t in closed)

        total_volume = sum(t["volume"] for t in closed)
        pct = (total_pnl / total_volume * 100) if total_volume > 0 else 0

        atr_total = 0.0
        for t in closed:
            if t["side"] == "long":
                pd = t["exit_price"] - t["entry_price"]
            else:
                pd = t["entry_price"] - t["exit_price"]
            if t["atr_at_entry"] > 0:
                atr_total += pd / t["atr_at_entry"]

        sign = "+" if total_pnl >= 0 else ""
        pnl_str = f"{sign}{total_pnl:.2f} USDT / %{pct:.2f} / {atr_total:.2f} ATR"

        red_long = sum(1 for t in closed if t["color"] == "kirmizi" and t["side"] == "long")
        red_short = sum(1 for t in closed if t["color"] == "kirmizi" and t["side"] == "short")
        blue_long = sum(1 for t in closed if t["color"] == "mavi" and t["side"] == "long")
        blue_short = sum(1 for t in closed if t["color"] == "mavi" and t["side"] == "short")

        lines = [
            f"💰 GENEL TOPLAM ({label})",
            f"├ Açılan İşlem: {len(opened)}",
            f"├ Kapanan İşlem: {len(closed)}",
            f"├ Kazanan: {won} / Kaybeden: {lost}",
        ]
        if len(closed) > 0:
            wr = won / len(closed) * 100
            lines.append(f"├ Winrate: %{wr:.1f}")
        lines.append(f"├ Net K/Z: {pnl_str}")
        lines.append("│")
        lines.append("├ 📊 İŞLEM DAĞILIMI")
        lines.append(f"├ 🔴 Kırmızı Toplam: {red_long + red_short}")
        lines.append(f"│   ├ Long: {red_long}")
        lines.append(f"│   └ Short: {red_short}")
        lines.append(f"└ 🔵 Mavi Toplam: {blue_long + blue_short}")
        lines.append(f"    ├ Long: {blue_long}")
        lines.append(f"    └ Short: {blue_short}")
        return "\n".join(lines)

    # ──────────────────────────────────────────
    # KISA RAPORLAR (10dk, 1sa) — temel
    # ──────────────────────────────────────────

    def build_short_report(self) -> str:
        since = datetime.now() - timedelta(seconds=self.short_interval)
        return self._build_basic_report("📊 DURUM RAPORU — 10 DAKİKA", "Son 10 dk", since)

    def build_medium_report(self) -> str:
        since = datetime.now() - timedelta(seconds=self.medium_interval)
        return self._build_basic_report("📊 DURUM RAPORU — 1 SAAT", "Son 1 saat", since)

    def _build_basic_report(self, title: str, period_label: str, since: datetime) -> str:
        lines = [
            title,
            "━━━━━━━━━━━━━━━━━━━━",
            f"🕐 Zaman: {fmt_time()}",
            "",
            f"🎰 SLOTLAR: {state.get_open_count()}/{self.max_slots}",
            "",
            self.build_open_positions_block(),
            "",
            self.build_closed_positions_block(since, period_label),
            "━━━━━━━━━━━━━━━━━━━━",
            self.build_flag_status_block(),
            "",
            self.build_flag_stats_block(since, period_label),
            "",
            self.build_period_summary(since, period_label),
            "",
            self.build_total_summary(self.day_start, "Bugün"),
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        return "\n".join(lines)

    # ──────────────────────────────────────────
    # VARDIYA RAPORU (8sa) — detayli
    # ──────────────────────────────────────────

    def build_shift_report(self) -> str:
        since = datetime.now() - timedelta(seconds=self.shift_interval)
        return self._build_detailed_report("📊 VARDİYA RAPORU — 8 SAAT", "Son 8 saat", since)

    # ──────────────────────────────────────────
    # GUN SONU RAPORU (24sa) — en detayli
    # ──────────────────────────────────────────

    def build_daily_report(self) -> str:
        since = datetime.now() - timedelta(seconds=self.daily_interval)
        return self._build_detailed_report("📊 GÜN SONU RAPORU — 24 SAAT", "Son 24 saat", since, with_best_worst=True)

    def _build_detailed_report(self, title: str, period_label: str, since: datetime, with_best_worst: bool = False) -> str:
        closed = state.get_closed_trades_since(since)

        # Cikis tipi dagilimi
        exit_counter = Counter(t["exit_type"] for t in closed)
        exit_block_lines = ["📊 ÇIKIŞ TİPİ DAĞILIMI"]
        all_exits = ["ENTRY EXIT", "BE EXIT", "CE1 EXIT", "CE2 EXIT", "CHANDELIER EXIT", "WINRATE EXIT", "MANUEL EXIT", "SL EXIT", "TASFIYE"]
        for et in all_exits:
            if exit_counter.get(et, 0) > 0:
                exit_block_lines.append(f"├ {et}: {exit_counter[et]}")
        # son satir
        if len(exit_block_lines) > 1:
            exit_block_lines[-1] = exit_block_lines[-1].replace("├", "└", 1)
        exit_block = "\n".join(exit_block_lines) if len(exit_block_lines) > 1 else "📊 ÇIKIŞ TİPİ DAĞILIMI\n(yok)"

        # En cok islem goren
        coin_counter = Counter(t["coin"] for t in closed)
        top_traded = coin_counter.most_common(3)
        top_traded_block = ["🏆 EN ÇOK İŞLEM GÖREN"]
        for i, (coin, cnt) in enumerate(top_traded, 1):
            prefix = "├" if i < len(top_traded) else "└"
            top_traded_block.append(f"{prefix} {i}. {coin} — {cnt} işlem")
        if len(top_traded_block) == 1:
            top_traded_block.append("(yok)")

        # En cok kazandiran / kaybettiren
        coin_pnl = {}
        for t in closed:
            coin_pnl[t["coin"]] = coin_pnl.get(t["coin"], 0) + t["net_pnl"]
        winners = sorted([(c, p) for c, p in coin_pnl.items() if p > 0], key=lambda x: -x[1])[:3]
        losers = sorted([(c, p) for c, p in coin_pnl.items() if p < 0], key=lambda x: x[1])[:3]

        winners_block = ["💰 EN ÇOK KAZANDIRAN"]
        for i, (coin, pnl) in enumerate(winners, 1):
            prefix = "├" if i < len(winners) else "└"
            winners_block.append(f"{prefix} {i}. {coin} — +{pnl:.2f} USDT")
        if len(winners_block) == 1:
            winners_block.append("(yok)")

        losers_block = ["📉 EN ÇOK KAYBETTİREN"]
        for i, (coin, pnl) in enumerate(losers, 1):
            prefix = "├" if i < len(losers) else "└"
            losers_block.append(f"{prefix} {i}. {coin} — {pnl:.2f} USDT")
        if len(losers_block) == 1:
            losers_block.append("(yok)")

        lines = [
            title,
            "━━━━━━━━━━━━━━━━━━━━",
            f"🕐 Zaman: {fmt_time()}",
            f"⏱ Periyot: {fmt_time(since)} → {fmt_time()}",
            "",
            f"🎰 SLOTLAR: {state.get_open_count()}/{self.max_slots}",
            "",
            self.build_open_positions_block(),
            "",
            self.build_closed_positions_block(since, period_label),
            "━━━━━━━━━━━━━━━━━━━━",
            self.build_flag_status_block(),
            "",
            self.build_flag_stats_block(since, period_label),
            "",
            exit_block,
            "",
            "\n".join(top_traded_block),
            "",
            "\n".join(winners_block),
            "",
            "\n".join(losers_block),
            "",
        ]

        if with_best_worst:
            best = state.best_trade
            worst = state.worst_trade
            if best:
                duration = (best["exit_time"] - best["entry_time"]).total_seconds()
                volume = best["volume"]
                atr = best["atr_at_entry"]
                if best["side"] == "long":
                    pd = best["exit_price"] - best["entry_price"]
                else:
                    pd = best["entry_price"] - best["exit_price"]
                lines.append("🏅 EN İYİ İŞLEM")
                lines.append(f"├ Coin: {best['coin']} — {fmt_color_side(best['color'], best['side'])}")
                lines.append(f"├ Çıkış: {best['exit_type']}")
                lines.append(f"├ Süre: {fmt_duration(duration)}")
                lines.append(f"└ Net K/Z: {fmt_pnl(best['net_pnl'], volume, pd, atr)}")
                lines.append("")
            if worst:
                duration = (worst["exit_time"] - worst["entry_time"]).total_seconds()
                volume = worst["volume"]
                atr = worst["atr_at_entry"]
                if worst["side"] == "long":
                    pd = worst["exit_price"] - worst["entry_price"]
                else:
                    pd = worst["entry_price"] - worst["exit_price"]
                lines.append("📉 EN KÖTÜ İŞLEM")
                lines.append(f"├ Coin: {worst['coin']} — {fmt_color_side(worst['color'], worst['side'])}")
                lines.append(f"├ Çıkış: {worst['exit_type']}")
                lines.append(f"├ Süre: {fmt_duration(duration)}")
                lines.append(f"└ Net K/Z: {fmt_pnl(worst['net_pnl'], volume, pd, atr)}")
                lines.append("")

            # Ortalama islem suresi
            if closed:
                avg_duration = sum((t["exit_time"] - t["entry_time"]).total_seconds() for t in closed) / len(closed)
                lines.append(f"⏱ ORTALAMA İŞLEM SÜRESİ: {fmt_duration(avg_duration)}")
                lines.append("")

        lines.append(self.build_period_summary(since, period_label))
        lines.append("")
        # Genel toplam — gun sonu icin baslangictan beri, vardiya icin bugun
        if with_best_worst:
            lines.append(self.build_total_summary(state.start_time or self.day_start, "Başlangıçtan Bugüne"))
        else:
            lines.append(self.build_total_summary(self.day_start, "Bugün"))
        lines.append("━━━━━━━━━━━━━━━━━━━━")

        return "\n".join(lines)
