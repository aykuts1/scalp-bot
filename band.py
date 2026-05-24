"""
SMARTBOT REDBLUE — band.py
Bant sistemi: EMA, ATR ve tum cizgilerin hesaplanmasi.
"""

from typing import List, Dict


def calculate_ema(closes: List[float], period: int) -> float:
    """Standart EMA. En son deger dondurulur."""
    if len(closes) < period:
        return sum(closes) / len(closes) if closes else 0.0
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period  # ilk SMA
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def calculate_atr(klines: List[dict], period: int) -> float:
    """
    Standart ATR (Wilder smoothing). En son deger dondurulur.
    klines: [{open, high, low, close}, ...] sirali (eskiden yeniye)
    """
    if len(klines) < period + 1:
        # Yeterli veri yok — basit ortalama
        if len(klines) < 2:
            return 0.0
        trs = []
        for i in range(1, len(klines)):
            h = klines[i]["high"]
            l = klines[i]["low"]
            pc = klines[i - 1]["close"]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        return sum(trs) / len(trs) if trs else 0.0

    # True Range hesapla
    trs = []
    for i in range(1, len(klines)):
        h = klines[i]["high"]
        l = klines[i]["low"]
        pc = klines[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    # Wilder smoothing: ilk ATR = ilk N TR'nin ortalamasi
    atr = sum(trs[:period]) / period
    # Sonraki ATR'ler: ((onceki ATR * (period-1)) + current TR) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def calculate_bands(klines: List[dict], config: dict) -> Dict[str, float]:
    """
    Tum bant cizgilerini hesapla.
    Donen sozluk anahtarlari:
      - ema, atr
      - kirmizi_ust_disticizgi, kirmizi_ust_distampon, kirmizi_ust_ictampon
      - kirmizi_ust_seviye1, kirmizi_ust_seviye2
      - mavi_ust_disticizgi, mavi_ust_distampon, mavi_ust_ictampon
      - mavi_ust_seviye1, mavi_ust_seviye2
      - kirmizi_alt_*, mavi_alt_* (simetrik)
    """
    band_cfg = config["band"]
    ema_period = band_cfg["ema_period"]
    atr_period = band_cfg["atr_period"]

    closes = [k["close"] for k in klines]
    ema = calculate_ema(closes, ema_period)
    atr = calculate_atr(klines, atr_period)

    red_mult = band_cfg["red_outer_multiplier"]
    blue_mult = band_cfg["blue_outer_multiplier"]
    outer_buf = band_cfg["outer_buffer_multiplier"]
    inner_buf = band_cfg["inner_buffer_multiplier"]
    lvl1 = band_cfg["level1_multiplier"]
    lvl2 = band_cfg["level2_multiplier"]

    # Ust taraf
    kirmizi_ust_dis = ema + red_mult * atr
    mavi_ust_dis = ema + blue_mult * atr

    # Alt taraf
    kirmizi_alt_dis = ema - red_mult * atr
    mavi_alt_dis = ema - blue_mult * atr

    return {
        "ema": ema,
        "atr": atr,

        # KIRMIZI UST
        "kirmizi_ust_seviye2": kirmizi_ust_dis + lvl2 * atr,
        "kirmizi_ust_seviye1": kirmizi_ust_dis + lvl1 * atr,
        "kirmizi_ust_distampon": kirmizi_ust_dis + outer_buf * atr,
        "kirmizi_ust_disticizgi": kirmizi_ust_dis,
        "kirmizi_ust_ictampon": kirmizi_ust_dis - inner_buf * atr,

        # MAVI UST
        "mavi_ust_distampon": mavi_ust_dis + outer_buf * atr,
        "mavi_ust_disticizgi": mavi_ust_dis,
        "mavi_ust_ictampon": mavi_ust_dis - inner_buf * atr,
        "mavi_ust_seviye1": mavi_ust_dis - lvl1 * atr,
        "mavi_ust_seviye2": mavi_ust_dis - lvl2 * atr,

        # MAVI ALT
        "mavi_alt_seviye2": mavi_alt_dis + lvl2 * atr,
        "mavi_alt_seviye1": mavi_alt_dis + lvl1 * atr,
        "mavi_alt_ictampon": mavi_alt_dis + inner_buf * atr,
        "mavi_alt_disticizgi": mavi_alt_dis,
        "mavi_alt_distampon": mavi_alt_dis - outer_buf * atr,

        # KIRMIZI ALT
        "kirmizi_alt_ictampon": kirmizi_alt_dis + inner_buf * atr,
        "kirmizi_alt_disticizgi": kirmizi_alt_dis,
        "kirmizi_alt_distampon": kirmizi_alt_dis - outer_buf * atr,
        "kirmizi_alt_seviye1": kirmizi_alt_dis - lvl1 * atr,
        "kirmizi_alt_seviye2": kirmizi_alt_dis - lvl2 * atr,
    }
