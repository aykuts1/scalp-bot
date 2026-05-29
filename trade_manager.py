"""
Trade Manager
- Slot ve işlem limit yönetimi
- İşlem açma/kapama
- Bot başlangıcında borsadaki açık işlemleri okuyup slot olarak işaretler
"""
import math
import threading
import logging
from datetime import datetime, timezone

from utils import now_ts, fmt_money

log = logging.getLogger("TradeManager")


class Trade:
    """Tek bir bot tarafından yönetilen işlem."""
    __slots__ = (
        "id", "symbol", "side", "thread", "entry_price", "qty",
        "opened_ts", "current_level", "highest_level",
        "lose_line", "winrate_line", "level_lines",
        "closed", "close_price", "close_ts", "exit_name",
        "pnl_usdt", "pnl_pct", "position_idx",
        "extras",
    )
    
    _id_counter = 0
    _id_lock = threading.Lock()
    
    @classmethod
    def _next_id(cls):
        with cls._id_lock:
            cls._id_counter += 1
            return cls._id_counter
    
    def __init__(self, symbol, side, thread, entry_price, qty, position_idx,
                 lose_line, winrate_line, level_lines, current_level):
        self.id = Trade._next_id()
        self.symbol = symbol
        self.side = side  # "LONG" / "SHORT"
        self.thread = thread  # "RED" / "BLUE" / "YELLOW"
        self.entry_price = float(entry_price)
        self.qty = float(qty)
        self.opened_ts = now_ts()
        self.current_level = current_level
        self.highest_level = current_level
        self.lose_line = lose_line  # float veya None
        self.winrate_line = winrate_line  # float veya None
        self.level_lines = dict(level_lines)  # {"ENTRY": x, "ST1": y, ...}
        self.closed = False
        self.close_price = None
        self.close_ts = None
        self.exit_name = None
        self.pnl_usdt = 0.0
        self.pnl_pct = 0.0
        self.position_idx = position_idx
        self.extras = {}


