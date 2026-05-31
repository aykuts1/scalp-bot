"""
🔵 MAVİ THREAD — Yeni mantık (Sarı'ya benzer ama hedge)

Yön:
  Kırmızı Short → Mavi Long
  Kırmızı Long  → Mavi Short

Tablo:
  Kırmızı giriş çizgisi ↔ Kırmızı LOSE arası 5 EŞİT parça
  Bölgeler (Mavi Long için aşağıdan yukarı): FLAG, ST1, ST2, ST3, ST4
  6 çizgi: Kırmızı giriş → ST1 → ST2 → ST3 → ST4 → Kırmızı LOSE

Flag mantığı (KONUM BAZLI):
  - Her tarama: fiyat FLAG bölgesindeyse flag açık, değilse kapalı.
  - Aşağı çıkış (Mavi Long için Kırmızı giriş altına) → flag silinir
  - Yukarı çıkış (ST1 bölgesine) → işlem açma akışı tetiklenir

İşlem açılışı:
  - Fiyat ST1 giriş çizgisini cross → işlem açılır, seviye = ST1
  - Tablo kurulurken fiyat zaten ST1+ bölgesindeyse → otomatik açılır

Seviye geçişi:
  - İki yönlü değişir (Sarı/Turuncu gibi)
  - Sadece bilgi/telemetri, çıkışı ETKİLEMEZ
  - Her geçişte Telegram bildirimi atılır

Kapanış (3 yol):
  1. Fiyat Kırmızı giriş çizgisini ters yöne cross (Mavi Long için aşağı)
     → Mavi kendi başına kapanır
  2. Fiyat Kırmızı LOSE'u Mavi kâr yönüne cross → Kırmızı kapanır
     (close_red_and_dependents zincirinden Mavi de kapanır — Mavi WINRATE)
  3. Kırmızı herhangi bir sebepten kapandı → Mavi de kapanır (zincir)

Yeniden giriş:
  - Mavi kapanınca tablo silinmez (Kırmızı yaşadığı sürece).
  - flag_open, current_level sıfırlanır → yeniden açılış akışı tekrar başlar.
  - Sınırsız tekrar.

Flag bildirimleri Telegram'a atılmaz — sadece raporlarda görünür.

Hızlı tarama: 1 sn (config'den).
"""
import threading
import logging

from utils import crossed_up, crossed_down

log = logging.getLogger("BlueThread")


class BlueTable:
    __slots__ = ("red_trade_id", "red_side", "symbol", "side",
                 "levels", "entry_line", "lose_line",
                 "flag_open", "current_level", "active_trade")

    def __init__(self, red_trade, levels):
        self.red_trade_id = red_trade.id
        self.red_side = red_trade.side
        self.symbol = red_trade.symbol
        # Mavi yön Kırmızı'nın tersi
        self.side = "LONG" if red_trade.side == "SHORT" else "SHORT"
        self.levels = dict(levels)  # ST1..ST4 giriş çizgileri
        self.entry_line = red_trade.level_lines["ENTRY"]  # Kırmızı işlem giriş çizgisi
        self.lose_line = red_trade.lose_line  # Kırmızı LOSE
        self.flag_open = False
        self.current_level = None  # işlem yokken None
        self.active_trade = None


