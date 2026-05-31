"""
Trade Manager
- Trade objesi (tek bir işlemi temsil eder)
- SlotManager (hangi coinde ne açık, kim kime bağlı)
  * KIRMIZI grubu: Kırmızı → Mavi + Sarı
  * BEYAZ grubu: Beyaz → Mor + Turuncu
  * İki grup bağımsız çalışır
- TradeManager (açma, kapatma, stake hesabı, PnL hesabı)
  * Hard SL çakışma kuralı: aynı coin+yön için "daha geniş" SL kullanılır
    (Short'ta yüksek, Long'da düşük)
  * Real entry: her thread kendi giriş bilgilerini kaydeder, Bybit'e sormaz
"""
import math
import threading
import time
import logging

from utils import now_ts, fmt_money

log = logging.getLogger("TradeManager")


# =========================================================================
# TRADE OBJESİ
# =========================================================================
class Trade:
    _id_counter = 0
    _id_lock = threading.Lock()

    def __init__(self, symbol, side, thread, entry_price, qty,
                 lose_line=None, winrate_line=None,
                 level_lines=None, current_level=None,
                 position_idx=None, hard_sl=None):
        with Trade._id_lock:
            Trade._id_counter += 1
            self.id = Trade._id_counter

        self.symbol = symbol
        self.side = side  # "LONG" / "SHORT"
        # Geçerli thread değerleri: "RED" / "BLUE" / "YELLOW" / "WHITE" / "PURPLE" / "ORANGE"
        self.thread = thread
        self.entry_price = float(entry_price)
        self.qty = float(qty)
        self.opened_ts = now_ts()

        self.current_level = current_level
        self.highest_level = current_level

        self.lose_line = lose_line
        self.winrate_line = winrate_line
        self.level_lines = dict(level_lines) if level_lines else {}

        self.position_idx = position_idx

        # Bu işlemin kendi hesapladığı Hard SL (raporlama/referans için)
        # Bybit'te aktif olan SL, çakışma durumunda farklı olabilir (daha geniş).
        self.hard_sl = hard_sl

        # Kapanış bilgileri
        self.closed = False
        self.close_price = None
        self.close_ts = None
        self.exit_name = None
        self.pnl_usdt = 0.0
        self.pnl_pct = 0.0

        # SARI/TURUNCU chandelier alanları
        self.chandelier_distance = None
        self.chandelier_best_price = None
        self.chandelier_line = None

        # SARI/TURUNCU yeniden giriş çizgisi (chandelier sonrası hafıza)
        self.reentry_line = None

        # Ek bilgiler (parent_red_id / parent_white_id vs)
        self.extras = {}

    def duration_sec(self):
        end = self.close_ts if self.closed else now_ts()
        return max(0, end - self.opened_ts)