class SlotManager:
    """
    Slot = coin + yön kombinasyonu.
    Her slot içinde max 1 Kırmızı + 1 Mavi + 1 Sarı işlem.
    Kural: Bir coinde aynı anda max 1 Kırmızı işlem (yön farketmez).
    Bu Kırmızı'ya bağlı 1 Mavi + 1 Sarı açılır.
    Kırmızı kapanmadan o coinde yeni Kırmızı (ne aynı ne ters yön) açılamaz.
    Global limit yok — 16 coinin hepsinde aynı anda Kırmızı olabilir.
    Bot başlangıcında borsadaki dış pozisyonlar slot olarak sayılır (yönetilmez).
    """
    
    def __init__(self, config):
        self.cfg = config
        self.lock = threading.RLock()
        # Bot tarafından yönetilen aktif işlemler
        # key -> Trade
        # key formatı: (symbol, side, thread)  side=LONG/SHORT, thread=RED/BLUE/YELLOW
        self.trades = {}
        
        # Dışarıdan (borsada) açık ama bot tarafından yönetilmeyen slotlar
        # Yeni mantık: yön farketmez, sadece coin bazında set tut
        self.external_slots = set()  # set of symbol strings
        
        # Kırmızı işlem ile bağlı Mavi/Sarı arasındaki bağlantı
        # red_trade.id -> {"blue": Trade or None, "yellow": Trade or None}
        self.red_links = {}
    
    # ------------- SLOT SAYIMI -------------
    def _coins_with_red(self):
        """Şu anda bir Kırmızı işlem (bot veya dış) açık olan coinlerin seti."""
        coins = set()
        with self.lock:
            for (sym, _side, th), t in self.trades.items():
                if th == "RED" and not t.closed:
                    coins.add(sym)
            # Dış pozisyonlar (yön farketmez)
            coins |= set(self.external_slots)
        return coins
    
    def can_open_new_slot(self, symbol, side):
        """
        Yeni kural: Bu coinde herhangi bir yönde Kırmızı varsa açılamaz.
        Yoksa açılabilir (global limit yok).
        """
        return symbol not in self._coins_with_red()
    
    def red_can_open(self, symbol, side):
        """Yeni bir Kırmızı işlem açabilir miyiz? Coin başına 1 Kırmızı kuralı."""
        with self.lock:
            if symbol in self._coins_with_red():
                return False, "Bu coinde zaten bir Kırmızı işlem var (yön farketmez)"
            return True, ""
    
    def blue_can_open(self, red_trade_id):
        """Belirli Kırmızı'ya bağlı Mavi açabilir miyiz?"""
        with self.lock:
            link = self.red_links.get(red_trade_id)
            if not link:
                return False, "Kırmızı bağlantı yok"
            if link.get("blue") and not link["blue"].closed:
                return False, "Bu Kırmızı'ya bağlı Mavi zaten açık"
            return True, ""
    
    def yellow_can_open(self, red_trade_id):
        """Belirli Kırmızı'ya bağlı Sarı açabilir miyiz?"""
        with self.lock:
            link = self.red_links.get(red_trade_id)
            if not link:
                return False, "Kırmızı bağlantı yok"
            if link.get("yellow") and not link["yellow"].closed:
                return False, "Bu Kırmızı'ya bağlı Sarı zaten açık"
            return True, ""
    
    # ------------- TRADE REGISTRY -------------
    def register_red(self, trade):
        with self.lock:
            self.trades[(trade.symbol, trade.side, "RED")] = trade
            self.red_links[trade.id] = {"blue": None, "yellow": None}
    
    def register_blue(self, red_trade, blue_trade):
        with self.lock:
            self.trades[(blue_trade.symbol, blue_trade.side, "BLUE")] = blue_trade
            link = self.red_links.setdefault(red_trade.id, {"blue": None, "yellow": None})
            link["blue"] = blue_trade
    
    def register_yellow(self, red_trade, yellow_trade):
        with self.lock:
            self.trades[(yellow_trade.symbol, yellow_trade.side, "YELLOW")] = yellow_trade
            link = self.red_links.setdefault(red_trade.id, {"blue": None, "yellow": None})
            link["yellow"] = yellow_trade
    
    def unregister(self, trade):
        with self.lock:
            key = (trade.symbol, trade.side, trade.thread)
            if key in self.trades and self.trades[key].id == trade.id:
                del self.trades[key]
    
    def get_red_link(self, red_trade_id):
        with self.lock:
            return self.red_links.get(red_trade_id)
    
    def get_red_for(self, symbol, side):
        """Belirli coin/yöndeki açık Kırmızı işlem."""
        with self.lock:
            t = self.trades.get((symbol, side, "RED"))
            if t and not t.closed:
                return t
            return None
    
    def get_all_open(self):
        with self.lock:
            return [t for t in self.trades.values() if not t.closed]
    
    def get_open_by_thread(self, thread):
        with self.lock:
            return [t for t in self.trades.values() if not t.closed and t.thread == thread]
    
    # ------------- EXTERNAL SLOTS -------------
    def add_external_slot(self, symbol, side=None):
        """Dış pozisyon - yön farketmez, sadece coinin slotu işgal."""
        with self.lock:
            self.external_slots.add(symbol)
    
    def clear_external_slot(self, symbol, side=None):
        with self.lock:
            self.external_slots.discard(symbol)


