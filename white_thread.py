"""
⚪️ BEYAZ THREAD

Kırmızı'dan bağımsız ikinci ana thread. Donchian50 üst/alt çizgiler ile çalışır.

Açılış mantığı (Beyaz SHORT için — LONG tam simetri):

1) FLAG + GİRİŞ ÇİZGİSİ KAYDI (anlık fiyat hareketi)
   - Fiyat Donchian50 üst çizgisine değer → flag açılır + giriş çizgisi kaydedilir.
   - Mesafe = (Donchian üst − Donchian alt) / 4
   - Giriş çizgisi = Donchian üst − mesafe  (Donchian'ın 1/4 içinde)
   - Fiyat üste her tekrar değdiğinde flag ve giriş çizgisi GÜNCELLENİR (eski silinir).
   - Flag asla otomatik silinmez. Sadece işleme dönüşünce silinir.
   - 15dk mum kapanışı beklemez (Kırmızı'dan FARKI bu).

2) İŞLEM AÇILIŞI (cross + EMA800)
   - Fiyat kaydedilen giriş çizgisini aşağı cross + fiyat < EMA800 → BEYAZ SHORT açılır.
   - LONG için: yukarı cross + fiyat > EMA800.
   - İşlem açılınca flag VE giriş çizgisi silinir.

3) DEĞME ve CROSS aynı taramada üst üste binemez
   - Değme tespit edildiği taramada cross kontrolü atlanır.

Tablo (işlem açıldığı andaki Donchian değerleriyle sabitlenir):
- LOSE = o anki Donchian üst (Short için yukarıda)
- ENTRY = giriş çizgisi (Donchian'ın 1/4 içinde)
- WINRATE = o anki Donchian alt (Short için aşağıda)
- ENTRY ↔ WINRATE arası 6 eşit parça → ENTRY, ST1, ST2, ST3, ST4, ST5
- max_lose_pct SINIRI YOK (Kırmızı'dan FARKI bu)

Seviye/çıkış kuralları Kırmızı ile aynı:
- Seviye sadece ileri yönlü (geri gitmez)
- EXIT_LINE_FOR_LEVEL haritası:
    ENTRY/ST1 → LOSE, ST2 → ENTRY, ST3 → ST1, ST4 → ST2, ST5 → ST3
- WINRATE cross → kâr ile kapanış
- Kapanırken bağlı Mor + Turuncu da kapanır
"""
import threading
import logging

from utils import crossed_up, crossed_down

log = logging.getLogger("WhiteThread")


