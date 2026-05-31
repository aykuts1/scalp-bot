"""
🟠 TURUNCU THREAD — Beyaz'ın trend pekiştirici thread'i

Yön: Beyaz ile aynı yön
  Beyaz Short → Turuncu Short
  Beyaz Long  → Turuncu Long

Sarı ile birebir aynı mantık. Tek fark: parent thread Beyaz.

Tablo:
  Beyaz giriş ↔ Beyaz WINRATE arası 6 EŞİT parça
  Bölgeler (Turuncu Short için üstten alta): FLAG, ST1, ST2, ST3, ST4, ST5

Flag (KONUM BAZLI):
  - Fiyat FLAG bölgesindeyse flag açık
  - Yukarı çıkış → flag silinir
  - Aşağı çıkış (ST1+) → işlem açma akışı tetiklenir

İşlem açılışı:
  - Fiyat ST1 cross → işlem açılır
  - Chandelier devreye girer
  - Tablo kurulurken fiyat zaten ST1+ → otomatik açılır

Chandelier:
  - Mesafe = |Beyaz LOSE − Beyaz giriş| / 2
  - En iyi fiyat: ömür boyunca görülen en düşük (Short) / en yüksek (Long)
  - Chandelier çizgisi = en iyi ± mesafe
  - Ters cross → Turuncu kapanır + reentry_line set edilir

Yeniden giriş (chandelier sonrası):
  - reentry_line = chandelier'in takip ettiği en iyi fiyat
  - Flag aranmaz, fiyat reentry_line'ı kâr yönüne cross → yeniden açılır
  - Yeni chandelier aynı mesafe ile sıfırdan
  - Beyaz yaşadığı sürece sınırsız

WINRATE çıkışı:
  - Fiyat Beyaz WINRATE'i cross → Beyaz kapanır → zincir Turuncu'yu da kapatır

Beyaz kapanırsa Turuncu da otomatik kapanır.
Flag bildirimleri Telegram'a atılmaz, sadece raporlarda.
"""
import threading
import logging

from utils import crossed_up, crossed_down

log = logging.getLogger("OrangeThread")


class OrangeTable:
    __slots__ = ("white_trade_id", "white_side", "symbol", "side",
                 "levels", "entry_line", "winrate_line", "lose_line",
                 "chandelier_distance",
                 "flag_open", "current_level", "active_trade",
                 "reentry_line")

    def __init__(self, white_trade, levels, chandelier_distance):
        self.white_trade_id = white_trade.id
        self.white_side = white_trade.side
        self.symbol = white_trade.symbol
        # Turuncu yön Beyaz ile aynı
        self.side = white_trade.side
        self.levels = dict(levels)
        self.entry_line = white_trade.level_lines["ENTRY"]
        self.winrate_line = white_trade.winrate_line
        self.lose_line = white_trade.lose_line
        self.chandelier_distance = chandelier_distance
        self.flag_open = False
        self.current_level = None
        self.active_trade = None
        self.reentry_line = None