# =========================================================================
# SLOT MANAGER
# =========================================================================
class SlotManager:
    """
    Slot kuralları:
    - Her coine en fazla 1 KIRMIZI (yön farketmez).
    - Her coine en fazla 1 BEYAZ (yön farketmez).
    - Aynı coinde 1 Kırmızı + 1 Beyaz aynı anda açık olabilir (bağımsız gruplar).
    - Her Kırmızı'ya 1 Mavi + 1 Sarı (mantık gereği aynı anda olamaz).
    - Her Beyaz'a 1 Mor + 1 Turuncu (mantık gereği aynı anda olamaz).
    """

    # Thread isimleri ve karşılıkları
    MAIN_THREADS = ("RED", "WHITE")
    HEDGE_THREADS = ("BLUE", "PURPLE")
    TREND_THREADS = ("YELLOW", "ORANGE")

    # KIRMIZI grubu eşleştirmeleri
    RED_GROUP_HEDGE = "BLUE"
    RED_GROUP_TREND = "YELLOW"
    # BEYAZ grubu eşleştirmeleri
    WHITE_GROUP_HEDGE = "PURPLE"
    WHITE_GROUP_TREND = "ORANGE"

    def __init__(self):
        self.lock = threading.Lock()
        # (symbol, side, thread) -> Trade
        self.trades = {}
        # KIRMIZI bağ haritası: red_trade_id -> {"blue": Trade|None, "yellow": Trade|None}
        self.red_links = {}
        # BEYAZ bağ haritası: white_trade_id -> {"purple": Trade|None, "orange": Trade|None}
        self.white_links = {}

    # ============================================================
    # KIRMIZI GRUBU
    # ============================================================

    def coin_has_red(self, symbol):
        """O coinde herhangi yönde açık Kırmızı var mı?"""
        with self.lock:
            for (s, side, thr), t in self.trades.items():
                if s == symbol and thr == "RED" and not t.closed:
                    return True
            return False

    def red_can_open(self, symbol):
        if self.coin_has_red(symbol):
            return (False, "COİNDE KIRMIZI VAR")
        return (True, None)

    def blue_can_open(self, red_id):
        with self.lock:
            link = self.red_links.get(red_id)
            if link and link.get("blue") and not link["blue"].closed:
                return (False, "BU KIRMIZIYA BAĞLI MAVİ ZATEN VAR")
            return (True, None)

    def yellow_can_open(self, red_id):
        with self.lock:
            link = self.red_links.get(red_id)
            if link and link.get("yellow") and not link["yellow"].closed:
                return (False, "BU KIRMIZIYA BAĞLI SARI ZATEN VAR")
            return (True, None)

    def register_red(self, trade):
        with self.lock:
            self.trades[(trade.symbol, trade.side, "RED")] = trade
            self.red_links[trade.id] = {"blue": None, "yellow": None}

    def register_blue(self, trade, red_id):
        with self.lock:
            self.trades[(trade.symbol, trade.side, "BLUE")] = trade
            link = self.red_links.setdefault(red_id, {"blue": None, "yellow": None})
            link["blue"] = trade
            trade.extras["parent_red_id"] = red_id

    def register_yellow(self, trade, red_id):
        with self.lock:
            self.trades[(trade.symbol, trade.side, "YELLOW")] = trade
            link = self.red_links.setdefault(red_id, {"blue": None, "yellow": None})
            link["yellow"] = trade
            trade.extras["parent_red_id"] = red_id

    def get_red_link(self, red_id):
        with self.lock:
            return self.red_links.get(red_id)

    def get_red_for(self, symbol, side):
        with self.lock:
            return self.trades.get((symbol, side, "RED"))

    def get_red_for_symbol(self, symbol):
        with self.lock:
            for (s, side, thr), t in self.trades.items():
                if s == symbol and thr == "RED" and not t.closed:
                    return t
            return None

    # ============================================================
    # BEYAZ GRUBU
    # ============================================================

    def coin_has_white(self, symbol):
        """O coinde herhangi yönde açık Beyaz var mı?"""
        with self.lock:
            for (s, side, thr), t in self.trades.items():
                if s == symbol and thr == "WHITE" and not t.closed:
                    return True
            return False

    def white_can_open(self, symbol):
        if self.coin_has_white(symbol):
            return (False, "COİNDE BEYAZ VAR")
        return (True, None)

    def purple_can_open(self, white_id):
        with self.lock:
            link = self.white_links.get(white_id)
            if link and link.get("purple") and not link["purple"].closed:
                return (False, "BU BEYAZA BAĞLI MOR ZATEN VAR")
            return (True, None)

    def orange_can_open(self, white_id):
        with self.lock:
            link = self.white_links.get(white_id)
            if link and link.get("orange") and not link["orange"].closed:
                return (False, "BU BEYAZA BAĞLI TURUNCU ZATEN VAR")
            return (True, None)

    def register_white(self, trade):
        with self.lock:
            self.trades[(trade.symbol, trade.side, "WHITE")] = trade
            self.white_links[trade.id] = {"purple": None, "orange": None}

    def register_purple(self, trade, white_id):
        with self.lock:
            self.trades[(trade.symbol, trade.side, "PURPLE")] = trade
            link = self.white_links.setdefault(white_id, {"purple": None, "orange": None})
            link["purple"] = trade
            trade.extras["parent_white_id"] = white_id

    def register_orange(self, trade, white_id):
        with self.lock:
            self.trades[(trade.symbol, trade.side, "ORANGE")] = trade
            link = self.white_links.setdefault(white_id, {"purple": None, "orange": None})
            link["orange"] = trade
            trade.extras["parent_white_id"] = white_id

    def get_white_link(self, white_id):
        with self.lock:
            return self.white_links.get(white_id)

    def get_white_for(self, symbol, side):
        with self.lock:
            return self.trades.get((symbol, side, "WHITE"))

    def get_white_for_symbol(self, symbol):
        with self.lock:
            for (s, side, thr), t in self.trades.items():
                if s == symbol and thr == "WHITE" and not t.closed:
                    return t
            return None

    # ============================================================
    # ORTAK
    # ============================================================

    def unregister(self, trade):
        with self.lock:
            key = (trade.symbol, trade.side, trade.thread)
            if key in self.trades and self.trades[key].id == trade.id:
                del self.trades[key]

            if trade.thread == "RED":
                self.red_links.pop(trade.id, None)
            elif trade.thread == "WHITE":
                self.white_links.pop(trade.id, None)
            elif trade.thread == "BLUE":
                parent_red_id = trade.extras.get("parent_red_id")
                if parent_red_id is not None:
                    link = self.red_links.get(parent_red_id)
                    if link and link.get("blue") and link["blue"].id == trade.id:
                        link["blue"] = None
            elif trade.thread == "YELLOW":
                parent_red_id = trade.extras.get("parent_red_id")
                if parent_red_id is not None:
                    link = self.red_links.get(parent_red_id)
                    if link and link.get("yellow") and link["yellow"].id == trade.id:
                        link["yellow"] = None
            elif trade.thread == "PURPLE":
                parent_white_id = trade.extras.get("parent_white_id")
                if parent_white_id is not None:
                    link = self.white_links.get(parent_white_id)
                    if link and link.get("purple") and link["purple"].id == trade.id:
                        link["purple"] = None
            elif trade.thread == "ORANGE":
                parent_white_id = trade.extras.get("parent_white_id")
                if parent_white_id is not None:
                    link = self.white_links.get(parent_white_id)
                    if link and link.get("orange") and link["orange"].id == trade.id:
                        link["orange"] = None

    def get_all_open(self):
        with self.lock:
            return [t for t in self.trades.values() if not t.closed]

    def get_open_by_thread(self, thread):
        with self.lock:
            return [t for (s, sd, thr), t in self.trades.items()
                    if thr == thread and not t.closed]

    def get_open_by_symbol_side(self, symbol, side):
        """O coinde o yönde açık olan tüm trade'leri döndürür (Hard SL çakışma kontrolü için)."""
        with self.lock:
            return [t for (s, sd, thr), t in self.trades.items()
                    if s == symbol and sd == side and not t.closed]

    def count_by_thread(self):
        """Açık işlem sayısı kırılımı (6 thread)."""
        counts = {"RED": 0, "BLUE": 0, "YELLOW": 0,
                  "WHITE": 0, "PURPLE": 0, "ORANGE": 0}
        with self.lock:
            for t in self.trades.values():
                if not t.closed:
                    counts[t.thread] = counts.get(t.thread, 0) + 1
        return counts