class WhiteThread(threading.Thread):

    LEVEL_ORDER = ["ENTRY", "ST1", "ST2", "ST3", "ST4", "ST5"]
    EXIT_LINE_FOR_LEVEL = {
        "ENTRY": "LOSE",
        "ST1": "LOSE",
        "ST2": "ENTRY",
        "ST3": "ST1",
        "ST4": "ST2",
        "ST5": "ST3",
    }

    def __init__(self, config, data_manager, trade_manager,
                 purple_thread_ref=None, orange_thread_ref=None):
        super().__init__(name="WhiteThread", daemon=True)
        self.cfg = config
        self.dm = data_manager
        self.tm = trade_manager
        self.purple = purple_thread_ref
        self.orange = orange_thread_ref

        self._stop = threading.Event()

        # symbol -> {"long_flag": bool, "short_flag": bool,
        #            "long_entry_line": float|None, "short_entry_line": float|None}
        self.state = {s: {
            "long_flag": False,
            "short_flag": False,
            "long_entry_line": None,
            "short_entry_line": None,
        } for s in config.symbols}

    def set_thread_refs(self, purple, orange):
        self.purple = purple
        self.orange = orange

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    # OKUMA HELPERS (raporlar için)
    # ------------------------------------------------------------------
    def get_open_flags(self):
        """Açık Beyaz flag'leri döndür."""
        result = []
        for symbol, st in self.state.items():
            if st["long_flag"]:
                result.append({"symbol": symbol, "thread": "WHITE", "side": "LONG",
                               "entry_line": st["long_entry_line"]})
            if st["short_flag"]:
                result.append({"symbol": symbol, "thread": "WHITE", "side": "SHORT",
                               "entry_line": st["short_entry_line"]})
        return result

    # ------------------------------------------------------------------
    # AÇILIŞ — 2 AŞAMALI (değme + cross)
    # ------------------------------------------------------------------
    def scan_open_signals(self):
        for symbol in self.cfg.symbols:
            if self.dm.is_paused(symbol):
                continue
            if self._stop.is_set():
                return
            self._scan_open_one(symbol)

    def _scan_open_one(self, symbol):
        prev, curr = self.dm.get_price_pair(symbol)
        if prev is None or curr is None:
            return

        d_upper, d_lower = self.dm.get_donchian_current(symbol)
        ema_val = self.dm.get_ema(symbol)
        if d_upper is None or d_lower is None or ema_val is None:
            return

        st = self.state[symbol]

        # SHORT akışı
        self._tick_short_open(symbol, st, prev, curr, d_upper, d_lower, ema_val)
        # LONG akışı (simetrik)
        self._tick_long_open(symbol, st, prev, curr, d_upper, d_lower, ema_val)

    def _calc_entry_line(self, d_upper, d_lower, side):
        """
        Beyaz'ın giriş çizgisi hesabı.
        Mesafe = (d_upper - d_lower) / 4
        Short: d_upper - mesafe  (üstün altında)
        Long:  d_lower + mesafe  (altın üstünde)
        """
        if d_upper is None or d_lower is None:
            return None
        spread = d_upper - d_lower
        if spread <= 0:
            return None
        mesafe = spread / 4.0
        if side == "SHORT":
            return d_upper - mesafe
        else:
            return d_lower + mesafe

    def _tick_short_open(self, symbol, st, prev, curr, d_upper, d_lower, ema_val):
        """
        Beyaz SHORT açılış mantığı.
        ADIM 1: Fiyat Donchian ÜST'e değdi mi? → flag aç + giriş çizgisi (re-)kaydet
        ADIM 2: Kaydedilen giriş çizgisini aşağı cross + EMA800 altı → SHORT aç
        """
        # ADIM 1: ÜST'E DEĞME (prev < d_upper idi, curr >= d_upper oldu)
        if crossed_up(prev, curr, d_upper):
            new_entry = self._calc_entry_line(d_upper, d_lower, "SHORT")
            if new_entry is None:
                return
            # Flag açık değilse aç, açıksa sadece giriş çizgisini güncelle
            if not st["short_flag"]:
                st["short_flag"] = True
                self.tm.log_flag_event(symbol, "WHITE", "SHORT", "OPENED")
            st["short_entry_line"] = new_entry
            log.info(f"[{symbol}] BEYAZ SHORT giriş çizgisi: {new_entry}")
            # Aynı taramada cross kontrolü yok
            return

        # ADIM 2: Kaydedilmiş çizgi cross + EMA800 altı
        if st["short_flag"] and st["short_entry_line"] is not None:
            entry_line = st["short_entry_line"]
            if crossed_down(prev, curr, entry_line):
                if curr < ema_val:
                    self._open_white(symbol, "SHORT", entry_line, d_upper, d_lower)

    def _tick_long_open(self, symbol, st, prev, curr, d_upper, d_lower, ema_val):
        """LONG açılış (SHORT'un simetriği)."""
        # ADIM 1: ALT'A DEĞME (prev > d_lower idi, curr <= d_lower oldu)
        if crossed_down(prev, curr, d_lower):
            new_entry = self._calc_entry_line(d_upper, d_lower, "LONG")
            if new_entry is None:
                return
            if not st["long_flag"]:
                st["long_flag"] = True
                self.tm.log_flag_event(symbol, "WHITE", "LONG", "OPENED")
            st["long_entry_line"] = new_entry
            log.info(f"[{symbol}] BEYAZ LONG giriş çizgisi: {new_entry}")
            return

        # ADIM 2: Yukarı cross + EMA800 üstü
        if st["long_flag"] and st["long_entry_line"] is not None:
            entry_line = st["long_entry_line"]
            if crossed_up(prev, curr, entry_line):
                if curr > ema_val:
                    self._open_white(symbol, "LONG", entry_line, d_upper, d_lower)

    def _calc_white_levels(self, side, entry_price, d_upper, d_lower):
        """
        Beyaz seviyelerini hesapla.
        LOSE = doğrudan Donchian (max_lose_pct sınırı YOK)
        WINRATE = doğrudan ters Donchian
        ST1..ST5 = ENTRY ile WINRATE arası 6 eşit parça
        """
        if side == "SHORT":
            lose = d_upper
            winrate = d_lower
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
            lose = d_lower
            winrate = d_upper
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

    def _open_white(self, symbol, side, entry_price, d_upper, d_lower):
        levels = self._calc_white_levels(side, entry_price, d_upper, d_lower)

        if side == "SHORT" and levels["LOSE"] <= entry_price:
            return
        if side == "LONG" and levels["LOSE"] >= entry_price:
            return

        trade = self.tm.open_trade(
            symbol=symbol, side=side, thread="WHITE",
            entry_price=entry_price,
            lose_line=levels["LOSE"],
            winrate_line=levels["WINRATE"],
            level_lines=levels,
            current_level="ENTRY",
        )
        if not trade:
            # Açılamadı (slot dolu / qty yetersiz / order hatası) → flag temizleme yok
            return

        # İşlem açıldı: flag + giriş çizgisi silinir
        st = self.state[symbol]
        if side == "SHORT":
            if st["short_flag"]:
                st["short_flag"] = False
                self.tm.log_flag_event(symbol, "WHITE", "SHORT", "CONVERTED")
            st["short_entry_line"] = None
        else:
            if st["long_flag"]:
                st["long_flag"] = False
                self.tm.log_flag_event(symbol, "WHITE", "LONG", "CONVERTED")
            st["long_entry_line"] = None

        # Mor ve Turuncu tablolarını kur
        try:
            if self.purple:
                self.purple.create_table_for_white(trade)
        except Exception as e:
            log.error(f"Mor tablo oluşturma hatası: {e}")
        try:
            if self.orange:
                self.orange.create_table_for_white(trade)
        except Exception as e:
            log.error(f"Turuncu tablo oluşturma hatası: {e}")

    # ------------------------------------------------------------------
    # SEVİYE GEÇİŞİ + ÇIKIŞ
    # ------------------------------------------------------------------
    def scan_levels_and_exits(self):
        trades = self.tm.slots.get_open_by_thread("WHITE")
        for t in trades:
            if self._stop.is_set():
                return
            self._tick_white(t)

    def _tick_white(self, trade):
        prev, curr = self.dm.get_price_pair(trade.symbol)
        if prev is None or curr is None:
            return

        levels = trade.level_lines

        # 1) WINRATE çıkışı
        if trade.side == "SHORT":
            if crossed_down(prev, curr, levels["WINRATE"]):
                self.tm.close_white_and_dependents(
                    trade, f"BEYAZ {trade.current_level} WINRATE EXIT", curr)
                return
        else:
            if crossed_up(prev, curr, levels["WINRATE"]):
                self.tm.close_white_and_dependents(
                    trade, f"BEYAZ {trade.current_level} WINRATE EXIT", curr)
                return

        # 2) Seviye geçişi
        new_level = self._maybe_advance_level(trade, prev, curr)
        if new_level:
            trade.current_level = new_level
            trade.highest_level = new_level
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
            if crossed_up(prev, curr, exit_line):
                self.tm.close_white_and_dependents(
                    trade, f"BEYAZ {cur_lvl} {exit_line_name} EXIT", curr)
        else:
            if crossed_down(prev, curr, exit_line):
                self.tm.close_white_and_dependents(
                    trade, f"BEYAZ {cur_lvl} {exit_line_name} EXIT", curr)

    def _maybe_advance_level(self, trade, prev, curr):
        cur_lvl = trade.current_level
        try:
            idx = self.LEVEL_ORDER.index(cur_lvl)
        except ValueError:
            return None
        if idx + 1 >= len(self.LEVEL_ORDER):
            return None
        next_lvl = self.LEVEL_ORDER[idx + 1]
        next_line = trade.level_lines.get(next_lvl)
        if next_line is None:
            return None
        if trade.side == "SHORT":
            if crossed_down(prev, curr, next_line):
                return next_lvl
        else:
            if crossed_up(prev, curr, next_line):
                return next_lvl
        return None

    # ------------------------------------------------------------------
    # RUN
    # ------------------------------------------------------------------
    def run(self):
        log.info("Beyaz thread başladı.")
        scan_interval = self.cfg.thread_scan_interval_sec
        while not self._stop.is_set():
            try:
                self.scan_open_signals()
                self.scan_levels_and_exits()
            except Exception as e:
                log.exception(f"WhiteThread döngü hatası: {e}")
            self._stop.wait(scan_interval)
        log.info("Beyaz thread durdu.")
