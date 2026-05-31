"""
🟣 MOR THREAD — Beyaz'ın hedge thread'i

Yön:
  Beyaz Short → Mor Long
  Beyaz Long  → Mor Short

Tablo:
  Beyaz giriş çizgisi ↔ Beyaz LOSE arası 5 EŞİT parça
  Bölgeler (Mor Long için aşağıdan yukarı): FLAG, ST1, ST2, ST3, ST4

Yeni Mavi ile birebir aynı mantık. Tek fark: parent thread Beyaz.

Flag mantığı (KONUM BAZLI):
  - Fiyat FLAG bölgesindeyse flag açık, değilse kapalı.

İşlem açılışı:
  - Fiyat ST1 cross → işlem açılır, seviye = ST1
  - Tablo kurulurken fiyat zaten ST1+ bölgesindeyse → otomatik açılır

Seviye geçişi: iki yönlü, sadece telemetri.

Kapanış (3 yol):
  1. Fiyat Beyaz giriş çizgisini ters yöne cross → Mor kendi başına kapanır
  2. Fiyat Beyaz LOSE'u Mor kâr yönüne cross → Beyaz kapanır (zincir Mor'u da kapatır)
  3. Beyaz herhangi bir sebepten kapandı → Mor da kapanır

Yeniden giriş: Beyaz yaşadığı sürece sınırsız.
Flag bildirimleri Telegram'a atılmaz, sadece raporlarda.
"""
import threading
import logging

from utils import crossed_up, crossed_down

log = logging.getLogger("PurpleThread")


class PurpleTable:
    __slots__ = ("white_trade_id", "white_side", "symbol", "side",
                 "levels", "entry_line", "lose_line",
                 "flag_open", "current_level", "active_trade")

    def __init__(self, white_trade, levels):
        self.white_trade_id = white_trade.id
        self.white_side = white_trade.side
        self.symbol = white_trade.symbol
        # Mor yön Beyaz'ın tersi
        self.side = "LONG" if white_trade.side == "SHORT" else "SHORT"
        self.levels = dict(levels)
        self.entry_line = white_trade.level_lines["ENTRY"]
        self.lose_line = white_trade.lose_line
        self.flag_open = False
        self.current_level = None
        self.active_trade = None


