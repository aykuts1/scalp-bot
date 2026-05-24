"""
SMARTBOT REDBLUE — main.py
Ana orkestratör. Tum threadleri baslatir ve calistirir.
"""

import json
import os
import signal
import sys
import threading
import time
from datetime import datetime

from state import state
from bybit_client import BybitClient
from telegram_notifier import notifier, msg_bot_started
from entry import EntryThread
from exit import ExitThread
from report import ReportThread


def load_config(path: str = "config.json") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    print("=" * 50)
    print("SMARTBOT REDBLUE BASLATILIYOR")
    print("=" * 50)

    # 1. Config yukle
    config = load_config()
    print(f"[OK] Config yuklendi — Timeframe: {config['band']['timeframe']}, Coin sayisi: {config['coins']['count']}")

    # 2. Bybit baglantisi
    testnet = config["env"]["testnet"]
    bybit = BybitClient(testnet=testnet)
    print(f"[OK] Bybit baglantisi kuruldu — Testnet: {testnet}")

    # 3. Bakiye oku, stake hesapla
    balance = bybit.get_balance()
    stake_pct = config["account"]["stake_percent"] / 100.0
    stake = balance * stake_pct
    state.start_time = datetime.now()
    state.initial_balance = balance
    state.stake = stake
    state.leverage = config["order"]["leverage"]
    print(f"[OK] Bakiye: {balance:.2f} USDT, Stake: {stake:.2f} USDT")

    # 4. Coinler icin baslangic durumlari
    for coin in config["coins"]["list"]:
        state.init_coin(coin)

    # 5. Kaldirac ayarla
    for coin in config["coins"]["list"]:
        try:
            bybit.set_leverage(coin, config["order"]["leverage"])
        except Exception as e:
            print(f"[UYARI] {coin} kaldirac ayarlanamadi: {e}")

    # 6. Stop event
    stop_event = threading.Event()

    def handle_shutdown(signum, frame):
        print("\n[SHUTDOWN] Bot durduruluyor...")
        stop_event.set()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # 7. Threadleri olustur
    threads = [
        EntryThread("kirmizi", config, bybit, stop_event),
        EntryThread("mavi", config, bybit, stop_event),
        ExitThread("kirmizi", config, bybit, stop_event),
        ExitThread("mavi", config, bybit, stop_event),
        ReportThread(config, bybit, stop_event),
    ]

    # 8. Bot baslatildi bildirimi
    notifier.send(msg_bot_started(config, balance, stake))
    print("[OK] Bot baslatildi bildirimi gonderildi")

    # 9. Threadleri baslat
    for t in threads:
        t.start()
        print(f"[OK] {t.name} baslatildi")

    print("=" * 50)
    print("BOT CALISIYOR")
    print("=" * 50)

    # 10. Ana thread bekle
    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        handle_shutdown(None, None)


if __name__ == "__main__":
    main()
