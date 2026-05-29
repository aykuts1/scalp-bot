"""
Ortak yardımcı fonksiyonlar.
"""
import time
from datetime import datetime, timezone


def crossed_up(prev_price, curr_price, level):
    """
    Fiyat çizgiyi yukarı cross etti mi?
    Önceki fiyat çizginin altındaydı, şimdi üstüne çıktı.
    """
    if prev_price is None or curr_price is None or level is None:
        return False
    return prev_price < level <= curr_price


def crossed_down(prev_price, curr_price, level):
    """
    Fiyat çizgiyi aşağı cross etti mi?
    Önceki fiyat çizginin üstündeydi, şimdi altına indi.
    """
    if prev_price is None or curr_price is None or level is None:
        return False
    return prev_price > level >= curr_price


def touched(curr_price, level, tolerance=0.0):
    """
    Fiyat çizgiye değdi mi? (cross şartı yok)
    Short için: fiyat çizginin altına veya tam üstüne geldi
    Long için: fiyat çizginin üstüne veya tam altına geldi
    Burada sadece "bu çizgi seviyesine ulaşıldı mı" kontrolü.
    """
    if curr_price is None or level is None:
        return False
    return abs(curr_price - level) <= tolerance or curr_price <= level or curr_price >= level


def touched_from_above(curr_price, level):
    """
    Short açılış için: fiyat Donchian alt çizgisine yukarıdan indi mi?
    """
    if curr_price is None or level is None:
        return False
    return curr_price <= level


def touched_from_below(curr_price, level):
    """
    Long açılış için: fiyat Donchian üst çizgisine aşağıdan yükseldi mi?
    """
    if curr_price is None or level is None:
        return False
    return curr_price >= level


def now_ts():
    """Şu anki Unix timestamp (saniye)."""
    return int(time.time())


def now_str(tz=None):
    """Şu anki tarih/saat string."""
    return datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M:%S")


def utc_now():
    """UTC datetime."""
    return datetime.now(tz=timezone.utc)


def safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pct_diff(entry, current, side):
    """
    İşlem PnL yüzdesi (kaldıraçsız).
    side: "LONG" veya "SHORT"
    """
    if entry == 0:
        return 0.0
    if side == "LONG":
        return (current - entry) / entry * 100.0
    else:
        return (entry - current) / entry * 100.0


def fmt_money(value):
    """USDT formatla."""
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return "0.0000"


def fmt_pct(value):
    """Yüzde formatla."""
    try:
        v = float(value)
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.2f}%"
    except (TypeError, ValueError):
        return "0.00%"
