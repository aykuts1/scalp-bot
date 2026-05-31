"""
🚀 BOT ANA GİRİŞ NOKTASI

6 thread:
- 🔴 KIRMIZI grubu: Kırmızı + Mavi + Sarı
- ⚪️ BEYAZ grubu:   Beyaz + Mor + Turuncu

Her grup birbirinden bağımsız çalışır. Aynı coinde aynı anda
1 Kırmızı + 1 Beyaz açık olabilir.
"""
import logging
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
from white_thread import WhiteThread
from purple_thread import PurpleThread
from orange_thread import OrangeThread


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

        # Telegram'ı önce kur (Data Manager hatalarını bildirebilsin diye)
        self.tg = TelegramThread(self.cfg, data_manager=None,
                                 trade_manager_ref=None, control_ref=None)

        # Data Manager
        self.dm = DataManager(self.cfg, telegram_notifier=self.tg)
        self.tg.dm = self.dm

        # Trade Manager
        self.tm = TradeManager(self.cfg, self.dm, self.tg)
        self.tg.set_trade_manager(self.tm)

        # KIRMIZI grubu thread'leri
        self.red_thread = RedThread(self.cfg, self.dm, self.tm, None, None)
        self.blue_thread = BlueThread(self.cfg, self.dm, self.tm)
        self.yellow_thread = YellowThread(self.cfg, self.dm, self.tm)
        self.red_thread.set_thread_refs(self.blue_thread, self.yellow_thread)

        # BEYAZ grubu thread'leri
        self.white_thread = WhiteThread(self.cfg, self.dm, self.tm, None, None)
        self.purple_thread = PurpleThread(self.cfg, self.dm, self.tm)
        self.orange_thread = OrangeThread(self.cfg, self.dm, self.tm)
        self.white_thread.set_thread_refs(self.purple_thread, self.orange_thread)

        # Telegram'a kontrol referansı
        self.tg.set_control(self)

        # Scheduler
        self._scheduler_stop = threading.Event()
        self._scheduler_thread = None
        self._running = False
        self._shutdown_requested = threading.Event()

        # Zamanlama state
        self._last_15m_close = None
        self._last_stake_update_ts = time.time()

    # ---------------------------------------------------------------------
    def is_running(self):
        return self._running

    def _all_threads(self):
        """Tüm trading thread'leri (6 thread). Telegram dahil değil."""
        return (self.red_thread, self.blue_thread, self.yellow_thread,
                self.white_thread, self.purple_thread, self.orange_thread)

    def start_trading(self):
        if self._running:
            return
        log.info("Trading başlatılıyor.")

        # Ölmüş thread'leri yeniden yarat (KIRMIZI grubu)
        if not self.red_thread.is_alive():
            self.red_thread = RedThread(self.cfg, self.dm, self.tm, None, None)
        if not self.blue_thread.is_alive():
            self.blue_thread = BlueThread(self.cfg, self.dm, self.tm)
        if not self.yellow_thread.is_alive():
            self.yellow_thread = YellowThread(self.cfg, self.dm, self.tm)
        self.red_thread.set_thread_refs(self.blue_thread, self.yellow_thread)

        # Ölmüş thread'leri yeniden yarat (BEYAZ grubu)
        if not self.white_thread.is_alive():
            self.white_thread = WhiteThread(self.cfg, self.dm, self.tm, None, None)
        if not self.purple_thread.is_alive():
            self.purple_thread = PurpleThread(self.cfg, self.dm, self.tm)
        if not self.orange_thread.is_alive():
            self.orange_thread = OrangeThread(self.cfg, self.dm, self.tm)
        self.white_thread.set_thread_refs(self.purple_thread, self.orange_thread)

        # 6 thread'i başlat
        for t in self._all_threads():
            t.start()

        # Pozisyon önbelleğini ilk başta doldur
        try:
            log.info("Başlangıç pozisyon senkronizasyonu yapılıyor...")
            self.dm.sync_open_positions()
        except Exception as e:
            log.exception(f"Başlangıç pozisyon senkron hatası: {e}")

        # Kırmızı için başlangıç flag taraması (15dk boundary beklemeden)
        # Beyaz için ekstra tarama gerekmiyor — anlık fiyat hareketine bağlı.
        try:
            log.info("Başlangıç Kırmızı flag taraması yapılıyor...")
            self.red_thread.scan_flags()
        except Exception as e:
            log.exception(f"Başlangıç flag scan hatası: {e}")

        # 15dk takip için ilk boundary
        self._last_15m_close = self._current_15m_period()

        self._scheduler_stop.clear()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, name="Scheduler", daemon=True)
        self._scheduler_thread.start()
        self._running = True

    def stop_trading(self):
        if not self._running:
            return
        log.info("Trading durduruluyor.")
        self._scheduler_stop.set()
        for t in self._all_threads():
            t.stop()
        for t in self._all_threads():
            try:
                t.join(timeout=8)
            except Exception:
                pass
        self._running = False

    # ---------------------------------------------------------------------
    @staticmethod
    def _current_15m_period():
        now = datetime.now(tz=timezone.utc)
        minute_bucket = (now.minute // 15) * 15
        return now.replace(minute=minute_bucket, second=0, microsecond=0)

    def _scheduler_loop(self):
        """Master zamanlayıcı."""
        log.info("Scheduler başladı.")
        last_price_fetch = 0.0
        last_position_sync = 0.0
        while not self._scheduler_stop.is_set():
            now = time.time()

            # Fiyat çekme
            if now - last_price_fetch >= self.cfg.price_update_interval_sec:
                try:
                    self.dm.fetch_all_prices()
                except Exception as e:
                    log.exception(f"Fiyat çekme hatası: {e}")
                last_price_fetch = now

            # Pozisyon senkron (önbellek için — yetim kapatma kararı buna BAĞLI DEĞİL)
            if now - last_position_sync >= self.cfg.position_sync_interval_sec:
                try:
                    self.dm.sync_open_positions()
                except Exception as e:
                    log.exception(f"Pozisyon senkron hatası: {e}")
                last_position_sync = now

            # 15dk kontrol (Kırmızı flag tarama)
            try:
                self._check_15m_close()
            except Exception as e:
                log.exception(f"15dk check hatası: {e}")

            # 12h stake
            try:
                self._check_stake_update()
            except Exception as e:
                log.exception(f"Stake update hatası: {e}")

            # 6 thread sağlık kontrolü
            try:
                self._check_thread_health()
            except Exception as e:
                log.exception(f"Thread health check hatası: {e}")

            time.sleep(0.5)
        log.info("Scheduler durdu.")

    def _check_15m_close(self):
        if self._last_15m_close is None:
            self._last_15m_close = self._current_15m_period()
            return

        current_period = self._current_15m_period()
        if current_period > self._last_15m_close:
            self._last_15m_close = current_period
            log.info(f"15dk mum kapandı: {current_period.isoformat()}. Mum çekiliyor...")
            try:
                self.dm.fetch_all_candles()
                # Kırmızı'nın flag mantığı 15dk kapanışa bağlı (Beyaz için gerekmiyor)
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

    def _check_thread_health(self):
        """Çöken thread'i otomatik yeniden başlat (6 thread)."""
        if not self._running:
            return

        # Kırmızı
        if not self.red_thread.is_alive():
            log.error("Kırmızı thread çökmüş, yeniden başlatılıyor...")
            self.tg.notify_critical("Kırmızı thread çöktü", "Otomatik yeniden başlatılıyor.")
            self.red_thread = RedThread(self.cfg, self.dm, self.tm,
                                        self.blue_thread, self.yellow_thread)
            self.red_thread.start()

        # Mavi
        if not self.blue_thread.is_alive():
            log.error("Mavi thread çökmüş, yeniden başlatılıyor...")
            self.tg.notify_critical("Mavi thread çöktü", "Otomatik yeniden başlatılıyor.")
            self.blue_thread = BlueThread(self.cfg, self.dm, self.tm)
            self.blue_thread.start()
            self.red_thread.set_thread_refs(self.blue_thread, self.yellow_thread)

        # Sarı
        if not self.yellow_thread.is_alive():
            log.error("Sarı thread çökmüş, yeniden başlatılıyor...")
            self.tg.notify_critical("Sarı thread çöktü", "Otomatik yeniden başlatılıyor.")
            self.yellow_thread = YellowThread(self.cfg, self.dm, self.tm)
            self.yellow_thread.start()
            self.red_thread.set_thread_refs(self.blue_thread, self.yellow_thread)

        # Beyaz
        if not self.white_thread.is_alive():
            log.error("Beyaz thread çökmüş, yeniden başlatılıyor...")
            self.tg.notify_critical("Beyaz thread çöktü", "Otomatik yeniden başlatılıyor.")
            self.white_thread = WhiteThread(self.cfg, self.dm, self.tm,
                                            self.purple_thread, self.orange_thread)
            self.white_thread.start()

        # Mor
        if not self.purple_thread.is_alive():
            log.error("Mor thread çökmüş, yeniden başlatılıyor...")
            self.tg.notify_critical("Mor thread çöktü", "Otomatik yeniden başlatılıyor.")
            self.purple_thread = PurpleThread(self.cfg, self.dm, self.tm)
            self.purple_thread.start()
            self.white_thread.set_thread_refs(self.purple_thread, self.orange_thread)

        # Turuncu
        if not self.orange_thread.is_alive():
            log.error("Turuncu thread çökmüş, yeniden başlatılıyor...")
            self.tg.notify_critical("Turuncu thread çöktü", "Otomatik yeniden başlatılıyor.")
            self.orange_thread = OrangeThread(self.cfg, self.dm, self.tm)
            self.orange_thread.start()
            self.white_thread.set_thread_refs(self.purple_thread, self.orange_thread)

    # ---------------------------------------------------------------------
    def run(self):
        # Telegram önce başla
        self.tg.start()
        time.sleep(1.0)

        # Bot başladı bildirimi
        self.tg.notify_bot_started(self.cfg.to_dict())

        # Trading başlat
        self.start_trading()

        # Sinyal yakalama — sys.exit() kullanma
        def _signal_handler(signum, frame):
            log.info(f"Sinyal alındı ({signum}), kapatma başlatılıyor...")
            self._shutdown_requested.set()

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        # Ana thread bekler
        try:
            while not self._shutdown_requested.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self._shutdown_requested.set()

        self.shutdown()

    def shutdown(self):
        log.info("Shutdown başladı.")

        # Açık işlemler varsa uyar (otomatik kapatma yok)
        try:
            open_trades = self.tm.slots.get_all_open() if self.tm else []
            if open_trades:
                summary = ", ".join(f"{t.symbol}({t.thread})" for t in open_trades[:8])
                self.tg.notify_critical(
                    f"Bot kapanıyor — {len(open_trades)} açık işlem var",
                    f"Açık işlemler otomatik kapatılmıyor. Manuel takip gerekebilir.\n{summary}"
                )
        except Exception:
            pass

        try:
            self.tg.notify_bot_stopped()
        except Exception:
            pass

        self.stop_trading()
        self.dm.stop()
        try:
            self.tg.stop()
            self.tg.join(timeout=5)
        except Exception:
            pass

        log.info("Shutdown tamamlandı.")


# =========================================================================
def main():
    bot = Bot()
    bot.run()


if __name__ == "__main__":
    main()
