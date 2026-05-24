"""
SMARTBOT REDBLUE — exit.py
Kirmizi ve Mavi cikis threadleri.
Her 5 saniyede acik pozisyonlari tarar:
  - Seviye gecislerini kontrol eder (ENTRY -> BE -> CE1 -> CE2)
  - Cikis kosullarini kontrol eder
  - Dis kaynakli kapanislari tespit eder
"""

import threading
from datetime import datetime

from state import state
from band import calculate_bands
from bybit_client import BybitClient
from telegram_notifier import (
    notifier, msg_trade_closed, msg_level_transition,
    msg_external_close, msg_error,
)


class ExitThread(threading.Thread):
    """
    color: "kirmizi" veya "mavi"
    """

    def __init__(self, color: str, config: dict, bybit: BybitClient, stop_event: threading.Event):
        super().__init__(daemon=True, name=f"{color.capitalize()}_Exit")
        self.color = color
        self.config = config
        self.bybit = bybit
        self.stop_event = stop_event
        self.scan_interval = config["scan"]["interval_seconds"]
        self.timeframe = config["band"]["timeframe"]
        self.max_slots = config["slot"]["max_open_positions"]
        self.commission_rate = config["commission"]["rate"]
        self.chandelier_mult = config["band"]["chandelier_multiplier"]

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.scan_positions()
            except Exception as e:
                notifier.send(msg_error(self.name, "-", "Genel Cikis Tarama Hatasi", str(e)))
            self.stop_event.wait(self.scan_interval)

    def scan_positions(self):
        positions = state.get_all_positions()
        for pos in positions:
            if self.stop_event.is_set():
                return
            if pos["color"] != self.color:
                continue
            try:
                self.handle_position(pos)
            except Exception as e:
                notifier.send(msg_error(self.name, pos["coin"], "Pozisyon Tarama Hatasi", str(e)))

    def handle_position(self, pos: dict):
        coin = pos["coin"]

        # 1. Bybit'te pozisyon hala acik mi? (manuel/SL kapanisi tespiti)
        # Grace period: yeni acilan pozisyonlarda 30 saniye boyunca external close kontrolu yapma
        # (Bybit API gecikmesi olabilir)
        age_seconds = (datetime.now() - pos["entry_time"]).total_seconds()
        if age_seconds < 30:
            check_external = False
        else:
            check_external = True

        if check_external:
            bybit_pos = self.bybit.get_position(coin)
            if bybit_pos is None:
                self.handle_external_close(pos)
                return

        # 2. Guncel fiyat ve bantlar
        price = self.bybit.get_price(coin)
        klines = self.bybit.get_klines(coin, self.timeframe, limit=200)
        bands = calculate_bands(klines, self.config)

        # 3. En yuksek/dusuk fiyat takibi (chandelier icin)
        updates = {}
        if price > pos["highest_price"]:
            updates["highest_price"] = price
        if price < pos["lowest_price"]:
            updates["lowest_price"] = price
        if updates:
            state.update_position(coin, updates)
            pos.update(updates)

        # 4. Seviye gecisleri ve cikis kontrolu
        if self.color == "kirmizi":
            self.handle_kirmizi_exit(pos, price, bands)
        else:
            self.handle_mavi_exit(pos, price, bands)

    # ──────────────────────────────────────────
    # KIRMIZI CIKIS
    # ──────────────────────────────────────────

    def handle_kirmizi_exit(self, pos: dict, price: float, bands: dict):
        coin = pos["coin"]
        side = pos["side"]
        level = pos["level"]

        if side == "long":
            # Seviye gecisleri
            new_level = self.check_kirmizi_long_level(price, bands, level)
            if new_level and new_level != level:
                self.upgrade_level(pos, level, new_level, price, bands)
                level = new_level

            # Cikis kosullari
            if level in ("ENTRY", "BE"):
                # Kirmizi Ust Ic Tampon altina duserse
                if price < bands["kirmizi_ust_ictampon"]:
                    self.close_position(pos, price, f"{level} EXIT")
                    return
            elif level in ("CE1", "CE2"):
                # Chandelier veya Kirmizi Ust Dis Cizgi altina duserse
                if pos.get("chandelier_start_price") is not None:
                    chandelier = pos["highest_price"] - self.chandelier_mult * pos["atr_at_entry"]
                    if price < chandelier:
                        self.close_position(pos, price, "CHANDELIER EXIT")
                        return
                if price < bands["kirmizi_ust_disticizgi"]:
                    self.close_position(pos, price, f"{level} EXIT")
                    return

        else:  # short
            new_level = self.check_kirmizi_short_level(price, bands, level)
            if new_level and new_level != level:
                self.upgrade_level(pos, level, new_level, price, bands)
                level = new_level

            if level in ("ENTRY", "BE"):
                if price > bands["kirmizi_alt_ictampon"]:
                    self.close_position(pos, price, f"{level} EXIT")
                    return
            elif level in ("CE1", "CE2"):
                if pos.get("chandelier_start_price") is not None:
                    chandelier = pos["lowest_price"] + self.chandelier_mult * pos["atr_at_entry"]
                    if price > chandelier:
                        self.close_position(pos, price, "CHANDELIER EXIT")
                        return
                if price > bands["kirmizi_alt_disticizgi"]:
                    self.close_position(pos, price, f"{level} EXIT")
                    return

    def check_kirmizi_long_level(self, price: float, bands: dict, current: str) -> str:
        """Hangi seviyeye gecmeli?"""
        if current == "CE2":
            return "CE2"
        if price > bands["kirmizi_ust_seviye2"] and current in ("ENTRY", "BE", "CE1"):
            return "CE2"
        if price > bands["kirmizi_ust_seviye1"] and current in ("ENTRY", "BE"):
            return "CE1"
        if price > bands["kirmizi_ust_distampon"] and current == "ENTRY":
            return "BE"
        return current

    def check_kirmizi_short_level(self, price: float, bands: dict, current: str) -> str:
        if current == "CE2":
            return "CE2"
        if price < bands["kirmizi_alt_seviye2"] and current in ("ENTRY", "BE", "CE1"):
            return "CE2"
        if price < bands["kirmizi_alt_seviye1"] and current in ("ENTRY", "BE"):
            return "CE1"
        if price < bands["kirmizi_alt_distampon"] and current == "ENTRY":
            return "BE"
        return current

    # ──────────────────────────────────────────
    # MAVI CIKIS
    # ──────────────────────────────────────────

    def handle_mavi_exit(self, pos: dict, price: float, bands: dict):
        coin = pos["coin"]
        side = pos["side"]
        level = pos["level"]

        if side == "long":
            # Winrate cikis kontrolu (her seviyede gecerli)
            if price > bands["mavi_ust_ictampon"]:
                self.close_position(pos, price, "WINRATE EXIT")
                return

            new_level = self.check_mavi_long_level(price, bands, level)
            if new_level and new_level != level:
                self.upgrade_level(pos, level, new_level, price, bands)
                level = new_level

            if level == "ENTRY":
                if price < bands["mavi_alt_distampon"]:
                    self.close_position(pos, price, "ENTRY EXIT")
                    return
            elif level == "BE":
                if price < bands["mavi_alt_distampon"]:
                    self.close_position(pos, price, "BE EXIT")
                    return
            elif level in ("CE1", "CE2"):
                if pos.get("chandelier_start_price") is not None:
                    chandelier = pos["highest_price"] - self.chandelier_mult * pos["atr_at_entry"]
                    if price < chandelier:
                        self.close_position(pos, price, "CHANDELIER EXIT")
                        return
                if price < bands["mavi_alt_disticizgi"]:
                    self.close_position(pos, price, f"{level} EXIT")
                    return

        else:  # short
            # Winrate cikis kontrolu
            if price < bands["mavi_alt_ictampon"]:
                self.close_position(pos, price, "WINRATE EXIT")
                return

            new_level = self.check_mavi_short_level(price, bands, level)
            if new_level and new_level != level:
                self.upgrade_level(pos, level, new_level, price, bands)
                level = new_level

            if level == "ENTRY":
                if price > bands["mavi_ust_distampon"]:
                    self.close_position(pos, price, "ENTRY EXIT")
                    return
            elif level == "BE":
                if price > bands["mavi_ust_distampon"]:
                    self.close_position(pos, price, "BE EXIT")
                    return
            elif level in ("CE1", "CE2"):
                if pos.get("chandelier_start_price") is not None:
                    chandelier = pos["lowest_price"] + self.chandelier_mult * pos["atr_at_entry"]
                    if price > chandelier:
                        self.close_position(pos, price, "CHANDELIER EXIT")
                        return
                if price > bands["mavi_ust_disticizgi"]:
                    self.close_position(pos, price, f"{level} EXIT")
                    return

    def check_mavi_long_level(self, price: float, bands: dict, current: str) -> str:
        """Mavi Long: fiyat asagidan yukari geliyor — seviyeler ustte."""
        if current == "CE2":
            return "CE2"
        if price > bands["mavi_alt_seviye2"] and current in ("ENTRY", "BE", "CE1"):
            return "CE2"
        if price > bands["mavi_alt_seviye1"] and current in ("ENTRY", "BE"):
            return "CE1"
        if price > bands["mavi_alt_ictampon"] and current == "ENTRY":
            return "BE"
        return current

    def check_mavi_short_level(self, price: float, bands: dict, current: str) -> str:
        """Mavi Short: fiyat yukaridan asagi gidiyor — seviyeler altta."""
        if current == "CE2":
            return "CE2"
        if price < bands["mavi_ust_seviye2"] and current in ("ENTRY", "BE", "CE1"):
            return "CE2"
        if price < bands["mavi_ust_seviye1"] and current in ("ENTRY", "BE"):
            return "CE1"
        if price < bands["mavi_ust_ictampon"] and current == "ENTRY":
            return "BE"
        return current

    # ──────────────────────────────────────────
    # SEVIYE GECISI
    # ──────────────────────────────────────────

    def upgrade_level(self, pos: dict, old_level: str, new_level: str, price: float, bands: dict):
        """Seviye yukseltme — istatistik, chandelier baslangici, bildirim."""
        updates = {"level": new_level}

        # CE1 veya CE2 gecisinde chandelier baslar/yenilenir
        if new_level in ("CE1", "CE2"):
            updates["chandelier_start_price"] = price
            # Highest/lowest'i sifirla — chandelier o andaki fiyattan basliyor
            updates["highest_price"] = price
            updates["lowest_price"] = price

        state.update_position(pos["coin"], updates)
        pos.update(updates)

        # Bildirim icin seviye fiyati ve anlik K/Z
        level_price = self.get_level_price(pos, new_level, bands)
        gross_pnl, net_pnl, _ = self.calculate_pnl(pos, price)

        notifier.send(msg_level_transition(
            pos, old_level, new_level, price, level_price,
            gross_pnl, net_pnl, state.get_open_count(), self.max_slots
        ))

    def get_level_price(self, pos: dict, level: str, bands: dict) -> float:
        color = pos["color"]
        side = pos["side"]
        if color == "kirmizi" and side == "long":
            mapping = {
                "BE": bands["kirmizi_ust_distampon"],
                "CE1": bands["kirmizi_ust_seviye1"],
                "CE2": bands["kirmizi_ust_seviye2"],
            }
        elif color == "kirmizi" and side == "short":
            mapping = {
                "BE": bands["kirmizi_alt_distampon"],
                "CE1": bands["kirmizi_alt_seviye1"],
                "CE2": bands["kirmizi_alt_seviye2"],
            }
        elif color == "mavi" and side == "long":
            mapping = {
                "BE": bands["mavi_alt_ictampon"],
                "CE1": bands["mavi_alt_seviye1"],
                "CE2": bands["mavi_alt_seviye2"],
            }
        else:  # mavi short
            mapping = {
                "BE": bands["mavi_ust_ictampon"],
                "CE1": bands["mavi_ust_seviye1"],
                "CE2": bands["mavi_ust_seviye2"],
            }
        return mapping.get(level, pos["entry_price"])

    # ──────────────────────────────────────────
    # POZISYON KAPATMA
    # ──────────────────────────────────────────

    def calculate_pnl(self, pos: dict, exit_price: float):
        """Brut K/Z, Net K/Z, komisyon."""
        entry = pos["entry_price"]
        qty = pos["qty"]
        if pos["side"] == "long":
            gross = (exit_price - entry) * qty
        else:
            gross = (entry - exit_price) * qty
        commission = pos["volume"] * self.commission_rate
        net = gross - commission
        return gross, net, commission

    def close_position(self, pos: dict, exit_price: float, exit_type: str):
        coin = pos["coin"]

        # Bybit'te kapat
        try:
            self.bybit.close_position(coin, pos["order_side"], pos["qty"])
        except Exception as e:
            notifier.send(msg_error(self.name, coin, "Kapatma Emri Hatasi", str(e)))
            return

        gross, net, commission = self.calculate_pnl(pos, exit_price)
        exit_data = {
            "exit_time": datetime.now(),
            "exit_price": exit_price,
            "exit_type": exit_type,
            "gross_pnl": gross,
            "net_pnl": net,
            "commission": commission,
        }

        state.remove_position(coin, exit_data)
        notifier.send(msg_trade_closed(pos, exit_data, state.get_open_count(), self.max_slots))

    def handle_external_close(self, pos: dict):
        """Bybit'te pozisyon yok — manuel/SL/tasfiye olmus."""
        coin = pos["coin"]
        # Gercek K/Z'yi Bybit'ten cek
        try:
            closed = self.bybit.get_closed_pnl(coin, limit=5)
        except Exception:
            closed = []

        # Bizim acilis zamanimizdan sonra kapanan en yeni kayit
        match = None
        for c in closed:
            try:
                created_time = int(c.get("createdTime", 0)) / 1000
                if created_time >= pos["entry_time"].timestamp():
                    match = c
                    break
            except Exception:
                continue

        if match:
            exit_price = float(match.get("avgExitPrice", pos["entry_price"]))
            real_pnl = float(match.get("closedPnl", 0))
            commission = pos["volume"] * self.commission_rate
            net = real_pnl - commission
            # Kapanma sebebi
            order_type = match.get("orderType", "")
            exec_type = match.get("execType", "")
            if "Liq" in exec_type or "Bust" in exec_type:
                reason = "TASFIYE"
            elif "StopLoss" in order_type or "Stop" in exec_type:
                reason = "SL EXIT"
            else:
                reason = "MANUEL EXIT"
        else:
            exit_price = self.bybit.get_price(coin)
            gross, net, commission = self.calculate_pnl(pos, exit_price)
            real_pnl = gross
            reason = "MANUEL EXIT"

        exit_data = {
            "exit_time": datetime.now(),
            "exit_price": exit_price,
            "exit_type": reason,
            "gross_pnl": real_pnl,
            "net_pnl": net,
            "commission": commission,
        }
        state.remove_position(coin, exit_data)
        notifier.send(msg_external_close(pos, exit_data, reason, state.get_open_count(), self.max_slots))
