"""
Göstergeler: Donchian Channel ve EMA
"""
import numpy as np


def donchian_history(highs, lows, period=50):
    """
    Donchian'ın geçmişteki değerlerini döner.
    Her mum için o ana kadar olan Donchian üst/alt çizgisi.
    Mevcut mum dahil edilmez (kapanmış mumlardan hesaplanır).

    Returns: (upper_list, lower_list) — her biri len(highs) uzunluğunda
    """
    upper_list = []
    lower_list = []

    for i in range(len(highs)):
        if i < period:
            upper_list.append(None)
            lower_list.append(None)
        else:
            window_high = highs[i - period:i]
            window_low = lows[i - period:i]
            upper_list.append(float(max(window_high)))
            lower_list.append(float(min(window_low)))

    return upper_list, lower_list


def ema(closes, period=800):
    """
    Exponential Moving Average.

    Returns: float (son EMA değeri)
    """
    if len(closes) < period:
        return None

    closes_np = np.array(closes, dtype=float)
    alpha = 2.0 / (period + 1)

    # İlk EMA değeri ilk `period` mumun ortalaması
    ema_val = float(np.mean(closes_np[:period]))

    # Geri kalanı hesapla
    for price in closes_np[period:]:
        ema_val = (price - ema_val) * alpha + ema_val

    return float(ema_val)
