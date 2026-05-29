"""
🟡 SARI THREAD

Trend yönünde kar maksimize.
- Her Kırmızı Short → Sarı Short tablosu
- Her Kırmızı Long → Sarı Long tablosu (aynı yön)
- Kırmızı kapanınca bağlı Sarı da kapanır

Seviye Hesaplama:
- Kırmızı giriş ile Kırmızı WINRATE arasına 12 eşit parça
- WAIT = Kırmızı giriş çizgisi
- FLAG = 1. parça
- ST1..ST10 = 2.-11. parçalar (sıralama: WAIT → FLAG → ST1 → ST2 → ... → ST10)
- Sonra Kırmızı WINRATE hedefi

Açılış (cross şartı VAR):
- Sarı Short: FLAG çizgisini aşağı cross → Flag açılır
            ST1'i aşağı cross → işlem açılır
- Sarı Long tam ters simetri

Çıkış (cross şartı VAR):
- ST1 → FLAG ters cross
- STn → STn-1 ters cross
- Her seviyede Kırmızı WINRATE cross → SARI [SEVİYE] WINRATE EXIT
"""
import threading
import time
import logging

from utils import crossed_up, crossed_down

log = logging.getLogger("YellowThread")


class YellowTable:
    __slots__ = ("red_trade_id", "symbol", "side", "levels", "winrate_line",
                 "flag_open", "current_level", "highest_level", "active_trade",
                 "last_exit_line_name")
    
    def __init__(self, red_trade, levels):
        self.red_trade_id = red_trade.id
        self.symbol = red_trade.symbol
        # Sarı yön Kırmızı ile aynı
        self.side = red_trade.side
        self.levels = dict(levels)
        self.winrate_line = red_trade.winrate_line
        self.flag_open = False
        self.current_level = "WAIT"
        self.highest_level = "WAIT"
        self.active_trade = None
        self.last_exit_line_name = None


