"""
🚀 BOT ANA GİRİŞ NOKTASI

Tüm thread'leri başlatır ve genel zamanlamayı yönetir:
- 15dk mum verisi güncelleme (DataManager fetch_all_candles + Red.scan_flags)
- 5sn anlık fiyat güncelleme (DataManager fetch_all_prices)
- 12 saatte bir stake güncelleme

Thread'ler kendi içlerinde her saniye loop ederek:
- Cross hesaplamaları
- Seviye geçişleri
- İşlem aç/kapat
yapar.
"""
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from config_loader import Config
from data_manager import DataManager
from trade_manager import TradeManager
from telegram_thread import TelegramThread
from red_thread import RedThread
from blue_thread import BlueThread
from yellow_thread import YellowThread


# =========================================================================
# LOGGING
# =========================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("Main")


# =========================================================================
# BOT
# =========================================================================
class Bot:
    
    def __init__(self):
        log.info("Bot başlatılıyor...")
        self.cfg = Config("config.json")
        
        # Telegram'ı önce kur (data manager hatalarını bildirmek için)
        self.tg = TelegramThread(self.cfg, data_manager=None, trade_manager_ref=None, control_ref=None)
        
        # Data Manager (Telegram'ı verir ki hataları bildirsin)
        self.dm = DataManager(self.cfg, telegram_notifier=self.tg)
        self.tg.dm = self.dm  # telegrama dm referansı
        
        # Trade Manager
        self.tm = TradeManager(self.cfg, self.dm, self.tg)
        self.tg.set_trade_manager(self.tm)
        
        # Borsadaki mevcut pozisyonları slot olarak işaretle (yönetilmez)
        self.tm.load_external_positions()
        
        # Thread'ler
        self.red_thread = RedThread(self.cfg, self.dm, self.tm, blue_thread_ref=None, yellow_thread_ref=None)
        self.blue_thread = BlueThread(self.cfg, self.dm, self.tm)
        self.yellow_thread = YellowThread(self.cfg, self.dm, self.tm)
        self.red_thread.set_thread_refs(self.blue_thread, self.yellow_thread)
        
        # Telegram'a kontrol referansı
        self.tg.set_control(self)
        
        # Master scheduler thread için stop event
        self._scheduler_stop = threading.Event()
        self._scheduler_thread = None
        self._running = False
        
        # 15dk takip için
        self._last_15m_close = None
        # 12h stake güncelleme için
        self._last_stake_update_ts = time.time()
    
    # ---------------------------------------------------------------------
    def is_running(self):
        return self._running
    
    def start_trading(self):
        if self._running:
            return
        log.info("Trading başlatılıyor (thread'ler).")
        # Eğer durdurulmuş thread'ler varsa yeniden oluştur
        if not self.red_thread.is_alive():
            self.red_thread = RedThread(self.cfg, self.dm, self.tm, None, None)
        if not self.blue_thread.is_alive():
            self.blue_thread = BlueThread(self.cfg, self.dm, self.tm)
        if not self.yellow_thread.is_alive():
            self.yellow_thread = YellowThread(self.cfg, self.dm, self.tm)
        self.red_thread.set_thread_refs(self.blue_thread, self.yellow_thread)
        
        self.red_thread.start()
        self.blue_thread.start()
        self.yellow_thread.start()
        
        self._scheduler_stop.clear()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, name="Scheduler", daemon=True)
        self._scheduler_thread.start()
        self._running = True
    
    def stop_trading(self):
        if not self._running:
            return
        log.info("Trading durduruluyor.")
        self._scheduler_stop.set()
        self.red_thread.stop()
        self.blue_thread.stop()
        self.yellow_thread.stop()
        # Birkaç saniye bekle
        for t in (self.red_thread, self.blue_thread, self.yellow_thread):
            try:
                t.join(timeout=5)
            except Exception:
                pass
        self._running = False
    
    # ---------------------------------------------------------------------
    def _scheduler_loop(self):
        """
        Master zamanlayıcı:
        - Her 5sn anlık fiyat çek
        - 15dk mum kapanışında mum verisi çek + flag scan
        - 12 saatte bir stake güncelle
        """
        log.info("Scheduler başladı.")
        last_price_fetch = 0
        while not self._scheduler_stop.is_set():
            now = time.time()
            
            # 5sn fiyat
            if now - last_price_fetch >= self.cfg.price_update_interval_sec:
                try:
                    self.dm.fetch_all_prices()
                except Exception as e:
                    log.exception(f"Fiyat çekme döngü hatası: {e}")
                last_price_fetch = now
            
            # 15dk mum kapanışı kontrol
            try:
                self._check_15m_close()
            except Exception as e:
                log.exception(f"15dk check hatası: {e}")
            
            # 12 saatte stake güncelleme
            try:
                self._check_stake_update()
            except Exception as e:
                log.exception(f"Stake update hatası: {e}")
            
            time.sleep(1.0)
        log.info("Scheduler durdu.")
    
    def _check_15m_close(self):
        """
        Yeni bir 15dk mumu kapandı mı?
        15dk boundary: 00, 15, 30, 45
        """
        now = datetime.now(tz=timezone.utc)
        # 15dk dönem başlangıcı
        minute_bucket = (now.minute // 15) * 15
        period_start = now.replace(minute=minute_bucket, second=0, microsecond=0)
        # Önceki kapanış = period_start (yeni dönem başladıysa eski dönem kapandı)
        
        if self._last_15m_close is None:
            self._last_15m_close = period_start
            return
        
        if period_start > self._last_15m_close:
            # Yeni 15dk başladı → eski mum kapandı
            self._last_15m_close = period_start
            log.info(f"15dk mum kapandı: {period_start.isoformat()}. Mum verisi çekiliyor...")
            try:
                self.dm.fetch_all_candles()
                # Flag scan
                self.red_thread.scan_flags()
            except Exception as e:
                log.exception(f"15dk işlem hatası: {e}")
    
    def _check_stake_update(self):
        if (time.time() - self._last_stake_update_ts) >= self.cfg.stake_update_interval_hours * 3600:
            self._last_stake_update_ts = time.time()
            try:
                bal = self.dm.update_balance()
                new_stake = self.tm.update_stake()
                self.tg.notify_stake_update(new_stake, bal)
            except Exception as e:
                log.exception(f"Stake update hatası: {e}")
    
    # ---------------------------------------------------------------------
    def run(self):
        """Tüm thread'leri ve scheduler'i başlatır."""
        # Telegram thread'i ilk başla (komutları dinler)
        self.tg.start()
        time.sleep(1.0)
        
        # Bot başladı bildirimi
        self.tg.notify_bot_started(self.cfg.to_dict())
        
        # Trading başlat
        self.start_trading()
        
        # Sinyal yakalama
        def shutdown(signum, frame):
            log.info(f"Sinyal alındı ({signum}), kapatılıyor...")
            self.shutdown()
            sys.exit(0)
        
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        
        # Ana thread bekler
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            self.shutdown()
    
    def shutdown(self):
        log.info("Shutdown başladı.")
        try:
            self.tg.notify_bot_stopped()
        except Exception:
            pass
        self.stop_trading()
        self.dm.stop()
        try:
            self.tg.stop()
            self.tg.join(timeout=3)
        except Exception:
            pass
        log.info("Shutdown tamamlandı.")


# =========================================================================
def main():
    bot = Bot()
    bot.run()


if __name__ == "__main__":
    main()
