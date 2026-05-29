"""
🔵 MAVİ THREAD

Hedge mantığı:
- Her Kırmızı Short açıldığında → Mavi Long tablosu oluşur
- Her Kırmızı Long açıldığında → Mavi Short tablosu oluşur
- Kırmızı kapanınca bağlı Mavi de kapanır

Seviye Hesaplama:
- Kırmızı giriş çizgisi ile Kırmızı LOSE çizgisi arasına 4 eşit parça
- FLAG = Kırmızı giriş çizgisi
- ST1..ST4 arada
- Çıkış için Kırmızı LOSE çizgisi hedef

Açılış (cross şartı VAR):
- Mavi Long: FLAG çizgisini yukarı cross → Flag açılır
            ST1'i yukarı cross → işlem açılır
- Mavi Short tam ters simetri

Seviye geçişi: cross ile yükselir (geri dönmez)

Çıkış (cross şartı VAR):
- ST1 → FLAG aşağı cross (Long için) / yukarı (Short için)
- STn → STn-1 aşağı/yukarı cross
- Her seviyede Kırmızı LOSE çizgisi cross ederse acil çıkış

Yeniden Giriş:
- Çıkılan çizgi tekrar cross ederse → Flag açılır
- Bir sonraki seviye cross → İşlem açılır
- Yeniden giriş mevcut işlem kapandıktan sonra olur
- Slot limitine dahildir
"""
import threading
import time
import logging

from utils import crossed_up, crossed_down, now_ts

log = logging.getLogger("BlueThread")


class BlueTable:
    """Bir Kırmızı işleme bağlı Mavi tablo."""
    __slots__ = ("red_trade_id", "symbol", "side", "levels", "lose_line",
                 "flag_open", "current_level", "highest_level",
                 "active_trade", "last_exit_line_name")
    
    def __init__(self, red_trade, levels):
        self.red_trade_id = red_trade.id
        self.symbol = red_trade.symbol
        # Mavi yön Kırmızı'nın tersi
        self.side = "LONG" if red_trade.side == "SHORT" else "SHORT"
        self.levels = dict(levels)  # FLAG, ST1, ST2, ST3, ST4
        self.lose_line = red_trade.lose_line  # Kırmızı LOSE
        self.flag_open = False
        self.current_level = "FLAG"  # Bekleme noktası
        self.highest_level = "FLAG"
        self.active_trade = None  # Trade obj
        self.last_exit_line_name = None  # yeniden giriş için


