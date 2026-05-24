"""
SMARTBOT REDBLUE — state.py
Merkezi durum yonetimi. Tum threadler buradan okur/yazar.
Thread-safe yapi icin lock kullanilir.
"""

import threading
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional


class BotState:
    """Botun tum anlik durumunu tutar. Singleton mantigiyla kullanilir."""

    def __init__(self):
        self.lock = threading.RLock()

        # Bot baslangic bilgileri
        self.start_time: Optional[datetime] = None
        self.initial_balance: float = 0.0
        self.stake: float = 0.0
        self.leverage: int = 20

        # Flagler — yapi: {coin: {"kirmizi_long": bool, "kirmizi_short": bool, "mavi_long": bool, "mavi_short": bool}}
        self.flags: Dict[str, Dict[str, bool]] = {}

        # Fiyat hafizasi — yapi: {coin: deque([(timestamp, price), ...])}
        self.price_history: Dict[str, deque] = {}

        # Acik pozisyonlar — yapi: {coin: position_dict}
        # position_dict: {
        #   "coin", "side" (long/short), "color" (kirmizi/mavi),
        #   "entry_price", "entry_time", "qty", "volume",
        #   "sl_price", "level" (ENTRY/BE/CE1/CE2),
        #   "highest_price", "lowest_price",  # chandelier icin
        #   "chandelier_start_price",  # CE1/CE2 gecisindeki fiyat
        #   "atr_at_entry"  # giris anindaki ATR
        # }
        self.open_positions: Dict[str, dict] = {}

        # Istatistikler
        self.stats = {
            "total_flags_opened": 0,
            "total_flags_closed": 0,
            "total_flags_to_trade": 0,
            "trades_opened": 0,
            "trades_closed": 0,
            "trades_won": 0,
            "trades_lost": 0,
            "total_pnl": 0.0,
            "total_commission": 0.0,
        }

        # Periyot bazli istatistikler (10dk, 1sa, 8sa, 24sa icin sifirlanabilir)
        # Her periyot icin ayri sayac tutmak yerine, kapanmis islem ve flag tarihlerini saklarız
        self.closed_trades_log: List[dict] = []  # her kapanan islem
        self.flag_events_log: List[dict] = []    # her flag acma/silme/dönüşüm
        self.opened_trades_log: List[dict] = []  # her acilan islem

        # En iyi/en kotu islem
        self.best_trade: Optional[dict] = None
        self.worst_trade: Optional[dict] = None

    # ──────────────────────────────────────────
    # FLAG YONETIMI
    # ──────────────────────────────────────────

    def init_coin(self, coin: str):
        """Bir coin icin baslangic yapilari olustur."""
        with self.lock:
            if coin not in self.flags:
                self.flags[coin] = {
                    "kirmizi_long": False,
                    "kirmizi_short": False,
                    "mavi_long": False,
                    "mavi_short": False,
                }
            if coin not in self.price_history:
                self.price_history[coin] = deque()

    def get_flag(self, coin: str, flag_name: str) -> bool:
        with self.lock:
            if coin not in self.flags:
                return False
            return self.flags[coin].get(flag_name, False)

    def set_flag(self, coin: str, flag_name: str, value: bool):
        with self.lock:
            self.init_coin(coin)
            old_value = self.flags[coin][flag_name]
            self.flags[coin][flag_name] = value

            # Istatistik
            if value and not old_value:
                self.stats["total_flags_opened"] += 1
                self.flag_events_log.append({
                    "time": datetime.now(),
                    "coin": coin,
                    "flag": flag_name,
                    "event": "opened"
                })
            elif not value and old_value:
                self.stats["total_flags_closed"] += 1
                self.flag_events_log.append({
                    "time": datetime.now(),
                    "coin": coin,
                    "flag": flag_name,
                    "event": "closed"
                })

    def flag_to_trade(self, coin: str, flag_name: str):
        """Flag isleme donustugunde cagir — istatistik icin."""
        with self.lock:
            self.stats["total_flags_to_trade"] += 1
            self.flag_events_log.append({
                "time": datetime.now(),
                "coin": coin,
                "flag": flag_name,
                "event": "to_trade"
            })

    def get_all_open_flags(self) -> Dict[str, List[str]]:
        """Tum acik flagleri renge gore grupla."""
        with self.lock:
            result = {"kirmizi": [], "mavi": []}
            for coin, flags in self.flags.items():
                if flags["kirmizi_long"] or flags["kirmizi_short"]:
                    result["kirmizi"].append(coin)
                if flags["mavi_long"] or flags["mavi_short"]:
                    result["mavi"].append(coin)
            return result

    def get_total_open_flags(self) -> int:
        with self.lock:
            count = 0
            for flags in self.flags.values():
                count += sum(1 for v in flags.values() if v)
            return count

    # ──────────────────────────────────────────
    # FIYAT HAFIZASI
    # ──────────────────────────────────────────

    def add_price(self, coin: str, price: float, memory_seconds: int):
        """Fiyat hafizasina ekle, eski fiyatlari temizle."""
        with self.lock:
            self.init_coin(coin)
            now = datetime.now().timestamp()
            self.price_history[coin].append((now, price))
            # Eski fiyatlari temizle
            cutoff = now - memory_seconds
            while self.price_history[coin] and self.price_history[coin][0][0] < cutoff:
                self.price_history[coin].popleft()

    def get_recent_prices(self, coin: str, count: int) -> List[float]:
        """Son N tarama fiyatini dondur (en yeni en sonda)."""
        with self.lock:
            if coin not in self.price_history:
                return []
            prices = list(self.price_history[coin])
            return [p[1] for p in prices[-count:]]

    # ──────────────────────────────────────────
    # POZISYON YONETIMI
    # ──────────────────────────────────────────

    def add_position(self, position: dict):
        with self.lock:
            coin = position["coin"]
            self.open_positions[coin] = position
            self.stats["trades_opened"] += 1
            self.opened_trades_log.append({
                "time": position["entry_time"],
                "coin": coin,
                "color": position["color"],
                "side": position["side"],
            })

    def remove_position(self, coin: str, exit_data: dict):
        """Pozisyonu kapat, istatistikleri guncelle."""
        with self.lock:
            if coin not in self.open_positions:
                return None
            position = self.open_positions.pop(coin)

            # Net K/Z
            net_pnl = exit_data.get("net_pnl", 0.0)
            commission = exit_data.get("commission", 0.0)

            self.stats["trades_closed"] += 1
            self.stats["total_pnl"] += net_pnl
            self.stats["total_commission"] += commission

            if net_pnl > 0:
                self.stats["trades_won"] += 1
            else:
                self.stats["trades_lost"] += 1

            # En iyi / en kotu
            trade_record = {**position, **exit_data}
            if self.best_trade is None or net_pnl > self.best_trade.get("net_pnl", -float("inf")):
                self.best_trade = trade_record
            if self.worst_trade is None or net_pnl < self.worst_trade.get("net_pnl", float("inf")):
                self.worst_trade = trade_record

            self.closed_trades_log.append(trade_record)
            return position

    def get_position(self, coin: str) -> Optional[dict]:
        with self.lock:
            return self.open_positions.get(coin)

    def has_position(self, coin: str) -> bool:
        with self.lock:
            return coin in self.open_positions

    def get_open_count(self) -> int:
        with self.lock:
            return len(self.open_positions)

    def get_all_positions(self) -> List[dict]:
        with self.lock:
            return list(self.open_positions.values())

    def update_position(self, coin: str, updates: dict):
        with self.lock:
            if coin in self.open_positions:
                self.open_positions[coin].update(updates)

    # ──────────────────────────────────────────
    # ISTATISTIK SORGULARI
    # ──────────────────────────────────────────

    def get_closed_trades_since(self, since: datetime) -> List[dict]:
        with self.lock:
            return [t for t in self.closed_trades_log if t.get("exit_time", datetime.min) >= since]

    def get_opened_trades_since(self, since: datetime) -> List[dict]:
        with self.lock:
            return [t for t in self.opened_trades_log if t.get("time", datetime.min) >= since]

    def get_flag_events_since(self, since: datetime) -> List[dict]:
        with self.lock:
            return [e for e in self.flag_events_log if e.get("time", datetime.min) >= since]


# Global singleton
state = BotState()
