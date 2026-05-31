"""
🟡 SARI THREAD (Yeni Yapı — madde 24-33)

Trend yönünde kâr maksimize.
- Kırmızı Short → Sarı Short
- Kırmızı Long → Sarı Long

Tablo (madde 24):
- Kırmızı giriş ↔ Kırmızı WINRATE arası 6 EŞİT parça
- 6 bölge (üstten alta, Short Kırmızı için): FLAG, ST1, ST2, ST3, ST4, ST5
- 7 çizgi: Kırmızı giriş → ST1 → ST2 → ST3 → ST4 → ST5 → Kırmızı WINRATE
- Her bölgenin "giriş çizgisi" = bölgenin üst sınırı

Flag mantığı (madde 25, KONUM BAZLI):
- Her tarama: fiyat FLAG bölgesindeyse flag açık, değilse kapalı.
- Yukarı çıkış → flag silinir (yanlış alarm)
- Aşağı çıkış → işlem açılır, flag silinir (CONVERTED)

İşlem açılışı (madde 26):
- ST1 giriş çizgisi cross → işlem açılır
- Chandelier devreye girer

Chandelier (madde 27):
- Mesafe: Kırmızı giriş ↔ Kırmızı LOSE mesafesinin yarısı
- "En iyi fiyat" = işlem ömründe görülen en düşük (Short) veya en yüksek (Long)
- Chandelier çizgisi = en iyi fiyat + mesafe (Short için yukarıda, Long için aşağıda)
- Fiyat chandelier çizgisini ters cross → Sarı kapanır

Seviye bildirimleri (madde 28):
- Her bölge geçişinde bildirim atılır
- İKİ YÖNLÜ değişir (Mavi/Kırmızı'dan farklı)
- Çıkışı ETKİLEMEZ, sadece takip

Trail YOK (madde 29).

WINRATE çıkışı (madde 30):
- Fiyat Kırmızı WINRATE'i cross → Kırmızı kapanır → Sarı kâr ile kapanır

Yeniden giriş (madde 31, ÖZEL):
- Chandelier çıkışı sonrası "en iyi fiyat" seviyesine yeni giriş çizgisi çizilir
- Flag aranmaz
- Fiyat kâr yönüne dönüp bu çizgiyi cross ederse → işlem yeniden açılır
- Yeni chandelier aynı mesafeyle sıfırdan başlar
- Yeni seviye = fiyatın bulunduğu bölge
- Kırmızı yaşadığı sürece sınırsız tekrar

Tablo kurulurken (madde 32):
- Fiyat FLAG bölgesindeyse → flag açık
- ST1+ bölgesindeyse → otomatik açılış, seviye = bölge

Sarı'nın sonu (madde 33):
- Kırmızı kapanınca tablo silinir
"""
import threading
import time
import logging

from utils import crossed_up, crossed_down

log = logging.getLogger("YellowThread")


class YellowTable:
    __slots__ = ("red_trade_id", "red_side", "symbol", "side",
                 "levels", "entry_line", "winrate_line", "lose_line",
                 "chandelier_distance",
                 "flag_open", "current_level", "active_trade",
                 "reentry_line")

    def __init__(self, red_trade, levels, chandelier_distance):
        self.red_trade_id = red_trade.id
        self.red_side = red_trade.side
        self.symbol = red_trade.symbol
        # Sarı yön Kırmızı ile aynı
        self.side = red_trade.side
        self.levels = dict(levels)  # ST1..ST5 giriş çizgileri
        self.entry_line = red_trade.level_lines["ENTRY"]
        self.winrate_line = red_trade.winrate_line
        self.lose_line = red_trade.lose_line
        self.chandelier_distance = chandelier_distance
        self.flag_open = False
        self.current_level = None  # işlem yokken None
        self.active_trade = None
        # Chandelier çıkışı sonrası hafıza (yeniden giriş çizgisi)
        self.reentry_line = None