class PurpleThread(threading.Thread):

    LEVEL_ORDER = ["ST1", "ST2", "ST3", "ST4"]

    def __init__(self, config, data_manager, trade_manager):
        super().__init__(name="PurpleThread", daemon=True)
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
                    result.append({"symbol": tbl.symbol, "thread": "PURPLE",
                                   "side": tbl.side})
        return result

    # ------------------------------------------------------------------
    # TABLO OLUŞTURMA
    # ------------------------------------------------------------------
    def create_table_for_white(self, white_trade):
        entry = white_trade.level_lines["ENTRY"]
        lose = white_trade.lose_line
        step = (lose - entry) / 5.0
        levels = {
            "ST1": entry + step * 1,
            "ST2": entry + step * 2,
            "ST3": entry + step * 3,
            "ST4": entry + step * 4,
        }

        table = PurpleTable(white_trade, levels)
        with self.tables_lock:
            self.tables[white_trade.id] = table

        all_lines = {
            "Beyaz Giriş": entry,
            **levels,
            "Beyaz LOSE": lose,
        }
        self.tm.tg.notify_thread_ready(white_trade, "PURPLE", table.side, all_lines)

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
            self.tm.log_flag_event(tbl.symbol, "PURPLE", tbl.side, "OPENED")
        elif zone in self.LEVEL_ORDER:
            tbl.flag_open = True
            opened = self._open_purple(tbl, curr, initial_level=zone)
            if opened:
                self.tm.log_flag_event(tbl.symbol, "PURPLE", tbl.side, "CONVERTED")

    def remove_table_for_white(self, white_trade_id):
        with self.tables_lock:
            tbl = self.tables.pop(white_trade_id, None)
        if not tbl:
            return
        if tbl.flag_open and tbl.active_trade is None:
            self.tm.log_flag_event(tbl.symbol, "PURPLE", tbl.side, "DELETED")

    # ------------------------------------------------------------------
    # BÖLGE TESPİTİ
    # ------------------------------------------------------------------
    def _find_zone(self, tbl, price):
        entry = tbl.entry_line
        lose = tbl.lose_line
        levels = tbl.levels

        if tbl.side == "LONG":
            # Beyaz Short, Mor Long → entry altta, lose üstte
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
            # Beyaz Long, Mor Short → entry üstte, lose altta
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
                log.exception(f"PurpleThread tick hatası ({tbl.symbol}): {e}")

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
                        self.tm.close_trade(tbl.active_trade, "MOR BEYAZ KAPANDI", curr)
                    except Exception as e:
                        log.error(f"Mor acil kapatma hatası ({tbl.symbol}): {e}")
                if tbl.flag_open and tbl.active_trade is None:
                    self.tm.log_flag_event(tbl.symbol, "PURPLE", tbl.side, "DELETED")
                with self.tables_lock:
                    self.tables.pop(white_id, None)

    def _tick_table(self, tbl):
        prev, curr = self.dm.get_price_pair(tbl.symbol)
        if prev is None or curr is None:
            return

        if tbl.active_trade and tbl.active_trade.closed:
            tbl.active_trade = None
            tbl.current_level = None
            tbl.flag_open = False

        # AÇIK İŞLEM VARSA
        if tbl.active_trade and not tbl.active_trade.closed:
            self._handle_active_trade(tbl, prev, curr)
            return

        # AÇIK İŞLEM YOK
        zone = self._find_zone(tbl, curr)

        # Konum bazlı flag
        if zone == "FLAG":
            if not tbl.flag_open:
                tbl.flag_open = True
                self.tm.log_flag_event(tbl.symbol, "PURPLE", tbl.side, "OPENED")
        else:
            if tbl.flag_open:
                tbl.flag_open = False
                if zone in self.LEVEL_ORDER:
                    self.tm.log_flag_event(tbl.symbol, "PURPLE", tbl.side, "CONVERTED")
                else:
                    self.tm.log_flag_event(tbl.symbol, "PURPLE", tbl.side, "DELETED")

        # ST1 cross → açılış
        if zone in self.LEVEL_ORDER:
            st1 = tbl.levels["ST1"]
            if tbl.side == "LONG":
                if crossed_up(prev, curr, st1):
                    self._open_purple(tbl, curr, initial_level=zone)
            else:
                if crossed_down(prev, curr, st1):
                    self._open_purple(tbl, curr, initial_level=zone)

    def _handle_active_trade(self, tbl, prev, curr):
        trade = tbl.active_trade
        if trade is None or trade.closed:
            return

        # 1) Beyaz giriş çizgisini ters yön cross → Mor kapanır
        if tbl.side == "LONG":
            if crossed_down(prev, curr, tbl.entry_line):
                self.tm.close_trade(trade, "MOR BEYAZ GİRİŞ EXIT", curr)
                tbl.active_trade = None
                tbl.current_level = None
                tbl.flag_open = False
                return
        else:
            if crossed_up(prev, curr, tbl.entry_line):
                self.tm.close_trade(trade, "MOR BEYAZ GİRİŞ EXIT", curr)
                tbl.active_trade = None
                tbl.current_level = None
                tbl.flag_open = False
                return

        # 2) Beyaz LOSE'u kâr yönüne cross → Beyaz'ı kapat (zincir)
        white_trade = self.tm.slots.get_white_for(tbl.symbol, tbl.white_side)
        if white_trade and not white_trade.closed:
            if tbl.side == "LONG":
                if crossed_up(prev, curr, tbl.lose_line):
                    self.tm.close_white_and_dependents(
                        white_trade, "BEYAZ LOSE (MOR WINRATE)", curr)
                    return
            else:
                if crossed_down(prev, curr, tbl.lose_line):
                    self.tm.close_white_and_dependents(
                        white_trade, "BEYAZ LOSE (MOR WINRATE)", curr)
                    return

        # 3) Seviye telemetrisi
        new_zone = self._find_zone(tbl, curr)
        if new_zone and new_zone in self.LEVEL_ORDER and new_zone != tbl.current_level:
            tbl.current_level = new_zone
            trade.current_level = new_zone
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

    def _open_purple(self, tbl, entry_price, initial_level=None):
        white_trade = self.tm.slots.get_white_for(tbl.symbol, tbl.white_side)
        if not white_trade or white_trade.id != tbl.white_trade_id or white_trade.closed:
            return False

        level_lines = {
            "ENTRY": tbl.entry_line,
            **tbl.levels,
            "LOSE": tbl.lose_line,
        }

        if initial_level is None:
            zone = self._find_zone(tbl, entry_price)
            initial_level = zone if zone in self.LEVEL_ORDER else "ST1"

        trade = self.tm.open_trade(
            symbol=tbl.symbol, side=tbl.side, thread="PURPLE",
            entry_price=entry_price,
            lose_line=tbl.lose_line,
            winrate_line=tbl.lose_line,
            level_lines=level_lines,
            current_level=initial_level,
            parent_white_trade=white_trade,
        )
        if not trade:
            return False

        tbl.active_trade = trade
        tbl.current_level = initial_level
        tbl.flag_open = False

        log.info(f"[{tbl.symbol}] MOR {tbl.side} açıldı @ {trade.entry_price} "
                 f"seviye={initial_level}")
        return True

    def run(self):
        log.info("Mor thread başladı.")
        scan_interval = self.cfg.thread_scan_interval_sec
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception as e:
                log.exception(f"PurpleThread döngü hatası: {e}")
            self._stop.wait(scan_interval)
        log.info("Mor thread durdu.")
