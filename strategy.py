"""
Strateji: Sinyal üretimi ve filtre kontrolü.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

import config
import indicators


@dataclass
class SignalResult:
    """Tarama sonucu: sinyal ya da hangi filtreye takıldığı bilgisi."""
    symbol: str
    has_signal: bool = False
    side: Optional[str] = None             # "long" / "short" / None
    crossover_happened: bool = False       # RSI cross gerçekleşti mi?
    crossover_side: Optional[str] = None   # cross yönü
    rejection_reason: Optional[str] = None # filtreye takılma sebebi

    # Debug / bilgi için
    last_close: float = 0.0
    rsi_value: float = 0.0
    rsi_long_th: float = 0.0
    rsi_short_th: float = 0.0
    ema200_30m: float = 0.0
    ema200_2h: float = 0.0
    atr_value: float = 0.0
    atr_ratio_value: float = 0.0


def evaluate_symbol(
    df_30m: pd.DataFrame,
    df_2h: pd.DataFrame,
    symbol: str,
) -> SignalResult:
    """
    Sembol için sinyal değerlendir.

    df_30m: 30 dakikalık kapanan mumlar (en eskiden en yeniye)
    df_2h: 2 saatlik kapanan mumlar (en eskiden en yeniye)
    """
    result = SignalResult(symbol=symbol)

    # === Yeterli veri kontrolü ===
    min_30m = max(config.EMA_PERIOD, config.RSI_LOOKBACK + config.RSI_PERIOD,
                  config.ATR_LOOKBACK + config.ATR_PERIOD) + 5
    if len(df_30m) < min_30m or len(df_2h) < config.EMA_PERIOD + 5:
        result.rejection_reason = "Yetersiz veri"
        return result

    close_30m = df_30m["close"]
    high_30m = df_30m["high"]
    low_30m = df_30m["low"]
    close_2h = df_2h["close"]

    # === Göstergeler ===
    ema30 = indicators.ema(close_30m, config.EMA_PERIOD)
    ema2h = indicators.ema(close_2h, config.EMA_PERIOD)
    rsi_series = indicators.rsi(close_30m, config.RSI_PERIOD)
    atr_series = indicators.atr(high_30m, low_30m, close_30m, config.ATR_PERIOD)

    last_close = float(close_30m.iloc[-1])
    last_ema30 = float(ema30.iloc[-1])
    last_ema2h = float(ema2h.iloc[-1])
    last_rsi = float(rsi_series.iloc[-1])
    last_atr = float(atr_series.iloc[-1])

    if pd.isna(last_ema30) or pd.isna(last_ema2h) or pd.isna(last_rsi) or pd.isna(last_atr):
        result.rejection_reason = "Gösterge NaN"
        return result

    # Dinamik RSI eşikleri
    long_th, short_th = indicators.dynamic_rsi_thresholds(
        rsi_series, config.RSI_LOOKBACK, config.RSI_EXTREME_COUNT
    )

    # ATR oranı
    atr_r = indicators.atr_ratio(atr_series, config.ATR_LOOKBACK)

    # Result'a doldur
    result.last_close = last_close
    result.rsi_value = last_rsi
    result.rsi_long_th = long_th
    result.rsi_short_th = short_th
    result.ema200_30m = last_ema30
    result.ema200_2h = last_ema2h
    result.atr_value = last_atr
    result.atr_ratio_value = atr_r

    # === RSI Crossover Kontrolü ===
    long_cross = indicators.rsi_cross_up(rsi_series, long_th)
    short_cross = indicators.rsi_cross_down(rsi_series, short_th)

    if not long_cross and not short_cross:
        result.rejection_reason = "RSI crossover yok"
        return result

    # === LONG değerlendirme ===
    if long_cross:
        result.crossover_happened = True
        result.crossover_side = "long"

        if last_close <= last_ema30:
            result.rejection_reason = (
                f"30dk EMA200 altında "
                f"(fiyat {last_close:.6f} <= EMA {last_ema30:.6f})"
            )
            return result
        if atr_r < config.ATR_RATIO_MIN:
            result.rejection_reason = (
                f"ATR oranı düşük ({atr_r:.2f} < {config.ATR_RATIO_MIN})"
            )
            return result

        result.has_signal = True
        result.side = "long"
        return result

    # === SHORT değerlendirme ===
    if short_cross:
        result.crossover_happened = True
        result.crossover_side = "short"

        if last_close >= last_ema30:
            result.rejection_reason = (
                f"30dk EMA200 üstünde "
                f"(fiyat {last_close:.6f} >= EMA {last_ema30:.6f})"
            )
            return result
        if atr_r < config.ATR_RATIO_MIN:
            result.rejection_reason = (
                f"ATR oranı düşük ({atr_r:.2f} < {config.ATR_RATIO_MIN})"
            )
            return result

        result.has_signal = True
        result.side = "short"
        return result

    return result


def compute_entry_atr(df_30m: pd.DataFrame) -> float:
    """Giriş anındaki ATR değeri (CE ve BE seviyelerinin hesabı için)."""
    atr_series = indicators.atr(
        df_30m["high"], df_30m["low"], df_30m["close"], config.ATR_PERIOD
    )
    val = atr_series.iloc[-1]
    return float(val) if not pd.isna(val) else 0.0


def compute_initial_ce(side: str, entry_price: float, entry_atr: float) -> float:
    """
    Giriş anındaki Chandelier Exit seviyesi.
    Her zaman giriş fiyatının 1 ATR gerisinde başlar.
      Long: entry - 1×ATR
      Short: entry + 1×ATR
    """
    if side == "long":
        return entry_price - config.CE_INITIAL_MULTIPLIER * entry_atr
    return entry_price + config.CE_INITIAL_MULTIPLIER * entry_atr
