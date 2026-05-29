"""
🔴 KIRMIZI THREAD

Ana strateji.
- 15dk mum kapanışında flag tarar (Donchian alt/üst çizgi yön değiştirdi mi)
- 5sn fiyat takibinde işlem açar (cross şartı yok, değme yeterli)
- Seviye geçişlerini cross ile takip eder
- Çıkışları cross ile yapar (en yüksek görülen seviye baz alınır)

Maksimum: 2 Long + 2 Short (slot bazlı)
"""
import threading
import time
import logging

from utils import (
    now_ts, crossed_up, crossed_down,
    touched_from_above, touched_from_below,
)

log = logging.getLogger("RedThread")


# 15dk = 900 sn
CANDLE_PERIOD_SEC = 15 * 60


class RedThread(threading.Thread):
    
    def __init__(self, config, data_manager, trade_manager, blue_thread_ref, yellow_thread_ref):
        super().__init__(name="RedThread", daemon=True)
        self.cfg = config
        self.dm = data_manager
        self.tm = trade_manager
        self.blue = blue_thread_ref  # set later
        self.yellow = yellow_thread_ref  # set later
        
        self._stop = threading.Event()
        
        # symbol -> {"long_flag": bool, "short_flag": bool}
        self.flags = {s: {"long_flag": False, "short_flag": False} for s in config.symbols}
        # En son flag check edilen mum timestamp
        self.last_flag_check_ts = {s: None for s in config.symbols}
        # En son mum verisi çekilen 15dk dönemi
        self.last_candle_fetch_period = None
        
        # Bot start = ts; 5 sn interval main loop
    
    def set_thread_refs(self, blue, yellow):
        self.blue = blue
        self.yellow = yellow
    
    def stop(self):
        self._stop.set()
    
    # ------------------------------------------------------------------
    # FLAG TARAMA — 15dk mum kapanışında
    # ------------------------------------------------------------------
    def scan_flags(self):
        """Tüm coinler için flag taraması (mum verisi çekildikten sonra çağrılır)."""
        for symbol in self.cfg.symbols:
            if self.dm.is_paused(symbol):
                continue
            if self._stop.is_set():
                return
            self._scan_flag_one(symbol)
    
    def _scan_flag_one(self, symbol):
        snap = self.dm.get_snapshot(symbol)
        if not snap:
            return
        upper_hist = snap["donchian_upper_history"]
        lower_hist = snap["donchian_lower_history"]
        if len(upper_hist) < 2 or upper_hist[-1] is None or upper_hist[-2] is None:
            return
        if lower_hist[-1] is None or lower_hist[-2] is None:
            return
        
        cur_upper = upper_hist[-1]
        prev_upper = upper_hist[-2]
        cur_lower = lower_hist[-1]
        prev_lower = lower_hist[-2]
        
        last_close_ts = snap["last_candle_close_ts"]
        if self.last_flag_check_ts.get(symbol) == last_close_ts:
            # Aynı mum üstünden tekrar tarama yapma
            return
        self.last_flag_check_ts[symbol] = last_close_ts
        
        # Donchian alt çizgi önceki seviyeden yukarı çıktı → Short Flag
        if cur_lower > prev_lower:
            if not self.flags[symbol]["short_flag"]:
                self.flags[symbol]["short_flag"] = True
                self.tm.log_flag_event(symbol, "RED", "SHORT", "OPENED")
                self.tm.tg.notify_flag(symbol, "RED", "SHORT", "OPENED")
        
        # Donchian üst çizgi önceki seviyeden aşağı indi → Long Flag
        if cur_upper < prev_upper:
            if not self.flags[symbol]["long_flag"]:
                self.flags[symbol]["long_flag"] = True
                self.tm.log_flag_event(symbol, "RED", "LONG", "OPENED")
                self.tm.tg.notify_flag(symbol, "RED", "LONG", "OPENED")
    
    # ------------------------------------------------------------------
    # İŞLEM AÇMA KONTROLÜ — 5sn fiyat takibinde
    # ------------------------------------------------------------------
    def scan_open_signals(self):
        """Tüm coinler için işlem açma sinyali kontrolü."""
        for symbol in self.cfg.symbols:
            if self.dm.is_paused(symbol):
                continue
            if self._stop.is_set():
                return
            self._scan_open_one(symbol)
    
    def _scan_open_one(self, symbol):
        price = self.dm.get_last_price(symbol)
        if price is None:
            return
        d_upper, d_lower = self.dm.get_donchian_current(symbol)
        ema_val = self.dm.get_ema(symbol)
        if d_upper is None or d_lower is None or ema_val is None:
            return
        
        flags = self.flags[symbol]
        
        # SHORT açılışı
        if flags["short_flag"]:
            # Fiyat Donchian alt çizgisine değmesi yeterli (cross yok)
            # + EMA 800 altında
            if touched_from_above(price, d_lower) and price < ema_val:
                self._open_red(symbol, "SHORT", price, d_upper, d_lower)
        
        # LONG açılışı
        if flags["long_flag"]:
            if touched_from_below(price, d_upper) and price > ema_val:
                self._open_red(symbol, "LONG", price, d_upper, d_lower)
    
    def _calc_red_levels(self, side, entry_price, d_upper, d_lower):
        """
        Kırmızı seviyelerini hesapla.
        LOSE = Donchian üst (Short) veya alt (Long), ama max %2 ile sınırlı.
        WINRATE = Entry'den 3x Lose mesafesi.
        ST1..ST5 = Entry ile Winrate arasında 6 eşit parça (5 ara çizgi: ST1..ST5).
        
        Returns: dict {"LOSE", "ENTRY", "ST1".."ST5", "WINRATE"}
        """
        max_lose_pct = self.cfg.max_lose_pct / 100.0
        
        if side == "SHORT":
            raw_lose = d_upper
            max_lose = entry_price * (1.0 + max_lose_pct)
            lose = min(raw_lose, max_lose)  # daha yakın olan
            # Lose mesafesi (Entry'den yukarı)
            lose_dist = lose - entry_price
            if lose_dist <= 0:
                # geçersiz, %2 zorla
                lose = max_lose
                lose_dist = lose - entry_price
            # Winrate = Entry'den lose mesafesinin 3 katı kadar aşağı
            winrate = entry_price - self.cfg.risk_reward * lose_dist
            # ST1..ST5 arası 6 eşit parça
            step = (entry_price - winrate) / 6.0
            levels = {
                "LOSE": lose,
                "ENTRY": entry_price,
                "ST1": entry_price - step * 1,
                "ST2": entry_price - step * 2,
                "ST3": entry_price - step * 3,
                "ST4": entry_price - step * 4,
                "ST5": entry_price - step * 5,
                "WINRATE": winrate,
            }
        else:  # LONG
            raw_lose = d_lower
            max_lose = entry_price * (1.0 - max_lose_pct)
            lose = max(raw_lose, max_lose)  # daha yakın olan (yukarıda)
            lose_dist = entry_price - lose
            if lose_dist <= 0:
                lose = max_lose
                lose_dist = entry_price - lose
            winrate = entry_price + self.cfg.risk_reward * lose_dist
            step = (winrate - entry_price) / 6.0
            levels = {
                "LOSE": lose,
                "ENTRY": entry_price,
                "ST1": entry_price + step * 1,
                "ST2": entry_price + step * 2,
                "ST3": entry_price + step * 3,
                "ST4": entry_price + step * 4,
                "ST5": entry_price + step * 5,
                "WINRATE": winrate,
            }
        return levels
    
    def _open_red(self, symbol, side, entry_price, d_upper, d_lower):
        levels = self._calc_red_levels(side, entry_price, d_upper, d_lower)
        # Geçerli mi?
        if side == "SHORT" and levels["LOSE"] <= entry_price:
            return
        if side == "LONG" and levels["LOSE"] >= entry_price:
            return
        
        trade = self.tm.open_trade(
            symbol=symbol,
            side=side,
            thread="RED",
            entry_price=entry_price,
            lose_line=levels["LOSE"],
            winrate_line=levels["WINRATE"],
            level_lines=levels,
            current_level="ENTRY",
        )
        if not trade:
            return
        
        # Flag sıfırla
        if side == "SHORT":
            if self.flags[symbol]["short_flag"]:
                self.flags[symbol]["short_flag"] = False
                self.tm.log_flag_event(symbol, "RED", "SHORT", "CONVERTED")
        else:
            if self.flags[symbol]["long_flag"]:
                self.flags[symbol]["long_flag"] = False
                self.tm.log_flag_event(symbol, "RED", "LONG", "CONVERTED")
        
        # Mavi ve Sarı tablo oluştur
        try:
            self.blue.create_table_for_red(trade)
        except Exception as e:
            log.error(f"Mavi tablo oluşturma hatası: {e}")
        try:
            self.yellow.create_table_for_red(trade)
        except Exception as e:
            log.error(f"Sarı tablo oluşturma hatası: {e}")
    
    # ------------------------------------------------------------------
    # SEVİYE GEÇİŞİ + ÇIKIŞ — 5sn fiyat takibinde
    # ------------------------------------------------------------------
    LEVEL_ORDER = ["ENTRY", "ST1", "ST2", "ST3", "ST4", "ST5"]
    # current_level → çıkış için karşılaştırılacak çizgi adı (LOSE/WINRATE hariç)
    EXIT_LINE_FOR_LEVEL = {
        "ENTRY": "LOSE",
        "ST1": "LOSE",
        "ST2": "ENTRY",
        "ST3": "ST1",
        "ST4": "ST2",
        "ST5": "ST3",
    }
    
    def scan_levels_and_exits(self):
        """Tüm açık Kırmızı işlemler için seviye/çıkış kontrolü."""
        trades = self.tm.slots.get_open_by_thread("RED")
        for t in trades:
            if self._stop.is_set():
                return
            self._tick_red(t)
    
    def _tick_red(self, trade):
        symbol = trade.symbol
        prev = self.dm.get_prev_price(symbol)
        curr = self.dm.get_last_price(symbol)
        if prev is None or curr is None:
            return
        
        levels = trade.level_lines
        
        # 1) WINRATE çıkışı (her seviyede geçerli)
        if trade.side == "SHORT":
            if crossed_down(prev, curr, levels["WINRATE"]):
                exit_name = f"KIRMIZI {trade.current_level} WINRATE EXIT"
                self.tm.close_red_and_dependents(trade, exit_name, curr)
                return
        else:
            if crossed_up(prev, curr, levels["WINRATE"]):
                exit_name = f"KIRMIZI {trade.current_level} WINRATE EXIT"
                self.tm.close_red_and_dependents(trade, exit_name, curr)
                return
        
        # 2) Seviye geçişi (cross şartı)
        new_level = self._maybe_advance_level(trade, prev, curr)
        if new_level:
            trade.current_level = new_level
            trade.highest_level = new_level  # zaten en yüksek
            self.tm.tg.notify_level_change(trade, new_level)
        
        # 3) Mevcut seviyeden çıkış kontrolü
        cur_lvl = trade.current_level
        exit_line_name = self.EXIT_LINE_FOR_LEVEL.get(cur_lvl)
        if exit_line_name is None:
            return
        exit_line = levels.get(exit_line_name)
        if exit_line is None:
            return
        
        if trade.side == "SHORT":
            # Short'ta çıkış: fiyat yukarı cross etti
            if crossed_up(prev, curr, exit_line):
                exit_name = self._red_exit_name(cur_lvl, exit_line_name)
                self.tm.close_red_and_dependents(trade, exit_name, curr)
        else:
            if crossed_down(prev, curr, exit_line):
                exit_name = self._red_exit_name(cur_lvl, exit_line_name)
                self.tm.close_red_and_dependents(trade, exit_name, curr)
    
    def _red_exit_name(self, current_level, exit_line_name):
        return f"KIRMIZI {current_level} {exit_line_name} EXIT"
    
    def _maybe_advance_level(self, trade, prev, curr):
        """Seviye yükseldi mi? cross ile."""
        cur_lvl = trade.current_level
        try:
            idx = self.LEVEL_ORDER.index(cur_lvl)
        except ValueError:
            return None
        # Sonraki seviye yok?
        if idx + 1 >= len(self.LEVEL_ORDER):
            return None
        next_lvl = self.LEVEL_ORDER[idx + 1]
        next_line = trade.level_lines.get(next_lvl)
        if next_line is None:
            return None
        if trade.side == "SHORT":
            # Fiyat aşağı cross etti
            if crossed_down(prev, curr, next_line):
                return next_lvl
        else:
            if crossed_up(prev, curr, next_line):
                return next_lvl
        return None
    
    # ------------------------------------------------------------------
    # FLAG SİLME — fiyat çizgiye değmeden geri dönerse?
    # ------------------------------------------------------------------
    # Belge: Sarı/Mavi flag fiyat çizgiyi cross edip geri dönerse silinir.
    # Kırmızı flag → İşlem açılınca silinir. Açılmadığı sürece bekler.
    # Yeni mum gelince üzerine yeni flag açılabilir, sorun değil.
    
    # ------------------------------------------------------------------
    # RUN
    # ------------------------------------------------------------------
    def run(self):
        log.info("Kırmızı thread başladı.")
        while not self._stop.is_set():
            try:
                self.scan_open_signals()
                self.scan_levels_and_exits()
            except Exception as e:
                log.exception(f"RedThread döngü hatası: {e}")
            # 1sn tick — DataManager 5sn'de fiyat günceller, biz daha sık check ederek crossleri kaçırmıyoruz
            time.sleep(1.0)
        log.info("Kırmızı thread durdu.")
