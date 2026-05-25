"""
SMARTBOT REDBLUE — fetcher.py
Merkezi veri cekici thread.
- Fiyat: her 5 saniyede tum coinler icin cekilir
- Kline: her 30 saniyede tum coinler icin cekilir (Bybit rate limit icin)
Diger threadler API'ye gitmek yerine cache'ten okur.
"""

import threading
import time
from datetime import datetime

from state import state
from bybit_client import BybitClient
from telegram_notifier import notifier, msg_error


class FetcherThread(threading.Thread):
    def __init__(self, config: dict, bybit: BybitClient, stop_event: threading.Event):
        super().__init__(daemon=True, name="Fetcher_Thread")
        self.config = config
        self.bybit = bybit
        self.stop_event = stop_event
        self.scan_interval = config["scan"]["interval_seconds"]  # 5 sn (fiyat icin)
        self.kline_interval = 60  # Kline 60 saniyede bir cekilir (rate limit icin)
        self.timeframe = config["band"]["timeframe"]
        self.coins = config["coins"]["list"]
        # Ayni hata art arda gelirse spam yapmasin diye basit kontrol
        self.last_error_per_coin = {}
        # Kline son cekildi mi takibi
        self.last_kline_fetch = 0.0

    def run(self):
        while not self.stop_event.is_set():
            start = time.time()
            # Kline'i 30 saniyede bir cek
            fetch_klines = (start - self.last_kline_fetch) >= self.kline_interval
            self.fetch_all_coins(fetch_klines=fetch_klines)
            if fetch_klines:
                self.last_kline_fetch = start
            # Bir sonraki tarama icin bekle (5 sn - gecen sure)
            elapsed = time.time() - start
            wait_time = max(0, self.scan_interval - elapsed)
            self.stop_event.wait(wait_time)

    def fetch_all_coins(self, fetch_klines: bool):
        for coin in self.coins:
            if self.stop_event.is_set():
                return
            try:
                # Fiyat her zaman cekilir
                price = self.bybit.get_price(coin)
                # Kline sadece zamani gelmisse
                if fetch_klines:
                    klines = self.bybit.get_klines(coin, self.timeframe, limit=200)
                    state.set_cached_data(coin, klines, price)
                    # Kline istekleri arasinda bekleme — rate limit asimini onler
                    time.sleep(2.0)
                else:
                    # Mevcut kline'i koru, sadece fiyati guncelle
                    existing_klines = state.get_cached_klines(coin)
                    if existing_klines is not None:
                        state.set_cached_data(coin, existing_klines, price)
                # Hata cache temizle
                if coin in self.last_error_per_coin:
                    del self.last_error_per_coin[coin]
            except Exception as e:
                err_str = str(e)[:80]
                if self.last_error_per_coin.get(coin) != err_str:
                    notifier.send(msg_error(self.name, coin, "Veri Cekme Hatasi", err_str))
                    self.last_error_per_coin[coin] = err_str
