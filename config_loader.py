"""
Config Loader
JSON config dosyasını okur ve environment variables ile birleştirir.
"""
import json
import os


class Config:
    def __init__(self, config_path="config.json"):
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # JSON parametreleri
        self.symbols = data["symbols"]
        self.leverage = int(data["leverage"])
        self.stake_pct = float(data["stake_pct"])
        self.hard_sl_pct = float(data["hard_sl_pct"])
        self.stake_update_interval_hours = int(data["stake_update_interval_hours"])
        self.max_lose_pct = float(data["max_lose_pct"])
        self.risk_reward = float(data["risk_reward"])
        self.donchian_period = int(data["donchian_period"])
        self.ema_period = int(data["ema_period"])
        self.timeframe = str(data["timeframe"])
        self.candle_count = int(data["candle_count"])
        self.price_update_interval_sec = int(data["price_update_interval_sec"])
        self.candle_fetch_delay_sec = int(data["candle_fetch_delay_sec"])
        self.thread_scan_interval_sec = int(data.get("thread_scan_interval_sec", 1))
        self.position_sync_interval_sec = int(data.get("position_sync_interval_sec", 1))

        # Environment variables (Railway)
        self.bybit_api_key = os.getenv("BYBIT_API_KEY", "")
        self.bybit_api_secret = os.getenv("BYBIT_API_SECRET", "")
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

        if not all([self.bybit_api_key, self.bybit_api_secret,
                    self.telegram_bot_token, self.telegram_chat_id]):
            raise ValueError(
                "Eksik environment variable! "
                "BYBIT_API_KEY, BYBIT_API_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID gerekli."
            )

    def to_dict(self):
        """Telegram bot başlangıç bildirimi için"""
        return {
            "Coin Sayısı": len(self.symbols),
            "Kaldıraç": f"{self.leverage}x",
            "Stake %": self.stake_pct,
            "Hard SL %": self.hard_sl_pct,
            "Stake Güncelleme (saat)": self.stake_update_interval_hours,
            "Max Lose %": self.max_lose_pct,
            "Risk/Reward": self.risk_reward,
            "Donchian Period": self.donchian_period,
            "EMA Period": self.ema_period,
            "Timeframe (dk)": self.timeframe,
            "Mum Sayısı": self.candle_count,
            "Fiyat Güncelleme (sn)": self.price_update_interval_sec,
            "Thread Tarama (sn)": self.thread_scan_interval_sec,
            "Pozisyon Senkron (sn)": self.position_sync_interval_sec,
            "Coin Bekleme (sn)": self.candle_fetch_delay_sec,
        }