class OrangeThread(threading.Thread):

    LEVEL_ORDER = ["ST1", "ST2", "ST3", "ST4", "ST5"]

    def __init__(self, config, data_manager, trade_manager):
        super().__init__(name="OrangeThread", daemon=True)
        self.cfg = config
        self.dm = data_manager
        self.tm = trade_manager
        self._stop = threading.Event()
        self.tables = {}
        self.tables_lock = threading.Lock()

    def stop(self):
        self._stop.set()

    def get_open_flags(self):
        result = []
        with self.tables_lock:
            for tbl in self.tables.values():
                if tbl.flag_open and tbl.active_trade is None:
                    result.append({"symbol": tbl.symbol, "thread": "ORANGE",
                                   "side": tbl.side})
        return result

    # ------------------------------------------------------------------
    # TABLO OLUŞTURMA
    # ------------------------------------------------------------------
    def create_table_for_white(self, white_trade):
        entry = white_trade.level_lines["ENTRY"]
        winrate = white_trade.winrate_line
        lose = white_trade.lose_line

        step = (winrate - entry) / 6.0
        levels = {
            "ST1": entry + step * 1,
            "ST2": entry + step * 2,
            "ST3": entry + step * 3,
            "ST4": entry + step * 4,
            "ST5": entry + step * 5,
        }
        chandelier_distance = abs(lose - entry) / 2.0

        table = OrangeTable(white_trade, levels, chandelier_distance)
        with self.tables_lock:
            self.tables[white_trade.id] = table

        all_lines = {
            "Beyaz Giriş": entry,
            **levels,
            "Beyaz WINRATE": winrate,
            "Chandelier Mesafe": chandelier_distance,
        }
        self.tm.tg.notify_thread_ready(white_trade, "ORANGE", table.side, all_lines)

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
            self.tm.log_flag_event(tbl.symbol, "ORANGE", tbl.side, "OPENED")
        elif zone in self.LEVEL_ORDER:
            tbl.flag_open = True
            opened = self._open_orange(tbl, curr, initial_level=zone)
            if opened:
                self.tm.log_flag_event(tbl.symbol, "ORANGE", tbl.side, "CONVERTED")

    def _find_zone(self, tbl, price):
        entry = tbl.entry_line
        winrate = tbl.winrate_line
        levels = tbl.levels

        if tbl.side == "SHORT":
            # Beyaz Short → tablo aşağı uzanır (entry üstte, winrate altta)
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

    def remove_table_for_white(self, white_trade_id):
        with self.tables_lock:
            tbl = self.tables.pop(white_trade_id, None)
        if not tbl:
            return
        if tbl.flag_open and tbl.active_trade is None:
            self.tm.log_flag_event(tbl.symbol, "ORANGE", tbl.side, "DELETED")

    # ------------------------------------------------------------------
    # SCAN
    # ------------------------------------------------------------------
    def scan(self):
        self._cleanup_dead_tables()

        with self.tables_lock:
            tbls = list(self.tables.values())

        for tbl in tbls:
            if self._stop.is_set():
                return
            try:
                self._tick_table(tbl)
            except Exception as e:
                log.exception(f"OrangeThread tick hatası ({tbl.symbol}): {e}")

    def _cleanup_dead_tables(self):
        """Sadece bot hafızasındaki Beyaz'a bakar."""
        with self.tables_lock:
            ids_snapshot = list(self.tables.items())

        for white_id, tbl in ids_snapshot:
            white = self.tm.slots.get_white_for(tbl.symbol, tbl.white_side)
            white_missing = (white is None or white.id != white_id or white.closed)

            if white_missing:
                if tbl.active_trade and not tbl.active_trade.closed:
                    curr = self.dm.get_last_price(tbl.symbol)
                    try:
                        self.tm.close_trade(tbl.active_trade, "TURUNCU BEYAZ KAPANDI", curr)
                    except Exception as e:
                        log.error(f"Turuncu acil kapatma hatası ({tbl.symbol}): {e}")
                if tbl.flag_open and tbl.active_trade is None:
                    self.tm.log_flag_event(tbl.symbol, "ORANGE", tbl.side, "DELETED")
                with self.tables_lock:
                    self.tables.pop(white_id, None)

    def _tick_table(self, tbl):
        prev, curr = self.dm.get_price_pair(tbl.symbol)
        if prev is None or curr is None:
            return

        if tbl.active_trade and tbl.active_trade.closed:
            tbl.active_trade = None
            tbl.current_level = None

        # AÇIK İŞLEM VARSA: chandelier güncelle + çıkış + seviye
        if tbl.active_trade and not tbl.active_trade.closed:
            self._update_chandelier(tbl, curr)

            cl = tbl.active_trade.chandelier_line
            if cl is not None:
                if tbl.side == "SHORT":
                    if crossed_up(prev, curr, cl):
                        self._chandelier_exit(tbl, curr)
                        return
                else:
                    if crossed_down(prev, curr, cl):
                        self._chandelier_exit(tbl, curr)
                        return

            # Seviye değişimi (iki yönlü)
            new_zone = self._find_zone(tbl, curr)
            if new_zone and new_zone in self.LEVEL_ORDER and new_zone != tbl.current_level:
                tbl.current_level = new_zone
                tbl.active_trade.current_level = new_zone
                try:
                    cur_idx = self.LEVEL_ORDER.index(new_zone)
                    high_idx = (self.LEVEL_ORDER.index(tbl.active_trade.highest_level)
                                if tbl.active_trade.highest_level in self.LEVEL_ORDER
                                else -1)
                    if cur_idx > high_idx:
                        tbl.active_trade.highest_level = new_zone
                except ValueError:
                    pass
                self.tm.tg.notify_level_change(tbl.active_trade, new_zone)
            return

        # AÇIK İŞLEM YOK

        # B1) reentry_line varsa (chandelier sonrası)
        if tbl.reentry_line is not None:
            if tbl.side == "SHORT":
                if crossed_down(prev, curr, tbl.reentry_line):
                    opened = self._open_orange(tbl, curr, initial_level=None,
                                               is_reentry=True)
                    if opened:
                        tbl.reentry_line = None
                        return
            else:
                if crossed_up(prev, curr, tbl.reentry_line):
                    opened = self._open_orange(tbl, curr, initial_level=None,
                                               is_reentry=True)
                    if opened:
                        tbl.reentry_line = None
                        return
            return

        # B2) Klasik akış: konum bazlı flag + ST1 cross
        zone = self._find_zone(tbl, curr)

        if zone == "FLAG":
            if not tbl.flag_open:
                tbl.flag_open = True
                self.tm.log_flag_event(tbl.symbol, "ORANGE", tbl.side, "OPENED")
        else:
            if tbl.flag_open:
                tbl.flag_open = False
                if zone in self.LEVEL_ORDER:
                    self.tm.log_flag_event(tbl.symbol, "ORANGE", tbl.side, "CONVERTED")
                else:
                    self.tm.log_flag_event(tbl.symbol, "ORANGE", tbl.side, "DELETED")

        if zone in self.LEVEL_ORDER:
            st1 = tbl.levels["ST1"]
            if tbl.side == "SHORT":
                if crossed_down(prev, curr, st1):
                    self._open_orange(tbl, curr, initial_level=zone)
            else:
                if crossed_up(prev, curr, st1):
                    self._open_orange(tbl, curr, initial_level=zone)

    def _update_chandelier(self, tbl, curr):
        trade = tbl.active_trade
        if trade.chandelier_distance is None:
            trade.chandelier_distance = tbl.chandelier_distance

        if trade.chandelier_best_price is None:
            trade.chandelier_best_price = curr
        else:
            if tbl.side == "SHORT":
                if curr < trade.chandelier_best_price:
                    trade.chandelier_best_price = curr
            else:
                if curr > trade.chandelier_best_price:
                    trade.chandelier_best_price = curr

        if tbl.side == "SHORT":
            trade.chandelier_line = trade.chandelier_best_price + trade.chandelier_distance
        else:
            trade.chandelier_line = trade.chandelier_best_price - trade.chandelier_distance

    def _chandelier_exit(self, tbl, curr):
        trade = tbl.active_trade
        best = trade.chandelier_best_price

        self.tm.close_trade(trade, "TURUNCU CHANDELIER EXIT", curr)
        tbl.reentry_line = best
        tbl.active_trade = None
        tbl.current_level = None
        tbl.flag_open = False

    def _open_orange(self, tbl, entry_price, initial_level=None, is_reentry=False):
        white_trade = self.tm.slots.get_white_for(tbl.symbol, tbl.white_side)
        if not white_trade or white_trade.id != tbl.white_trade_id or white_trade.closed:
            return False

        level_lines = {
            "ENTRY": tbl.entry_line,
            **tbl.levels,
            "WINRATE": tbl.winrate_line,
        }

        if initial_level is None:
            zone = self._find_zone(tbl, entry_price)
            initial_level = zone if zone in self.LEVEL_ORDER else "ST1"

        trade = self.tm.open_trade(
            symbol=tbl.symbol, side=tbl.side, thread="ORANGE",
            entry_price=entry_price,
            lose_line=None,
            winrate_line=tbl.winrate_line,
            level_lines=level_lines,
            current_level=initial_level,
            parent_white_trade=white_trade,
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

    def run(self):
        log.info("Turuncu thread başladı.")
        scan_interval = self.cfg.thread_scan_interval_sec
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception as e:
                log.exception(f"OrangeThread döngü hatası: {e}")
            self._stop.wait(scan_interval)
        log.info("Turuncu thread durdu.")
