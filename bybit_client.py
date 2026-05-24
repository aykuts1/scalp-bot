"""
SMARTBOT REDBLUE — bybit.py
Bybit V5 API katmani. Veri cekme, emir gonderme, pozisyon sorgulama.
"""

import os
import time
from typing import List, Optional
from pybit.unified_trading import HTTP


class BybitClient:
    def __init__(self, testnet: bool = False):
        api_key = os.getenv("BYBIT_API_KEY")
        api_secret = os.getenv("BYBIT_API_SECRET")
        if not api_key or not api_secret:
            raise RuntimeError("BYBIT_API_KEY ve BYBIT_API_SECRET environment variable olarak tanimlanmali.")

        self.client = HTTP(
            testnet=testnet,
            api_key=api_key,
            api_secret=api_secret,
        )
        self.category = "linear"  # USDT perpetual

    # ──────────────────────────────────────────
    # KLINE & FIYAT
    # ──────────────────────────────────────────

    def get_klines(self, symbol: str, interval: str, limit: int = 200) -> List[dict]:
        """
        Belirtilen timeframe icin mumler.
        interval: "1", "5", "15", "30", "60", "120", "240", "D"
        Donus: en eskiden en yeniye sirali liste
        """
        interval_map = {
            "1m": "1", "5m": "5", "15m": "15", "30m": "30",
            "1h": "60", "2h": "120", "4h": "240", "1d": "D"
        }
        bybit_interval = interval_map.get(interval, interval)

        resp = self.client.get_kline(
            category=self.category,
            symbol=symbol,
            interval=bybit_interval,
            limit=limit,
        )
        if resp.get("retCode") != 0:
            raise RuntimeError(f"Kline fetch failed for {symbol}: {resp.get('retMsg')}")

        klines = resp["result"]["list"]
        # Bybit en yeniden eskiye verir, biz ters cevirelim
        klines = list(reversed(klines))
        # Format: [timestamp, open, high, low, close, volume, turnover]
        return [
            {
                "timestamp": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
            for k in klines
        ]

    def get_price(self, symbol: str) -> float:
        """Son fiyat (ticker)."""
        resp = self.client.get_tickers(category=self.category, symbol=symbol)
        if resp.get("retCode") != 0:
            raise RuntimeError(f"Price fetch failed for {symbol}: {resp.get('retMsg')}")
        return float(resp["result"]["list"][0]["lastPrice"])

    # ──────────────────────────────────────────
    # HESAP
    # ──────────────────────────────────────────

    def get_balance(self) -> float:
        """USDT bakiye."""
        resp = self.client.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        if resp.get("retCode") != 0:
            raise RuntimeError(f"Balance fetch failed: {resp.get('retMsg')}")
        coins = resp["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] == "USDT":
                return float(c["walletBalance"])
        return 0.0

    def set_leverage(self, symbol: str, leverage: int):
        """Sembol icin kaldirac ayarla. Zaten ayarli ise hata vermez."""
        try:
            self.client.set_leverage(
                category=self.category,
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
        except Exception as e:
            # 110043 = leverage zaten ayarli — ignore
            if "110043" not in str(e):
                raise

    # ──────────────────────────────────────────
    # POZISYON
    # ──────────────────────────────────────────

    def get_position(self, symbol: str) -> Optional[dict]:
        """Acik pozisyon bilgisi. Yoksa None."""
        resp = self.client.get_positions(category=self.category, symbol=symbol)
        if resp.get("retCode") != 0:
            return None
        positions = resp["result"]["list"]
        for p in positions:
            size = float(p.get("size", 0))
            if size > 0:
                return {
                    "symbol": p["symbol"],
                    "side": p["side"],  # "Buy" or "Sell"
                    "size": size,
                    "entry_price": float(p["avgPrice"]),
                    "unrealized_pnl": float(p.get("unrealisedPnl", 0)),
                    "leverage": float(p.get("leverage", 1)),
                }
        return None

    def get_instrument_info(self, symbol: str) -> dict:
        """Sembol icin minQty, qtyStep, vs."""
        resp = self.client.get_instruments_info(category=self.category, symbol=symbol)
        if resp.get("retCode") != 0:
            raise RuntimeError(f"Instrument info failed for {symbol}: {resp.get('retMsg')}")
        info = resp["result"]["list"][0]
        return {
            "min_qty": float(info["lotSizeFilter"]["minOrderQty"]),
            "qty_step": float(info["lotSizeFilter"]["qtyStep"]),
            "tick_size": float(info["priceFilter"]["tickSize"]),
        }

    # ──────────────────────────────────────────
    # EMIR
    # ──────────────────────────────────────────

    def place_market_order(
        self,
        symbol: str,
        side: str,         # "Buy" or "Sell"
        qty: float,
        sl_price: float,
    ) -> dict:
        """Market emir gonder + SL ekle."""
        resp = self.client.place_order(
            category=self.category,
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(qty),
            stopLoss=str(sl_price),
            slTriggerBy="LastPrice",
        )
        if resp.get("retCode") != 0:
            raise RuntimeError(f"Order failed: {resp.get('retMsg')}")
        return resp["result"]

    def close_position(self, symbol: str, side: str, qty: float) -> dict:
        """Pozisyonu market emirle kapat. side = mevcut pozisyonun ters yonu."""
        # Long kapatmak icin Sell, short kapatmak icin Buy
        close_side = "Sell" if side == "Buy" else "Buy"
        resp = self.client.place_order(
            category=self.category,
            symbol=symbol,
            side=close_side,
            orderType="Market",
            qty=str(qty),
            reduceOnly=True,
        )
        if resp.get("retCode") != 0:
            raise RuntimeError(f"Close failed: {resp.get('retMsg')}")
        return resp["result"]

    # ──────────────────────────────────────────
    # KAPALI POZISYON GECMISI (gercek K/Z icin)
    # ──────────────────────────────────────────

    def get_closed_pnl(self, symbol: str, limit: int = 10) -> List[dict]:
        """Son kapanan pozisyonlarin gercek K/Z bilgisi."""
        resp = self.client.get_closed_pnl(
            category=self.category,
            symbol=symbol,
            limit=limit,
        )
        if resp.get("retCode") != 0:
            return []
        return resp["result"]["list"]


def round_qty(qty: float, step: float) -> float:
    """Quantity'yi step'e gore yuvarla."""
    if step == 0:
        return qty
    return float(int(qty / step) * step)


def round_price(price: float, tick: float) -> float:
    """Fiyati tick size'a gore yuvarla."""
    if tick == 0:
        return price
    return round(price / tick) * tick