class BlueThread(threading.Thread):
    
    LEVEL_ORDER = ["FLAG", "ST1", "ST2", "ST3", "ST4"]
    # current_level → çıkış için karşılaştırılacak çizgi
    EXIT_LINE_FOR_LEVEL = {
        "ST1": "FLAG",
        "ST2": "ST1",
        "ST3": "ST2",
        "ST4": "ST3",
    }
    
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
    # TABLO OLUŞTURMA
    # ------------------------------------------------------------------
    def create_table_for_red(self, red_trade):
        """Kırmızı işlem açıldığında çağrılır."""
        # Kırmızı giriş ile Kırmızı LOSE arası 4 eşit parça → 5 nokta
        # FLAG = Kırmızı giriş, ST1..ST4 = aralar
        entry = red_trade.level_lines["ENTRY"]
        lose = red_trade.level_lines["LOSE"]
        # Mavi yön Kırmızı'nın tersi: Kırmızı Short ise Mavi Long (Lose > Entry, yukarı doğru)
        # Kırmızı Long ise Mavi Short (Lose < Entry, aşağı doğru)
        step = (lose - entry) / 5.0
        levels = {
            "FLAG": entry,
            "ST1": entry + step * 1,
            "ST2": entry + step * 2,
            "ST3": entry + step * 3,
            "ST4": entry + step * 4,
        }
        # Bybit fiyat hassasiyeti için yuvarlama gerekirse trade_manager halleder
        
        table = BlueTable(red_trade, levels)
        with self.tables_lock:
            self.tables[red_trade.id] = table
        
        self.tm.tg.notify_thread_ready(red_trade, "BLUE", levels)
        return table
    
    def remove_table_for_red(self, red_trade_id):
        """Kırmızı kapandığında çağrılır. Bağlı Mavi tablo, flag, işlem temizlenir."""
        with self.tables_lock:
            tbl = self.tables.pop(red_trade_id, None)
        if not tbl:
            return
        # Açık işlem varsa kapat (trade_manager.close_red_and_dependents zaten yapar
        # ama burada tek başına da olabilir)
        if tbl.active_trade and not tbl.active_trade.closed:
            self.tm.close_trade(tbl.active_trade, "KIRMIZI KAPANDI (TABLO SİLİNDİ)")
        if tbl.flag_open:
            self.tm.log_flag_event(tbl.symbol, "BLUE", tbl.side, "DELETED")
    
    # ------------------------------------------------------------------
    # SCAN
    # ------------------------------------------------------------------
    def scan(self):
        """Tüm Mavi tabloları için scan."""
        # Önce Kırmızı'sı kapanmış tabloları temizle (genelde trade_manager halleder
        # ama emniyet için kontrol)
        with self.tables_lock:
            ids_to_remove = []
            for red_id, tbl in self.tables.items():
                red = self.tm.slots.get_red_link(red_id)
                if red is None:
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
        
        # 0) Kırmızı LOSE çizgisi cross → işlem varsa acil çıkış
        if tbl.active_trade and not tbl.active_trade.closed:
            if side == "LONG":
                # Kırmızı Short LOSE çizgisi yukarıda → Mavi Long için yukarı cross
                if crossed_up(prev, curr, tbl.lose_line):
                    exit_name = f"MAVİ {tbl.current_level} LOSE EXIT"
                    self.tm.close_trade(tbl.active_trade, exit_name, curr)
                    self._on_trade_closed(tbl)
                    return
            else:  # SHORT
                if crossed_down(prev, curr, tbl.lose_line):
                    exit_name = f"MAVİ {tbl.current_level} LOSE EXIT"
                    self.tm.close_trade(tbl.active_trade, exit_name, curr)
                    self._on_trade_closed(tbl)
                    return
        
        # 1) FLAG ACMA / SILME
        flag_line = levels["FLAG"]
        if not tbl.flag_open and tbl.active_trade is None:
            # Flag açma: cross şartı
            if side == "LONG":
                if crossed_up(prev, curr, flag_line):
                    tbl.flag_open = True
                    self.tm.log_flag_event(symbol, "BLUE", side, "OPENED")
                    self.tm.tg.notify_flag(symbol, "BLUE", side, "OPENED")
            else:
                if crossed_down(prev, curr, flag_line):
                    tbl.flag_open = True
                    self.tm.log_flag_event(symbol, "BLUE", side, "OPENED")
                    self.tm.tg.notify_flag(symbol, "BLUE", side, "OPENED")
        elif tbl.flag_open and tbl.active_trade is None:
            # Flag silme: cross ters yön (geri dönüş)
            if side == "LONG":
                if crossed_down(prev, curr, flag_line):
                    tbl.flag_open = False
                    self.tm.log_flag_event(symbol, "BLUE", side, "DELETED")
                    self.tm.tg.notify_flag(symbol, "BLUE", side, "DELETED")
                    return
            else:
                if crossed_up(prev, curr, flag_line):
                    tbl.flag_open = False
                    self.tm.log_flag_event(symbol, "BLUE", side, "DELETED")
                    self.tm.tg.notify_flag(symbol, "BLUE", side, "DELETED")
                    return
        
        # 2) İŞLEM AÇMA — Flag varken ST1 cross
        if tbl.flag_open and tbl.active_trade is None:
            st1_line = levels["ST1"]
            opened = False
            if side == "LONG":
                if crossed_up(prev, curr, st1_line):
                    opened = self._open_blue(tbl, curr)
            else:
                if crossed_down(prev, curr, st1_line):
                    opened = self._open_blue(tbl, curr)
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
        
        # 4) ÇIKIŞ — bir önceki seviye çizgisi cross
        if tbl.active_trade and not tbl.active_trade.closed:
            cur_lvl = tbl.current_level
            exit_line_name = self.EXIT_LINE_FOR_LEVEL.get(cur_lvl)
            if exit_line_name is None:
                return
            exit_line = levels.get(exit_line_name)
            if exit_line is None:
                return
            
            if side == "LONG":
                # Long'da çıkış: fiyat aşağı cross
                if crossed_down(prev, curr, exit_line):
                    exit_name = f"MAVİ {cur_lvl} {exit_line_name} EXIT"
                    self.tm.close_trade(tbl.active_trade, exit_name, curr)
                    tbl.last_exit_line_name = exit_line_name
                    self._on_trade_closed(tbl)
            else:
                if crossed_up(prev, curr, exit_line):
                    exit_name = f"MAVİ {cur_lvl} {exit_line_name} EXIT"
                    self.tm.close_trade(tbl.active_trade, exit_name, curr)
                    tbl.last_exit_line_name = exit_line_name
                    self._on_trade_closed(tbl)
    
    def _on_trade_closed(self, tbl):
        """Mavi işlem kapandı, tablo işlem-yok durumuna, flag kapalı."""
        tbl.active_trade = None
        tbl.flag_open = False
        tbl.current_level = "FLAG"
        tbl.highest_level = "FLAG"
    
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
        if tbl.side == "LONG":
            if crossed_up(prev, curr, next_line):
                return next_lvl
        else:
            if crossed_down(prev, curr, next_line):
                return next_lvl
        return None
    
    def _open_blue(self, tbl, entry_price):
        # Parent kırmızı
        red_link = self.tm.slots.get_red_link(tbl.red_trade_id)
        if not red_link:
            return False
        # Tek Kırmızı obj almak için trade_manager'da get_red_for kullanılır,
        # ama biz red_trade_id'yi ID olarak bilmiyoruz — slots.trades üstünden bulalım
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
            thread="BLUE",
            entry_price=entry_price,
            lose_line=tbl.lose_line,
            winrate_line=None,
            level_lines=tbl.levels,
            current_level="ST1",
            parent_red_trade=red_trade,
        )
        if trade:
            tbl.active_trade = trade
            tbl.current_level = "ST1"
            tbl.highest_level = "ST1"
            tbl.flag_open = False
            self.tm.log_flag_event(tbl.symbol, "BLUE", tbl.side, "CONVERTED")
            return True
        return False
    
    # ------------------------------------------------------------------
    # RUN
    # ------------------------------------------------------------------
    def run(self):
        log.info("Mavi thread başladı.")
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception as e:
                log.exception(f"BlueThread döngü hatası: {e}")
            time.sleep(1.0)
        log.info("Mavi thread durdu.")