class BlueThread(threading.Thread):

    LEVEL_ORDER = ["ST1", "ST2", "ST3", "ST4"]

    def __init__(self, config, data_manager, trade_manager):
        super().__init__(name="BlueThread", daemon=True)
        self.cfg = config
        self.dm = data_manager
        self.tm = trade_manager
        self._stop = threading.Event()
        # red_trade_id -> BlueTable
        self.tables = {}
        self.tables_lock = threading.Lock()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    # OKUMA HELPERS (raporlar için)
    # ------------------------------------------------------------------
    def get_open_flags(self):
        """Açık Mavi flag'lerini döndür (raporlar/status için)."""
        result = []
        with self.tables_lock:
            for tbl in self.tables.values():
                if tbl.flag_open and tbl.active_trade is None:
                    result.append({"symbol": tbl.symbol, "thread": "BLUE",
                                   "side": tbl.side})
        return result

    # ------------------------------------------------------------------
    # TABLO OLUŞTURMA
    # ------------------------------------------------------------------
    def create_table_for_red(self, red_trade):
        """
        Kırmızı işlem açıldığında Mavi tablosunu kurar.
        Tablo: Kırmızı giriş ↔ Kırmızı LOSE arası 5 eşit parça.
        """
        entry = red_trade.level_lines["ENTRY"]
        lose = red_trade.lose_line
        step = (lose - entry) / 5.0
        levels = {
            "ST1": entry + step * 1,
            "ST2": entry + step * 2,
            "ST3": entry + step * 3,
            "ST4": entry + step * 4,
        }

        table = BlueTable(red_trade, levels)
        with self.tables_lock:
            self.tables[red_trade.id] = table

        # Telegram bildirim
        all_lines = {
            "Kırmızı Giriş": entry,
            **levels,
            "Kırmızı LOSE": lose,
        }
        self.tm.tg.notify_thread_ready(red_trade, "BLUE", table.side, all_lines)

        # Tablo kurulurken fiyat durumunu kontrol et
        self._check_initial_position(table)

        return table

    def _check_initial_position(self, tbl):
        """Tablo kurulduğunda fiyat hangi bölgede?"""
        curr = self.dm.get_last_price(tbl.symbol)
        if curr is None:
            return

        zone = self._find_zone(tbl, curr)
        if zone is None:
            return

        if zone == "FLAG":
            tbl.flag_open = True
            self.tm.log_flag_event(tbl.symbol, "BLUE", tbl.side, "OPENED")
        elif zone in self.LEVEL_ORDER:
            # Otomatik açılış
            tbl.flag_open = True
            opened = self._open_blue(tbl, curr, initial_level=zone)
            if opened:
                self.tm.log_flag_event(tbl.symbol, "BLUE", tbl.side, "CONVERTED")

    def remove_table_for_red(self, red_trade_id):
        with self.tables_lock:
            tbl = self.tables.pop(red_trade_id, None)
        if not tbl:
            return
        if tbl.flag_open and tbl.active_trade is None:
            self.tm.log_flag_event(tbl.symbol, "BLUE", tbl.side, "DELETED")

    # ------------------------------------------------------------------
    # BÖLGE TESPİTİ
    # ------------------------------------------------------------------
    def _find_zone(self, tbl, price):
        """
        Fiyat hangi bölgede?
        Dönüş: "FLAG", "ST1".."ST4" veya None (tablo dışı).
        """
        entry = tbl.entry_line
        lose = tbl.lose_line
        levels = tbl.levels

        if tbl.side == "LONG":
            # Kırmızı Short, Mavi Long → tablo yukarı uzanır (entry altta, lose üstte)
            if price < entry or price > lose:
                return None
            if price < levels["ST1"]:
                return "FLAG"
            if price < levels["ST2"]:
                return "ST1"
            if price < levels["ST3"]:
                return "ST2"
            if price < levels["ST4"]:
                return "ST3"
            return "ST4"
        else:
            # Kırmızı Long, Mavi Short → tablo aşağı uzanır (entry üstte, lose altta)
            if price > entry or price < lose:
                return None
            if price > levels["ST1"]:
                return "FLAG"
            if price > levels["ST2"]:
                return "ST1"
            if price > levels["ST3"]:
                return "ST2"
            if price > levels["ST4"]:
                return "ST3"
            return "ST4"

    # ------------------------------------------------------------------
    # SCAN — her 1 sn'de çağrılır
    # ------------------------------------------------------------------
    def scan(self):
        # Kırmızı'sı bot hafızasında kapalı/yok olan tabloları temizle
        self._cleanup_dead_tables()

        with self.tables_lock:
            tbls = list(self.tables.values())

        for tbl in tbls:
            if self._stop.is_set():
                return
            try:
                self._tick_table(tbl)
            except Exception as e:
                log.exception(f"BlueThread tick hatası ({tbl.symbol}): {e}")

    def _cleanup_dead_tables(self):
        """
        Sadece bot hafızasındaki Kırmızı'ya bakar (Bybit önbelleğine değil).
        Bybit önbelleği kutu birleşmesi durumunda yanıltıcı olabilir.
        """
        with self.tables_lock:
            ids_snapshot = list(self.tables.items())

        for red_id, tbl in ids_snapshot:
            red = self.tm.slots.get_red_for(tbl.symbol, tbl.red_side)
            red_missing = (red is None or red.id != red_id or red.closed)

            if red_missing:
                # Önce açık Mavi varsa kapat
                if tbl.active_trade and not tbl.active_trade.closed:
                    curr = self.dm.get_last_price(tbl.symbol)
                    try:
                        self.tm.close_trade(tbl.active_trade, "MAVİ KIRMIZI KAPANDI", curr)
                    except Exception as e:
                        log.error(f"Mavi acil kapatma hatası ({tbl.symbol}): {e}")
                # Flag açıksa raporda DELETED olarak işaretle
                if tbl.flag_open and tbl.active_trade is None:
                    self.tm.log_flag_event(tbl.symbol, "BLUE", tbl.side, "DELETED")
                with self.tables_lock:
                    self.tables.pop(red_id, None)

    # ------------------------------------------------------------------
    # TEK TABLO TICK
    # ------------------------------------------------------------------
    def _tick_table(self, tbl):
        prev, curr = self.dm.get_price_pair(tbl.symbol)
        if prev is None or curr is None:
            return

        # Aktif işlem kapanmışsa state'i temizle (yeniden giriş için hazırla)
        if tbl.active_trade and tbl.active_trade.closed:
            tbl.active_trade = None
            tbl.current_level = None
            tbl.flag_open = False

        # ----- A) AÇIK İŞLEM VARSA: çıkış kontrolü + seviye telemetri -----
        if tbl.active_trade and not tbl.active_trade.closed:
            self._handle_active_trade(tbl, prev, curr)
            return

        # ----- B) AÇIK İŞLEM YOK: konum bazlı flag + ST1 cross ile açılış -----
        zone = self._find_zone(tbl, curr)

        # Konum bazlı flag güncelleme
        if zone == "FLAG":
            if not tbl.flag_open:
                tbl.flag_open = True
                self.tm.log_flag_event(tbl.symbol, "BLUE", tbl.side, "OPENED")
        else:
            if tbl.flag_open:
                tbl.flag_open = False
                if zone in self.LEVEL_ORDER:
                    self.tm.log_flag_event(tbl.symbol, "BLUE", tbl.side, "CONVERTED")
                else:
                    self.tm.log_flag_event(tbl.symbol, "BLUE", tbl.side, "DELETED")

        # İşlem açılışı: ST1 giriş çizgisi cross
        if zone in self.LEVEL_ORDER:
            st1 = tbl.levels["ST1"]
            if tbl.side == "LONG":
                # Long Mavi: fiyat yukarı yönlü ST1 cross (zarar tarafına ilerliyor)
                if crossed_up(prev, curr, st1):
                    self._open_blue(tbl, curr, initial_level=zone)
            else:
                # Short Mavi: fiyat aşağı yönlü ST1 cross
                if crossed_down(prev, curr, st1):
                    self._open_blue(tbl, curr, initial_level=zone)

    # ------------------------------------------------------------------
    # AÇIK İŞLEM YÖNETİMİ
    # ------------------------------------------------------------------
    def _handle_active_trade(self, tbl, prev, curr):
        trade = tbl.active_trade
        if trade is None or trade.closed:
            return

        # 1) Kırmızı giriş çizgisini ters yön cross → Mavi kendi başına kapanır
        if tbl.side == "LONG":
            # Long Mavi için "ters yön" = aşağı (kâr çevrilip K.giriş'in altına geçti)
            if crossed_down(prev, curr, tbl.entry_line):
                self.tm.close_trade(trade, "MAVİ KIRMIZI GİRİŞ EXIT", curr)
                tbl.active_trade = None
                tbl.current_level = None
                tbl.flag_open = False
                return
        else:
            # Short Mavi için "ters yön" = yukarı
            if crossed_up(prev, curr, tbl.entry_line):
                self.tm.close_trade(trade, "MAVİ KIRMIZI GİRİŞ EXIT", curr)
                tbl.active_trade = None
                tbl.current_level = None
                tbl.flag_open = False
                return

        # 2) Kırmızı LOSE'u kâr yönüne cross → Kırmızı'yı kapat (zincir Mavi'yi de kapatır)
        red_trade = self.tm.slots.get_red_for(tbl.symbol, tbl.red_side)
        if red_trade and not red_trade.closed:
            if tbl.side == "LONG":
                # Long Mavi kâr yönü = yukarı (Kırmızı Short için LOSE de yukarıda)
                if crossed_up(prev, curr, tbl.lose_line):
                    self.tm.close_red_and_dependents(
                        red_trade, "KIRMIZI LOSE (MAVİ WINRATE)", curr)
                    return
            else:
                # Short Mavi kâr yönü = aşağı
                if crossed_down(prev, curr, tbl.lose_line):
                    self.tm.close_red_and_dependents(
                        red_trade, "KIRMIZI LOSE (MAVİ WINRATE)", curr)
                    return

        # 3) Seviye telemetrisi (iki yönlü, çıkışı etkilemez)
        new_zone = self._find_zone(tbl, curr)
        if new_zone and new_zone in self.LEVEL_ORDER and new_zone != tbl.current_level:
            tbl.current_level = new_zone
            trade.current_level = new_zone
            # highest_level sadece ileri ilerler
            try:
                cur_idx = self.LEVEL_ORDER.index(new_zone)
                high_idx = (self.LEVEL_ORDER.index(trade.highest_level)
                            if trade.highest_level in self.LEVEL_ORDER
                            else -1)
                if cur_idx > high_idx:
                    trade.highest_level = new_zone
            except ValueError:
                pass
            self.tm.tg.notify_level_change(trade, new_zone)

    # ------------------------------------------------------------------
    # AÇILIŞ
    # ------------------------------------------------------------------
    def _open_blue(self, tbl, entry_price, initial_level=None):
        red_trade = self.tm.slots.get_red_for(tbl.symbol, tbl.red_side)
        if not red_trade or red_trade.id != tbl.red_trade_id or red_trade.closed:
            return False

        level_lines = {
            "ENTRY": tbl.entry_line,
            **tbl.levels,
            "LOSE": tbl.lose_line,
        }

        # Seviye fiyatın bölgesine göre
        if initial_level is None:
            zone = self._find_zone(tbl, entry_price)
            initial_level = zone if zone in self.LEVEL_ORDER else "ST1"

        trade = self.tm.open_trade(
            symbol=tbl.symbol, side=tbl.side, thread="BLUE",
            entry_price=entry_price,
            lose_line=tbl.lose_line,
            winrate_line=tbl.lose_line,  # Mavi için "WINRATE" = Kırmızı LOSE
            level_lines=level_lines,
            current_level=initial_level,
            parent_red_trade=red_trade,
        )
        if not trade:
            return False

        tbl.active_trade = trade
        tbl.current_level = initial_level
        tbl.flag_open = False

        log.info(f"[{tbl.symbol}] MAVİ {tbl.side} açıldı @ {trade.entry_price} "
                 f"seviye={initial_level}")
        return True

    # ------------------------------------------------------------------
    # RUN
    # ------------------------------------------------------------------
    def run(self):
        log.info("Mavi thread başladı.")
        scan_interval = self.cfg.thread_scan_interval_sec
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception as e:
                log.exception(f"BlueThread döngü hatası: {e}")
            self._stop.wait(scan_interval)
        log.info("Mavi thread durdu.")