class TradeManager:
    """
    İşlem açma/kapama operasyonlarını yönetir.
    SlotManager ile birlikte çalışır.
    """
    
    def __init__(self, config, data_manager, telegram_notifier):
        self.cfg = config
        self.dm = data_manager
        self.tg = telegram_notifier
        self.slots = SlotManager(config)
        self.stats_lock = threading.Lock()
        # İstatistik için kapanan işlemler kuyruğu (raporlar için)
        self.closed_trades_history = []
        self.flag_history = []  # her flag açılış/kapanış olayı
        self.errors_history = []
        self._insufficient_balance_count = 0
        self._slot_full_count = 0
        self._error_count = 0
        # Stake (12 saatte bir güncellenir)
        self.stake_usdt = 0.0
        self.update_stake()
    
    def update_stake(self):
        """Bakiyenin %stake_pct'i kadar stake belirle."""
        bal = self.dm.update_balance()
        new_stake = bal * (self.cfg.stake_pct / 100.0)
        self.stake_usdt = new_stake
        log.info(f"Stake güncellendi: bakiye={bal:.4f} USDT, stake={new_stake:.4f} USDT")
        return new_stake
    
    def get_stake(self):
        return self.stake_usdt
    
    # ------------------------------------------------------------------
    # BAŞLANGIÇTA BORSADAKİ POZİSYONLARI OKU (slot olarak işaretle)
    # ------------------------------------------------------------------
    def load_external_positions(self):
        """
        Bot başlatıldığında borsadaki açık pozisyonları okur ve
        her birini external slot olarak işaretler (yönetilmez).
        """
        positions = self.dm.get_open_positions()
        for p in positions:
            try:
                sz = float(p.get("size", 0))
                if sz <= 0:
                    continue
                symbol = p.get("symbol")
                bybit_side = p.get("side")  # "Buy" or "Sell"
                side = "LONG" if bybit_side == "Buy" else "SHORT"
                self.slots.add_external_slot(symbol, side)
                log.info(f"External slot işaretlendi: {symbol} {side} (size={sz})")
            except Exception as e:
                log.error(f"External pozisyon okuma hatası: {e}")
    
    # ------------------------------------------------------------------
    # MİKTAR HESAPLAMA
    # ------------------------------------------------------------------
    def _calc_qty(self, symbol, entry_price):
        """
        Stake USDT × leverage = pozisyon büyüklüğü (USDT)
        qty = pozisyon büyüklüğü / fiyat
        Daha sonra qtyStep'e yuvarlanır.
        """
        if entry_price <= 0:
            return 0.0
        position_value_usdt = self.stake_usdt * self.cfg.leverage
        raw_qty = position_value_usdt / entry_price
        
        info = self.dm.get_instrument_info(symbol)
        if not info:
            return raw_qty
        step = info["qtyStep"]
        min_qty = info["minOrderQty"]
        if step > 0:
            qty = math.floor(raw_qty / step) * step
            # Yuvarlama hatalarına karşı string'leme
            decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
            qty = round(qty, decimals + 4)
        else:
            qty = raw_qty
        if qty < min_qty:
            return 0.0
        return qty
    
    # ------------------------------------------------------------------
    # POSITION_IDX (Bybit hedge mode)
    # ------------------------------------------------------------------
    def _position_idx(self, side):
        # Bybit hedge mode:
        # 1 = Buy/Long
        # 2 = Sell/Short
        return 1 if side == "LONG" else 2
    
    def _order_side(self, side):
        return "Buy" if side == "LONG" else "Sell"
    
    def _close_side(self, side):
        # Kapatmak için ters yön
        return "Sell" if side == "LONG" else "Buy"
    
    # ------------------------------------------------------------------
    # AÇMA
    # ------------------------------------------------------------------
    def open_trade(self, symbol, side, thread, entry_price,
                   lose_line, winrate_line, level_lines, current_level,
                   parent_red_trade=None):
        """
        Genel işlem açma fonksiyonu.
        Slot kontrolü yapar.
        Başarılı olursa Trade objesi döner, hata olursa None.
        """
        # Slot kontrolü
        if thread == "RED":
            ok, msg = self.slots.red_can_open(symbol, side)
            if not ok:
                # Coinde zaten Kırmızı var → kullanıcıya bildir (flag tetiklendi ama açılamadı)
                self._slot_full_count += 1
                self.tg.notify_slot_full(symbol, side, thread)
                return None
        elif thread == "BLUE":
            if not parent_red_trade:
                return None
            ok, msg = self.slots.blue_can_open(parent_red_trade.id)
            if not ok:
                return None
        elif thread == "YELLOW":
            if not parent_red_trade:
                return None
            ok, msg = self.slots.yellow_can_open(parent_red_trade.id)
            if not ok:
                return None
        else:
            return None
        
        # Miktar
        qty = self._calc_qty(symbol, entry_price)
        if qty <= 0:
            self._insufficient_balance_count += 1
            self.tg.notify_insufficient_balance(symbol, side, thread, qty)
            return None
        
        # Hard SL fiyatı
        sl_price = self._calc_hard_sl(side, entry_price)
        
        # Order
        try:
            self.dm.place_market_order(
                symbol=symbol,
                side=self._order_side(side),
                qty=qty,
                position_idx=self._position_idx(side),
                stop_loss=sl_price,
            )
        except Exception as e:
            self._error_count += 1
            self.errors_history.append({"ts": now_ts(), "symbol": symbol, "thread": thread, "msg": str(e)})
            self.tg.notify_error("İşlem açma hatası", symbol, thread, str(e))
            return None
        
        # Trade objesi
        trade = Trade(
            symbol=symbol,
            side=side,
            thread=thread,
            entry_price=entry_price,
            qty=qty,
            position_idx=self._position_idx(side),
            lose_line=lose_line,
            winrate_line=winrate_line,
            level_lines=level_lines,
            current_level=current_level,
        )
        
        # Register
        if thread == "RED":
            self.slots.register_red(trade)
        elif thread == "BLUE":
            self.slots.register_blue(parent_red_trade, trade)
            trade.extras["parent_red_id"] = parent_red_trade.id
        elif thread == "YELLOW":
            self.slots.register_yellow(parent_red_trade, trade)
            trade.extras["parent_red_id"] = parent_red_trade.id
        
        # Telegram bildirim
        self.tg.notify_trade_open(trade)
        return trade
    
    def _calc_hard_sl(self, side, entry_price):
        pct = self.cfg.hard_sl_pct / 100.0
        if side == "LONG":
            return round(entry_price * (1.0 - pct), 8)
        else:
            return round(entry_price * (1.0 + pct), 8)
    
    # ------------------------------------------------------------------
    # KAPATMA
    # ------------------------------------------------------------------
    def close_trade(self, trade, exit_name, close_price=None):
        """Bir işlemi kapat."""
        if trade.closed:
            return False
        
        if close_price is None:
            close_price = self.dm.get_last_price(trade.symbol) or trade.entry_price
        
        try:
            self.dm.close_position_market(
                symbol=trade.symbol,
                side_to_close=self._close_side(trade.side),
                qty=trade.qty,
                position_idx=trade.position_idx,
            )
        except Exception as e:
            self._error_count += 1
            self.errors_history.append({"ts": now_ts(), "symbol": trade.symbol, "thread": trade.thread, "msg": str(e)})
            self.tg.notify_error("İşlem kapatma hatası", trade.symbol, trade.thread, str(e))
            return False
        
        # PnL hesapla (kaldıraçlı, gerçek USDT yaklaşık)
        if trade.side == "LONG":
            pnl_pct_raw = (close_price - trade.entry_price) / trade.entry_price
        else:
            pnl_pct_raw = (trade.entry_price - close_price) / trade.entry_price
        # Kaldıraçsız oran (stake bazlı PnL = stake * leverage * pnl_pct_raw)
        pnl_usdt = self.stake_usdt * self.cfg.leverage * pnl_pct_raw
        pnl_pct_on_stake = pnl_pct_raw * self.cfg.leverage * 100.0
        
        trade.closed = True
        trade.close_price = close_price
        trade.close_ts = now_ts()
        trade.exit_name = exit_name
        trade.pnl_usdt = pnl_usdt
        trade.pnl_pct = pnl_pct_on_stake
        
        with self.stats_lock:
            self.closed_trades_history.append(trade)
        
        self.slots.unregister(trade)
        self.tg.notify_trade_close(trade)
        return True
    
    def close_red_and_dependents(self, red_trade, exit_name, close_price=None):
        """Kırmızı'yı kapatırken bağlı Mavi/Sarı'yı da kapat."""
        link = self.slots.get_red_link(red_trade.id)
        if link:
            blue = link.get("blue")
            if blue and not blue.closed:
                self.close_trade(blue, f"KIRMIZI KAPANDI ({exit_name})")
            yellow = link.get("yellow")
            if yellow and not yellow.closed:
                self.close_trade(yellow, f"KIRMIZI KAPANDI ({exit_name})")
        self.close_trade(red_trade, exit_name, close_price)
    
    # ------------------------------------------------------------------
    # KAYIT / STATS
    # ------------------------------------------------------------------
    def log_flag_event(self, symbol, thread, side, event_type):
        """event_type: OPENED / CONVERTED / DELETED"""
        self.flag_history.append({
            "ts": now_ts(),
            "symbol": symbol,
            "thread": thread,
            "side": side,
            "event": event_type,
        })
    
    def get_closed_trades_window(self, start_ts, end_ts):
        with self.stats_lock:
            return [t for t in self.closed_trades_history if t.close_ts and start_ts <= t.close_ts <= end_ts]
    
    def get_all_closed_trades(self):
        with self.stats_lock:
            return list(self.closed_trades_history)