class YellowThread(threading.Thread):

    LEVEL_ORDER = ["ST1", "ST2", "ST3", "ST4", "ST5"]

    def __init__(self, config, data_manager, trade_manager):
        super().__init__(name="YellowThread", daemon=True)
        self.cfg = config
        self.dm = data_manager
        self.tm = trade_manager
        self._stop = threading.Event()
        self.tables = {}
        self.tables_lock = threading.Lock()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    # OKUMA HELPERS (raporlar için)
    # ------------------------------------------------------------------
    def get_open_flags(self):
        result = []
        with self.tables_lock:
            for tbl in self.tables.values():
                if tbl.flag_open and tbl.active_trade is None:
                    result.append({"symbol": tbl.symbol, "thread": "YELLOW",
                                   "side": tbl.side})
        return result

    # ------------------------------------------------------------------
    # TABLO OLUŞTURMA
    # ------------------------------------------------------------------
    def create_table_for_red(self, red_trade):
        entry = red_trade.level_lines["ENTRY"]
        winrate = red_trade.winrate_line
        lose = red_trade.lose_line

        # 6 eşit parça → step
        step = (winrate - entry) / 6.0
        # ST1..ST5 = entry + (1..5) * step
        levels = {
            "ST1": entry + step * 1,
            "ST2": entry + step * 2,
            "ST3": entry + step * 3,
            "ST4": entry + step * 4,
            "ST5": entry + step * 5,
        }
        # Chandelier mesafesi: Kırmızı giriş ↔ Kırmızı LOSE mesafesinin yarısı
        chandelier_distance = abs(lose - entry) / 2.0

        table = YellowTable(red_trade, levels, chandelier_distance)
        with self.tables_lock:
            self.tables[red_trade.id] = table

        all_lines = {
            "Kırmızı Giriş": entry,
            **levels,
            "Kırmızı WINRATE": winrate,
            "Chandelier Mesafe": chandelier_distance,
        }
        self.tm.tg.notify_thread_ready(red_trade, "YELLOW", table.side, all_lines)

        # Madde 32: tablo kurulurken fiyat hangi bölgede?
        self._check_initial_position(table)

        return table

    def _check_initial_position(self, tbl):
        curr = self.dm.get_last_price(tbl.symbol)
        if curr is None:
            return

        zone = self._find_zone(tbl, curr)
        if zone is None:
            return

        if zone == "FLAG":
            tbl.flag_open = True
            self.tm.log_flag_event(tbl.symbol, "YELLOW", tbl.side, "OPENED")
        elif zone in ("ST1", "ST2", "ST3", "ST4", "ST5"):
            # Otomatik açılış
            tbl.flag_open = True
            opened = self._open_yellow(tbl, curr, initial_level=zone)
            if opened:
                self.tm.log_flag_event(tbl.symbol, "YELLOW", tbl.side, "CONVERTED")

    def _find_zone(self, tbl, price):
        """
        Fiyat hangi bölgede?
        Dönüş: "FLAG", "ST1"..."ST5" veya None.
        """
        entry = tbl.entry_line
        winrate = tbl.winrate_line
        levels = tbl.levels

        if tbl.side == "SHORT":
            # Kırmızı Short → tablo aşağı uzanır (entry üstte, winrate altta)
            if price > entry or price < winrate:
                return None
            if price > levels["ST1"]:
                return "FLAG"
            if price > levels["ST2"]:
                return "ST1"
            if price > levels["ST3"]:
                return "ST2"
            if price > levels["ST4"]:
                return "ST3"
            if price > levels["ST5"]:
                return "ST4"
            return "ST5"
        else:  # LONG
            # Kırmızı Long → tablo yukarı uzanır
            if price < entry or price > winrate:
                return None
            if price < levels["ST1"]:
                return "FLAG"
            if price < levels["ST2"]:
                return "ST1"
            if price < levels["ST3"]:
                return "ST2"
            if price < levels["ST4"]:
                return "ST3"
            if price < levels["ST5"]:
                return "ST4"
            return "ST5"

    def remove_table_for_red(self, red_trade_id):
        with self.tables_lock:
            tbl = self.tables.pop(red_trade_id, None)
        if not tbl:
            return
        if tbl.flag_open and tbl.active_trade is None:
            self.tm.log_flag_event(tbl.symbol, "YELLOW", tbl.side, "DELETED")

    # ------------------------------------------------------------------
    # SCAN
    # ------------------------------------------------------------------
    def scan(self):
        # ÖNCE: Kırmızı'sı yok olan tabloları temizle.
        # Hem bot hafızası hem Bybit pozisyon önbelleği kontrol edilir.
        self._cleanup_dead_tables()

        with self.tables_lock:
            tbls = list(self.tables.values())

        for tbl in tbls:
            if self._stop.is_set():
                return
            try:
                self._tick_table(tbl)
            except Exception as e:
                log.exception(f"YellowThread tick hatası ({tbl.symbol}): {e}")

    def _cleanup_dead_tables(self):
        """
        Kırmızı'sı bot hafızasında yoksa/kapalıysa veya Bybit önbelleğinde
        artık yoksa → Sarı'yı kapat + tabloyu sil.
        """
        with self.tables_lock:
            ids_snapshot = list(self.tables.items())

        for red_id, tbl in ids_snapshot:
            red = self.tm.slots.get_red_for(tbl.symbol, tbl.red_side)
            red_missing_in_bot = (red is None or red.id != red_id or red.closed)

            red_pidx = 1 if tbl.red_side == "LONG" else 2
            red_missing_on_bybit = False
            if self.dm.positions_synced():
                if not self.dm.is_position_open(tbl.symbol, red_pidx):
                    red_missing_on_bybit = True

            if red_missing_in_bot or red_missing_on_bybit:
                if tbl.active_trade and not tbl.active_trade.closed:
                    reason = ("SARI KIRMIZI KAPANDI" if red_missing_in_bot
                              else "SARI KIRMIZI BYBIT'TE YOK")
                    curr = self.dm.get_last_price(tbl.symbol)
                    try:
                        self.tm.close_trade(tbl.active_trade, reason, curr)
                    except Exception as e:
                        log.error(f"Sarı acil kapatma hatası ({tbl.symbol}): {e}")
                with self.tables_lock:
                    self.tables.pop(red_id, None)


    def _tick_table(self, tbl):
        curr = self.dm.get_last_price(tbl.symbol)
        prev = self.dm.get_prev_price(tbl.symbol)
        if prev is None or curr is None:
            return

        # Active trade kapandıysa state'i toparla
        if tbl.active_trade and tbl.active_trade.closed:
            tbl.active_trade = None
            tbl.current_level = None

        # ----- A) AÇIK İŞLEM VARSA: chandelier güncelle + çıkış kontrolü + seviye -----
        if tbl.active_trade and not tbl.active_trade.closed:
            self._update_chandelier(tbl, curr)

            # Chandelier çıkışı
            cl = tbl.active_trade.chandelier_line
            if cl is not None:
                if tbl.side == "SHORT":
                    # Fiyat chandelier çizgisini yukarı cross → çıkış
                    if crossed_up(prev, curr, cl):
                        self._chandelier_exit(tbl, curr)
                        return
                else:  # LONG
                    if crossed_down(prev, curr, cl):
                        self._chandelier_exit(tbl, curr)
                        return

            # Seviye değişimi (iki yönlü)
            new_zone = self._find_zone(tbl, curr)
            if new_zone and new_zone in self.LEVEL_ORDER and new_zone != tbl.current_level:
                tbl.current_level = new_zone
                tbl.active_trade.current_level = new_zone
                if new_zone not in ("FLAG",):
                    # En yüksek seviye sadece ilerlerse güncellenir, geri giderse aynı
                    cur_idx = self.LEVEL_ORDER.index(new_zone)
                    high_idx = (self.LEVEL_ORDER.index(tbl.active_trade.highest_level)
                                if tbl.active_trade.highest_level in self.LEVEL_ORDER
                                else -1)
                    if cur_idx > high_idx:
                        tbl.active_trade.highest_level = new_zone
                self.tm.tg.notify_level_change(tbl.active_trade, new_zone)
            return

        # ----- B) AÇIK İŞLEM YOK -----

        # B1) Yeniden giriş hafıza çizgisi varsa (chandelier çıkışı sonrası)
        if tbl.reentry_line is not None:
            if tbl.side == "SHORT":
                # Fiyat kâr yönüne (aşağı) → reentry_line aşağı cross
                if crossed_down(prev, curr, tbl.reentry_line):
                    opened = self._open_yellow(tbl, curr, initial_level=None,
                                               is_reentry=True)
                    if opened:
                        tbl.reentry_line = None
                        return
            else:  # LONG
                if crossed_up(prev, curr, tbl.reentry_line):
                    opened = self._open_yellow(tbl, curr, initial_level=None,
                                               is_reentry=True)
                    if opened:
                        tbl.reentry_line = None
                        return
            # reentry_line kullanılırken klasik FLAG/ST1 akışı atlanır
            return

        # B2) Klasik akış: konum bazlı flag + ST1 cross ile açılış
        zone = self._find_zone(tbl, curr)

        # Konum bazlı flag güncelleme
        if zone == "FLAG":
            if not tbl.flag_open:
                tbl.flag_open = True
                self.tm.log_flag_event(tbl.symbol, "YELLOW", tbl.side, "OPENED")
        else:
            if tbl.flag_open:
                # FLAG bölgesinden çıktı
                # Yukarı çıkış (Kırmızı giriş çizgisinin üstü) → silme
                # Aşağı çıkış (ST1 bölgesine) → ST1 cross zaten işlem açıyor olacak
                tbl.flag_open = False
                # Aşağı çıkış ise işlem açma akışı tetiklenir (aşağıda)
                if zone in self.LEVEL_ORDER:
                    self.tm.log_flag_event(tbl.symbol, "YELLOW", tbl.side, "CONVERTED")
                else:
                    self.tm.log_flag_event(tbl.symbol, "YELLOW", tbl.side, "DELETED")

        # İşlem açılışı: ST1 giriş çizgisi cross
        if zone in self.LEVEL_ORDER:
            st1 = tbl.levels["ST1"]
            if tbl.side == "SHORT":
                if crossed_down(prev, curr, st1):
                    self._open_yellow(tbl, curr, initial_level=zone)
            else:
                if crossed_up(prev, curr, st1):
                    self._open_yellow(tbl, curr, initial_level=zone)

    def _update_chandelier(self, tbl, curr):
        """Chandelier'ı her tick'te güncelle."""
        trade = tbl.active_trade
        if trade.chandelier_distance is None:
            trade.chandelier_distance = tbl.chandelier_distance

        if trade.chandelier_best_price is None:
            trade.chandelier_best_price = curr
        else:
            if tbl.side == "SHORT":
                # En düşük fiyatı tut
                if curr < trade.chandelier_best_price:
                    trade.chandelier_best_price = curr
            else:
                # En yüksek fiyatı tut
                if curr > trade.chandelier_best_price:
                    trade.chandelier_best_price = curr

        # Chandelier çizgisi
        if tbl.side == "SHORT":
            trade.chandelier_line = trade.chandelier_best_price + trade.chandelier_distance
        else:
            trade.chandelier_line = trade.chandelier_best_price - trade.chandelier_distance

    def _chandelier_exit(self, tbl, curr):
        """Chandelier ile çıkış. Reentry_line set edilir."""
        trade = tbl.active_trade
        best = trade.chandelier_best_price

        self.tm.close_trade(trade, f"SARI CHANDELIER EXIT", curr)
        # Yeniden giriş çizgisi = chandelier'in takip ettiği en iyi fiyat
        tbl.reentry_line = best
        tbl.active_trade = None
        tbl.current_level = None
        tbl.flag_open = False

    def _open_yellow(self, tbl, entry_price, initial_level=None, is_reentry=False):
        red_trade = self.tm.slots.get_red_for(tbl.symbol, tbl.red_side)
        if not red_trade or red_trade.id != tbl.red_trade_id or red_trade.closed:
            return False

        level_lines = {
            "ENTRY": tbl.entry_line,
            **tbl.levels,
            "WINRATE": tbl.winrate_line,
        }

        # Seviye fiyatın bulunduğu bölgeye göre
        if initial_level is None:
            zone = self._find_zone(tbl, entry_price)
            initial_level = zone if zone in self.LEVEL_ORDER else "ST1"

        trade = self.tm.open_trade(
            symbol=tbl.symbol, side=tbl.side, thread="YELLOW",
            entry_price=entry_price,
            lose_line=None,
            winrate_line=tbl.winrate_line,
            level_lines=level_lines,
            current_level=initial_level,
            parent_red_trade=red_trade,
        )
        if trade:
            trade.chandelier_distance = tbl.chandelier_distance
            trade.chandelier_best_price = entry_price
            if tbl.side == "SHORT":
                trade.chandelier_line = entry_price + tbl.chandelier_distance
            else:
                trade.chandelier_line = entry_price - tbl.chandelier_distance

            tbl.active_trade = trade
            tbl.current_level = initial_level
            tbl.flag_open = False
            return True
        return False

    # ------------------------------------------------------------------
    # RUN
    # ------------------------------------------------------------------
    def run(self):
        log.info("Sarı thread başladı.")
        scan_interval = self.cfg.thread_scan_interval_sec
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception as e:
                log.exception(f"YellowThread döngü hatası: {e}")
            self._stop.wait(scan_interval)
        log.info("Sarı thread durdu.")