# =========================================================================
# TRADE MANAGER
# =========================================================================
class TradeManager:
    def __init__(self, config, data_manager, telegram_notifier):
        self.cfg = config
        self.dm = data_manager
        self.tg = telegram_notifier

        self.slots = SlotManager()

        # History
        self.closed_trades_history = []  # list of Trade
        self.flag_history = []  # list of dict
        self.errors_history = []  # list of dict

        # Sayaçlar
        self._insufficient_balance_count = 0
        self._slot_full_count = 0
        self._error_count = 0

        # Stake
        self.stake_usdt = 0.0
        self._stake_lock = threading.Lock()

        # İlk stake hesabı
        self.update_stake()

        # Rate limit koruması: ardışık order arası küçük bekleme
        self._order_lock = threading.Lock()
        self._last_order_ts = 0.0
        self._order_min_gap_sec = 0.1

    # ------------------------------------------------------------------
    # STAKE
    # ------------------------------------------------------------------
    def update_stake(self):
        bal = self.dm.get_balance()
        new_stake = bal * (self.cfg.stake_pct / 100.0)
        with self._stake_lock:
            self.stake_usdt = new_stake
        return new_stake

    def get_stake(self):
        with self._stake_lock:
            return self.stake_usdt

    # ------------------------------------------------------------------
    # YARDIMCILAR
    # ------------------------------------------------------------------
    def _position_idx(self, side):
        return 1 if side == "LONG" else 2

    def _order_side(self, side):
        return "Buy" if side == "LONG" else "Sell"

    def _close_side(self, side):
        return "Sell" if side == "LONG" else "Buy"

    def _calc_qty(self, symbol, entry_price):
        """Stake × leverage / entry → qtyStep'e taban yuvarlama."""
        info = self.dm.get_instrument_info(symbol)
        if not info:
            return 0.0
        stake = self.get_stake()
        if entry_price <= 0:
            return 0.0
        raw = (stake * self.cfg.leverage) / entry_price
        step = info["qtyStep"]
        if step <= 0:
            return 0.0
        qty = math.floor(raw / step) * step
        if step < 1:
            decimals = max(0, -int(math.floor(math.log10(step))))
        else:
            decimals = 0
        qty = round(qty, decimals + 4)
        if qty < info["minOrderQty"]:
            return 0.0
        return qty

    def _round_to_tick(self, price, tick_size, side, is_sl=True):
        """
        SL fiyatını tickSize'a yuvarla. SL "güvenli tarafa" yuvarlanır.
        Long SL (entry altında): aşağı yuvarla.
        Short SL (entry üstünde): yukarı yuvarla.
        """
        if tick_size <= 0:
            return price
        if is_sl:
            if side == "LONG":
                return math.floor(price / tick_size) * tick_size
            else:
                return math.ceil(price / tick_size) * tick_size
        return round(price / tick_size) * tick_size

    def _calc_hard_sl(self, symbol, side, entry_price):
        """Borsaya konacak hard SL fiyatı (tickSize'a yuvarlanmış)."""
        pct = self.cfg.hard_sl_pct / 100.0
        if side == "LONG":
            raw = entry_price * (1.0 - pct)
        else:
            raw = entry_price * (1.0 + pct)

        info = self.dm.get_instrument_info(symbol)
        tick = info["tickSize"] if info else 0.0
        if tick > 0:
            return self._round_to_tick(raw, tick, side, is_sl=True)
        return round(raw, 8)

    def _decide_effective_hard_sl(self, symbol, side, new_hard_sl):
        """
        Aynı coin+yönde başka açık trade(ler) varsa Bybit'te tek SL tutulur.
        "Daha geniş (daha uzak, daha gevşek)" olan SL kullanılır:
        - SHORT: en yüksek SL kazanır (entry'den en uzak yukarıda)
        - LONG: en düşük SL kazanır (entry'den en uzak aşağıda)
        Yeni SL daha sıkıysa eski korunur, daha gevşekse yenisi yazılır.
        """
        existing = self.slots.get_open_by_symbol_side(symbol, side)
        # Sadece hard_sl set edilmiş trade'leri al
        existing_sls = [t.hard_sl for t in existing if t.hard_sl is not None]
        if not existing_sls:
            return new_hard_sl

        if side == "SHORT":
            # En yüksek olan kazanır
            return max(new_hard_sl, max(existing_sls))
        else:
            # LONG: en düşük olan kazanır
            return min(new_hard_sl, min(existing_sls))

    def _rate_limit_order(self):
        """Ardışık emirler arasına 100ms minimum gecikme."""
        with self._order_lock:
            gap = time.time() - self._last_order_ts
            if gap < self._order_min_gap_sec:
                time.sleep(self._order_min_gap_sec - gap)
            self._last_order_ts = time.time()

    # ------------------------------------------------------------------
    # SLOT KONTROLÜ (thread'e göre)
    # ------------------------------------------------------------------
    def _check_slot(self, thread, symbol, side, parent_trade):
        """
        thread'e göre uygun slot kontrolünü çağırır.
        Returns: (ok, msg, parent_id_or_None)
        """
        if thread == "RED":
            ok, msg = self.slots.red_can_open(symbol)
            return ok, msg, None
        elif thread == "WHITE":
            ok, msg = self.slots.white_can_open(symbol)
            return ok, msg, None
        elif thread == "BLUE":
            if not parent_trade:
                return False, "PARENT KIRMIZI YOK", None
            ok, msg = self.slots.blue_can_open(parent_trade.id)
            return ok, msg, parent_trade.id
        elif thread == "YELLOW":
            if not parent_trade:
                return False, "PARENT KIRMIZI YOK", None
            ok, msg = self.slots.yellow_can_open(parent_trade.id)
            return ok, msg, parent_trade.id
        elif thread == "PURPLE":
            if not parent_trade:
                return False, "PARENT BEYAZ YOK", None
            ok, msg = self.slots.purple_can_open(parent_trade.id)
            return ok, msg, parent_trade.id
        elif thread == "ORANGE":
            if not parent_trade:
                return False, "PARENT BEYAZ YOK", None
            ok, msg = self.slots.orange_can_open(parent_trade.id)
            return ok, msg, parent_trade.id
        return False, "BİLİNMEYEN THREAD", None

    def _register_trade(self, trade, thread, parent_id):
        if thread == "RED":
            self.slots.register_red(trade)
        elif thread == "WHITE":
            self.slots.register_white(trade)
        elif thread == "BLUE":
            self.slots.register_blue(trade, parent_id)
        elif thread == "YELLOW":
            self.slots.register_yellow(trade, parent_id)
        elif thread == "PURPLE":
            self.slots.register_purple(trade, parent_id)
        elif thread == "ORANGE":
            self.slots.register_orange(trade, parent_id)

    # ------------------------------------------------------------------
    # AÇMA
    # ------------------------------------------------------------------
    def open_trade(self, symbol, side, thread, entry_price,
                   lose_line=None, winrate_line=None,
                   level_lines=None, current_level=None,
                   parent_red_trade=None, parent_white_trade=None):
        """
        Yeni işlem aç. Real entry: çağıran thread'in verdiği entry_price kullanılır
        (Bybit'in avgPrice'ı sorgulanmaz — kutu birleşmesi yanlış fiyat verir).
        Returns: Trade obj veya None.
        """
        # Parent ataması: thread'e göre
        parent_trade = parent_red_trade or parent_white_trade

        # Slot kontrolü
        ok, msg, parent_id = self._check_slot(thread, symbol, side, parent_trade)
        if not ok:
            if thread in ("RED", "WHITE"):
                self._slot_full_count += 1
                self.tg.notify_slot_full(symbol, side, thread, msg)
            return None

        # Qty hesap
        qty = self._calc_qty(symbol, entry_price)
        if qty <= 0:
            self._insufficient_balance_count += 1
            self.tg.notify_insufficient_balance(symbol, side, thread, entry_price)
            return None

        # Hard SL: bu işlem için kendi hesabı
        own_hard_sl = self._calc_hard_sl(symbol, side, entry_price)
        # Çakışma çözümü: aynı coin+yönde başka trade varsa "daha geniş" olan kullanılır.
        effective_sl = self._decide_effective_hard_sl(symbol, side, own_hard_sl)

        # Bybit order
        try:
            self._rate_limit_order()
            self.dm.place_market_order(
                symbol=symbol,
                side=self._order_side(side),
                qty=qty,
                position_idx=self._position_idx(side),
                stop_loss=effective_sl,
            )
        except Exception as e:
            self._error_count += 1
            self.errors_history.append({
                "ts": now_ts(), "title": "Order açılamadı",
                "symbol": symbol, "module": "TradeManager", "detail": str(e),
            })
            self.tg.notify_error("Order açılamadı", symbol, "TradeManager", str(e))
            return None

        # Pozisyon doğrulama: sadece "açıldı mı?" kontrolü (avgPrice kullanılmaz).
        # Kutu birleşmesi durumunda avgPrice yanıltıcı olur.
        time.sleep(1.5)
        verified = self._verify_position_open(symbol, self._position_idx(side))
        if not verified:
            self._error_count += 1
            self.tg.notify_error(
                "Pozisyon doğrulanamadı (Bybit'te açılmamış olabilir)",
                symbol, "TradeManager",
                f"side={side} qty={qty} entry={entry_price}",
            )
            return None

        # Real entry: çağıran thread'in verdiği fiyat (kendi kayıtlı bilgisi).
        real_entry = float(entry_price)

        # Trade objesi
        trade = Trade(
            symbol=symbol, side=side, thread=thread,
            entry_price=real_entry, qty=qty,
            lose_line=lose_line, winrate_line=winrate_line,
            level_lines=level_lines, current_level=current_level,
            position_idx=self._position_idx(side),
            hard_sl=own_hard_sl,
        )

        # Register
        self._register_trade(trade, thread, parent_id)

        # Telegram bildirim (Bybit'te aktif olan effective SL'i geçer — kullanıcı görsün)
        self.tg.notify_trade_open(trade, hard_sl=effective_sl)
        log.info(f"İşlem açıldı: {thread} {side} {symbol} @ {real_entry} qty={qty} "
                 f"own_sl={own_hard_sl} effective_sl={effective_sl}")

        return trade

    def _verify_position_open(self, symbol, position_idx):
        """
        Pozisyonun gerçekten açıldığını doğrula (sadece varlık kontrolü).
        Hem önbellekten (hızlı) hem doğrudan (kesin) kontrol eder.
        """
        try:
            positions = self.dm.get_open_positions(symbol)
            for p in positions:
                pidx = int(p.get("positionIdx", 0))
                size = float(p.get("size", 0))
                if pidx == position_idx and size > 0:
                    return True
            return False
        except Exception as e:
            log.error(f"Pozisyon doğrulama hatası {symbol}: {e}")
            return False

    # ------------------------------------------------------------------
    # KAPATMA
    # ------------------------------------------------------------------
    def close_trade(self, trade, exit_name, close_price_hint=None):
        """
        Tek bir işlemi kapat. close_price = bot'un en son bildiği fiyat
        (Bybit'e ekstra fiyat sorulmaz — kutu birleşmesi sorununu önler).
        """
        if trade.closed:
            return False

        try:
            self._rate_limit_order()
            self.dm.close_position_market(
                symbol=trade.symbol,
                side_to_close=self._close_side(trade.side),
                qty=trade.qty,
                position_idx=trade.position_idx,
            )
        except Exception as e:
            self._error_count += 1
            self.errors_history.append({
                "ts": now_ts(), "title": "Order kapatılamadı",
                "symbol": trade.symbol, "module": "TradeManager", "detail": str(e),
            })
            self.tg.notify_error("Order kapatılamadı", trade.symbol, "TradeManager", str(e))
            # Yine de trade'i kapalı işaretle ki sonsuz kapatma denenmesin
            close_price = close_price_hint if close_price_hint else trade.entry_price
            self._finalize_close(trade, exit_name, close_price)
            return False

        # Kapanış fiyatı: çağırandan gelen hint (cross anındaki fiyat).
        # Bybit'e avg sorulmaz — kutu birleşmesi yanlış değer verir.
        time.sleep(0.5)
        if close_price_hint is not None:
            actual_close = float(close_price_hint)
        else:
            # Fallback: bot önbelleğindeki son fiyat
            lp = self.dm.get_last_price(trade.symbol)
            actual_close = float(lp) if lp is not None else trade.entry_price

        self._finalize_close(trade, exit_name, actual_close)
        return True

    def _finalize_close(self, trade, exit_name, close_price):
        trade.closed = True
        trade.close_price = float(close_price)
        trade.close_ts = now_ts()
        trade.exit_name = exit_name

        # PnL hesabı
        if trade.entry_price == 0:
            pnl_raw = 0.0
        elif trade.side == "LONG":
            pnl_raw = (trade.close_price - trade.entry_price) / trade.entry_price
        else:
            pnl_raw = (trade.entry_price - trade.close_price) / trade.entry_price

        # Stake o işlem açıldığında neyse onu yansıtmak için açılış anındaki stake olmalı.
        # Şu an global stake_usdt kullanılıyor (yeterince yakın).
        stake = self.get_stake()
        trade.pnl_usdt = stake * self.cfg.leverage * pnl_raw
        trade.pnl_pct = pnl_raw * self.cfg.leverage * 100.0

        # History
        self.closed_trades_history.append(trade)
        # Unregister slot
        self.slots.unregister(trade)

        # Telegram
        self.tg.notify_trade_close(trade)
        log.info(f"İşlem kapandı: {trade.thread} {trade.side} {trade.symbol} "
                 f"@ {trade.close_price} PnL={fmt_money(trade.pnl_usdt)} "
                 f"({trade.pnl_pct:+.2f}%) — {exit_name}")

    # ------------------------------------------------------------------
    # KIRMIZI + BAĞIMLI KAPATMA
    # ------------------------------------------------------------------
    def close_red_and_dependents(self, red_trade, exit_name, close_price_hint=None):
        """Kırmızı'yı kapatırken bağlı Mavi+Sarı varsa önce onları kapatır."""
        if red_trade.thread != "RED":
            log.warning(f"close_red_and_dependents Kırmızı olmayan trade için çağrıldı: {red_trade.thread}")
            return

        link = self.slots.get_red_link(red_trade.id)
        if link:
            blue = link.get("blue")
            yellow = link.get("yellow")
            if blue and not blue.closed:
                self.close_trade(blue, "KIRMIZI KAPANDI", close_price_hint)
            if yellow and not yellow.closed:
                self.close_trade(yellow, "KIRMIZI KAPANDI", close_price_hint)

        self.close_trade(red_trade, exit_name, close_price_hint)

    # ------------------------------------------------------------------
    # BEYAZ + BAĞIMLI KAPATMA
    # ------------------------------------------------------------------
    def close_white_and_dependents(self, white_trade, exit_name, close_price_hint=None):
        """Beyaz'ı kapatırken bağlı Mor+Turuncu varsa önce onları kapatır."""
        if white_trade.thread != "WHITE":
            log.warning(f"close_white_and_dependents Beyaz olmayan trade için çağrıldı: {white_trade.thread}")
            return

        link = self.slots.get_white_link(white_trade.id)
        if link:
            purple = link.get("purple")
            orange = link.get("orange")
            if purple and not purple.closed:
                self.close_trade(purple, "BEYAZ KAPANDI", close_price_hint)
            if orange and not orange.closed:
                self.close_trade(orange, "BEYAZ KAPANDI", close_price_hint)

        self.close_trade(white_trade, exit_name, close_price_hint)

    # ------------------------------------------------------------------
    # FLAG HISTORY (raporlar için)
    # ------------------------------------------------------------------
    def log_flag_event(self, symbol, thread, side, event):
        """event: OPENED, DELETED, CONVERTED"""
        self.flag_history.append({
            "ts": now_ts(),
            "symbol": symbol,
            "thread": thread,
            "side": side,
            "event": event,
        })

    # ------------------------------------------------------------------
    # HISTORY OKUMA (raporlar için)
    # ------------------------------------------------------------------
    def get_closed_trades_window(self, start_ts, end_ts):
        return [t for t in self.closed_trades_history
                if t.close_ts is not None and start_ts <= t.close_ts <= end_ts]

    def get_flag_events_window(self, start_ts, end_ts):
        return [e for e in self.flag_history if start_ts <= e["ts"] <= end_ts]

    def get_errors_window(self, start_ts, end_ts):
        return [e for e in self.errors_history if start_ts <= e["ts"] <= end_ts]

    def get_all_closed_trades(self):
        return list(self.closed_trades_history)

    def get_counters(self):
        return {
            "insufficient_balance": self._insufficient_balance_count,
            "slot_full": self._slot_full_count,
            "error": self._error_count,
        }
