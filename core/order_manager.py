"""Order Manager — Bybit market emirleri ve borsadaki SL yönetimi.

Tasarım:
  * Bot 'hedge mode' kullanır → aynı sembolde long ve short aynı anda olabilir.
  * Pozisyon idx: Bybit V5 → positionIdx: 1=Buy/Long, 2=Sell/Short
  * Her giriş emri: market + %2 SL (stopLoss alanı).
  * Bot kendi içinde aynı yönde N adet işlem tutsa da borsada bunlar
    BİRLEŞİK görünür. Borsanın SL'i tek pozisyon üzerindedir.
    → Bot exit ettiğinde reduceOnly market order ile o işlemin size'ını kapatır.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from pybit.unified_trading import HTTP

log = logging.getLogger(__name__)


@dataclass
class InstrumentInfo:
    symbol: str
    qty_step: float        # qtyStep: emir adım büyüklüğü
    min_qty: float         # minOrderQty
    tick_size: float       # priceFilter.tickSize
    max_leverage: float

    @classmethod
    def from_api(cls, info: dict) -> "InstrumentInfo":
        lot = info.get("lotSizeFilter", {})
        price = info.get("priceFilter", {})
        lev = info.get("leverageFilter", {})
        return cls(
            symbol=info.get("symbol", ""),
            qty_step=float(lot.get("qtyStep", "0.001")),
            min_qty=float(lot.get("minOrderQty", "0.001")),
            tick_size=float(price.get("tickSize", "0.01")),
            max_leverage=float(lev.get("maxLeverage", "50")),
        )

    def round_qty(self, qty: float) -> float:
        """qty'yi step'in altına yuvarla (always down)."""
        if self.qty_step <= 0:
            return qty
        steps = math.floor(qty / self.qty_step)
        return round(steps * self.qty_step, 10)

    def round_price(self, price: float) -> float:
        if self.tick_size <= 0:
            return price
        # En yakın tick'e yuvarla
        ticks = round(price / self.tick_size)
        return round(ticks * self.tick_size, 10)


class OrderManager:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False,
                 retry_count: int = 3, retry_delay: int = 5) -> None:
        self.client = HTTP(api_key=api_key, api_secret=api_secret, testnet=testnet)
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self._instrument_cache: dict[str, InstrumentInfo] = {}

    # ---------- helpers ----------

    def _with_retry(self, fn, *args, **kwargs) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.retry_count):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last_exc = e
                msg = str(e).lower()
                # Retry edilmeyecek hatalar:
                if any(k in msg for k in [
                    "insufficient", "balance", "margin",
                    "max position", "leverage",
                    "not modified", "110025", "110043",
                ]):
                    raise
                log.warning("Bybit emir hatası (deneme %d/%d): %s",
                            attempt + 1, self.retry_count, e)
                if attempt < self.retry_count - 1:
                    time.sleep(self.retry_delay)
        raise last_exc  # type: ignore[misc]

    def get_instrument(self, symbol: str) -> InstrumentInfo:
        if symbol in self._instrument_cache:
            return self._instrument_cache[symbol]
        resp = self._with_retry(
            self.client.get_instruments_info, category="linear", symbol=symbol,
        )
        items = resp.get("result", {}).get("list", [])
        if not items:
            raise RuntimeError(f"{symbol} bulunamadı")
        info = InstrumentInfo.from_api(items[0])
        self._instrument_cache[symbol] = info
        return info

    def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            self._with_retry(
                self.client.set_leverage,
                category="linear", symbol=symbol,
                buyLeverage=str(leverage), sellLeverage=str(leverage),
            )
        except Exception as e:
            # "leverage not modified" gibi hatalar normal — bypass
            if "not modified" in str(e).lower() or "110043" in str(e):
                return
            raise

    def set_hedge_mode(self, symbol: str) -> None:
        """Aynı sembolde long+short için hedge mode."""
        try:
            self._with_retry(
                self.client.switch_position_mode,
                category="linear", symbol=symbol, mode=3,  # 3 = hedge
            )
        except Exception as e:
            if "not modified" in str(e).lower() or "110025" in str(e):
                return
            log.warning("Hedge mode set başarısız %s: %s", symbol, e)

    # ---------- entry ----------

    def open_market_with_sl(self, symbol: str, side: str, qty: float,
                            sl_price: float) -> dict:
        """Market giriş + borsa SL emniyet kemeri.

        side: 'long' veya 'short'
        Bybit: Buy/Sell + positionIdx 1/2
        """
        info = self.get_instrument(symbol)
        qty_r = info.round_qty(qty)
        sl_r = info.round_price(sl_price)
        if qty_r < info.min_qty:
            raise RuntimeError(
                f"qty {qty_r} < min {info.min_qty} ({symbol})"
            )

        bybit_side = "Buy" if side == "long" else "Sell"
        position_idx = 1 if side == "long" else 2

        resp = self._with_retry(
            self.client.place_order,
            category="linear",
            symbol=symbol,
            side=bybit_side,
            orderType="Market",
            qty=str(qty_r),
            positionIdx=position_idx,
            stopLoss=str(sl_r),
            slTriggerBy="LastPrice",
            tpslMode="Full",
            reduceOnly=False,
        )
        return resp

    # ---------- exit ----------

    def close_market(self, symbol: str, side: str, qty: float) -> dict:
        """Pozisyon kapatma — reduceOnly market.

        side: kapatılacak pozisyonun yönü ('long' veya 'short').
        Bybit'te long'u kapatmak için Sell, short'u kapatmak için Buy.
        """
        info = self.get_instrument(symbol)
        qty_r = info.round_qty(qty)
        bybit_side = "Sell" if side == "long" else "Buy"
        position_idx = 1 if side == "long" else 2

        resp = self._with_retry(
            self.client.place_order,
            category="linear",
            symbol=symbol,
            side=bybit_side,
            orderType="Market",
            qty=str(qty_r),
            positionIdx=position_idx,
            reduceOnly=True,
        )
        return resp

    # ---------- sizing ----------

    @staticmethod
    def calc_qty(stake_usdt: float, leverage: int, price: float,
                 instrument: InstrumentInfo) -> float:
        """Stake * leverage / price → adım büyüklüğüne yuvarla."""
        notional = stake_usdt * leverage
        raw_qty = notional / price
        return instrument.round_qty(raw_qty)