class YellowThread(threading.Thread):
    
    LEVEL_ORDER = ["WAIT", "FLAG", "ST1", "ST2", "ST3", "ST4", "ST5",
                   "ST6", "ST7", "ST8", "ST9", "ST10"]
    EXIT_LINE_FOR_LEVEL = {
        "ST1": "FLAG",
        "ST2": "ST1",
        "ST3": "ST2",
        "ST4": "ST3",
        "ST5": "ST4",
        "ST6": "ST5",
        "ST7": "ST6",
        "ST8": "ST7",
        "ST9": "ST8",
        "ST10": "ST9",
    }
    
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
    def create_table_for_red(self, red_trade):
        entry = red_trade.level_lines["ENTRY"]
        winrate = red_trade.level_lines["WINRATE"]
        # 12 eşit parça → 12 ara çizgi yani WAIT + FLAG + ST1..ST10 = 12 seviye
        # Entry ile Winrate arası bölünür. Her seviye 1 parça uzakta.
        # WAIT = Entry
        # FLAG = Entry + 1*step  (Short ise step negatif)
        # STn = Entry + (n+1)*step
        # Toplam 12 seviye + 1 hedef (winrate) = 12 step
        step = (winrate - entry) / 12.0
        
        levels = {
            "WAIT": entry,
            "FLAG": entry + step * 1,
        }
        for i in range(1, 11):
            levels[f"ST{i}"] = entry + step * (i + 1)
        
        table = YellowTable(red_trade, levels)
        with self.tables_lock:
            self.tables[red_trade.id] = table
        
        self.tm.tg.notify_thread_ready(red_trade, "YELLOW", levels)
        return table
    
    def remove_table_for_red(self, red_trade_id):
        with self.tables_lock:
            tbl = self.tables.pop(red_trade_id, None)
        if not tbl:
            return
        if tbl.active_trade and not tbl.active_trade.closed:
            self.tm.close_trade(tbl.active_trade, "KIRMIZI KAPANDI (TABLO SİLİNDİ)")
        if tbl.flag_open:
            self.tm.log_flag_event(tbl.symbol, "YELLOW", tbl.side, "DELETED")
    
    # ------------------------------------------------------------------
    def scan(self):
        with self.tables_lock:
            ids_to_remove = []
            for red_id in self.tables:
                if self.tm.slots.get_red_link(red_id) is None:
                    ids_to_remove.append(red_id)
            for rid in ids_to_remove:
                tbl = self.tables.pop(rid, None)
                if tbl and tbl.active_trade and not tbl.active_trade.closed:
                    self.tm.close_trade(tbl.active_trade, "KIRMIZI YOK")
        
        with self.tables_lock:
            tbls = list(self.tables.values())
        
        for tbl in tbls:
            if self._stop.is_set():
                return
            self._tick_table(tbl)
    
    def _tick_table(self, tbl):
        symbol = tbl.symbol
        prev = self.dm.get_prev_price(symbol)
        curr = self.dm.get_last_price(symbol)
        if prev is None or curr is None:
            return
        
        side = tbl.side
        levels = tbl.levels
        
        # 0) WINRATE cross → acil çıkış
        if tbl.active_trade and not tbl.active_trade.closed:
            if side == "SHORT":
                if crossed_down(prev, curr, tbl.winrate_line):
                    exit_name = f"SARI {tbl.current_level} WINRATE EXIT"
                    self.tm.close_trade(tbl.active_trade, exit_name, curr)
                    self._on_trade_closed(tbl)
                    return
            else:  # LONG
                if crossed_up(prev, curr, tbl.winrate_line):
                    exit_name = f"SARI {tbl.current_level} WINRATE EXIT"
                    self.tm.close_trade(tbl.active_trade, exit_name, curr)
                    self._on_trade_closed(tbl)
                    return
        
        # 1) FLAG ACMA / SILME
        flag_line = levels["FLAG"]
        if not tbl.flag_open and tbl.active_trade is None:
            if side == "SHORT":
                if crossed_down(prev, curr, flag_line):
                    tbl.flag_open = True
                    tbl.current_level = "FLAG"
                    self.tm.log_flag_event(symbol, "YELLOW", side, "OPENED")
                    self.tm.tg.notify_flag(symbol, "YELLOW", side, "OPENED")
            else:
                if crossed_up(prev, curr, flag_line):
                    tbl.flag_open = True
                    tbl.current_level = "FLAG"
                    self.tm.log_flag_event(symbol, "YELLOW", side, "OPENED")
                    self.tm.tg.notify_flag(symbol, "YELLOW", side, "OPENED")
        elif tbl.flag_open and tbl.active_trade is None:
            # Flag silme: ters cross
            if side == "SHORT":
                if crossed_up(prev, curr, flag_line):
                    tbl.flag_open = False
                    tbl.current_level = "WAIT"
                    self.tm.log_flag_event(symbol, "YELLOW", side, "DELETED")
                    self.tm.tg.notify_flag(symbol, "YELLOW", side, "DELETED")
                    return
            else:
                if crossed_down(prev, curr, flag_line):
                    tbl.flag_open = False
                    tbl.current_level = "WAIT"
                    self.tm.log_flag_event(symbol, "YELLOW", side, "DELETED")
                    self.tm.tg.notify_flag(symbol, "YELLOW", side, "DELETED")
                    return
        
        # 2) İŞLEM AÇMA — Flag varken ST1 cross
        if tbl.flag_open and tbl.active_trade is None:
            st1_line = levels["ST1"]
            opened = False
            if side == "SHORT":
                if crossed_down(prev, curr, st1_line):
                    opened = self._open_yellow(tbl, curr)
            else:
                if crossed_up(prev, curr, st1_line):
                    opened = self._open_yellow(tbl, curr)
            if opened:
                return
        
        # 3) SEVİYE GEÇİŞİ
        if tbl.active_trade and not tbl.active_trade.closed:
            new_lvl = self._maybe_advance(tbl, prev, curr)
            if new_lvl:
                tbl.current_level = new_lvl
                tbl.highest_level = new_lvl
                tbl.active_trade.current_level = new_lvl
                tbl.active_trade.highest_level = new_lvl
                self.tm.tg.notify_level_change(tbl.active_trade, new_lvl)
        
        # 4) ÇIKIŞ — bir önceki seviye çizgisi ters cross
        if tbl.active_trade and not tbl.active_trade.closed:
            cur_lvl = tbl.current_level
            exit_line_name = self.EXIT_LINE_FOR_LEVEL.get(cur_lvl)
            if exit_line_name is None:
                return
            exit_line = levels.get(exit_line_name)
            if exit_line is None:
                return
            
            if side == "SHORT":
                # Short çıkışı: yukarı cross
                if crossed_up(prev, curr, exit_line):
                    exit_name = f"SARI {cur_lvl} {exit_line_name} EXIT"
                    self.tm.close_trade(tbl.active_trade, exit_name, curr)
                    tbl.last_exit_line_name = exit_line_name
                    self._on_trade_closed(tbl)
            else:
                if crossed_down(prev, curr, exit_line):
                    exit_name = f"SARI {cur_lvl} {exit_line_name} EXIT"
                    self.tm.close_trade(tbl.active_trade, exit_name, curr)
                    tbl.last_exit_line_name = exit_line_name
                    self._on_trade_closed(tbl)
    
    def _on_trade_closed(self, tbl):
        tbl.active_trade = None
        tbl.flag_open = False
        tbl.current_level = "WAIT"
        tbl.highest_level = "WAIT"
    
    def _maybe_advance(self, tbl, prev, curr):
        cur_lvl = tbl.current_level
        try:
            idx = self.LEVEL_ORDER.index(cur_lvl)
        except ValueError:
            return None
        if idx + 1 >= len(self.LEVEL_ORDER):
            return None
        next_lvl = self.LEVEL_ORDER[idx + 1]
        next_line = tbl.levels.get(next_lvl)
        if next_line is None:
            return None
        if tbl.side == "SHORT":
            if crossed_down(prev, curr, next_line):
                return next_lvl
        else:
            if crossed_up(prev, curr, next_line):
                return next_lvl
        return None
    
    def _open_yellow(self, tbl, entry_price):
        red_trade = None
        with self.tm.slots.lock:
            for k, t in self.tm.slots.trades.items():
                if t.id == tbl.red_trade_id and t.thread == "RED" and not t.closed:
                    red_trade = t
                    break
        if not red_trade:
            return False
        
        trade = self.tm.open_trade(
            symbol=tbl.symbol,
            side=tbl.side,
            thread="YELLOW",
            entry_price=entry_price,
            lose_line=None,
            winrate_line=tbl.winrate_line,
            level_lines=tbl.levels,
            current_level="ST1",
            parent_red_trade=red_trade,
        )
        if trade:
            tbl.active_trade = trade
            tbl.current_level = "ST1"
            tbl.highest_level = "ST1"
            tbl.flag_open = False
            self.tm.log_flag_event(tbl.symbol, "YELLOW", tbl.side, "CONVERTED")
            return True
        return False
    
    # ------------------------------------------------------------------
    def run(self):
        log.info("Sarı thread başladı.")
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception as e:
                log.exception(f"YellowThread döngü hatası: {e}")
            time.sleep(1.0)
        log.info("Sarı thread durdu.")
