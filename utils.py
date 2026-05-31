"""
Ortak yardımcı fonksiyonlar.

İçerik:
- Cross fonksiyonları (crossed_up, crossed_down) → tüm thread'lerin kalbi
- Bölge/zone yardımcıları (in_zone, find_zone) → Mavi/Sarı için
- Zaman yardımcıları (now_ts, utc_now, now_str)
- Formatlayıcılar (fmt_money, fmt_pct, safe_float, pct_diff)

Not: Eski `touched`, `touched_from_above`, `touched_from_below` fonksiyonları
kaldırıldı. Yeni Kırmızı açılış mantığında değme tespiti zaten cross ile yapılıyor.
"""
import time
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# CROSS FONKSİYONLARI — botun en kritik fonksiyonları
# ---------------------------------------------------------------------------

def crossed_up(prev_price, curr_price, level):
    """
    Fiyat çizgiyi yukarı cross etti mi?
    Önceki tarama çizginin altındaydı, yeni tarama çizgide veya üstünde.
    """
    if prev_price is None or curr_price is None or level is None:
        return False
    return prev_price < level <= curr_price


def crossed_down(prev_price, curr_price, level):
    """
    Fiyat çizgiyi aşağı cross etti mi?
    Önceki tarama çizginin üstündeydi, yeni tarama çizgide veya altında.
    """
    if prev_price is None or curr_price is None or level is None:
        return False
    return prev_price > level >= curr_price


# ---------------------------------------------------------------------------
# BÖLGE / ZONE YARDIMCILARI — Mavi ve Sarı tablolar için
# ---------------------------------------------------------------------------

def in_zone(price, low, high):
    """
    Fiyat verilen iki çizgi arasında mı? (sınırlar dahil)
    low ve high'ın hangisi daha düşük/yüksek otomatik handle edilir.
    """
    if price is None or low is None or high is None:
        return False
    lo = min(low, high)
    hi = max(low, high)
    return lo <= price <= hi


def find_zone(price, boundaries):
    """
    Sıralı çizgi listesinde fiyatın hangi bölgede olduğunu döner.

    boundaries: dict — {"isim": fiyat_değeri, ...}
                bot boundary'leri sıralayıp price'ın hangi iki çizgi arasında
                olduğuna karar verir.

    Dönüş: (alt_çizgi_adı, üst_çizgi_adı) veya (None, None) dışındaysa.
    """
    if price is None or not boundaries:
        return (None, None)

    # Çizgileri fiyat değerine göre sırala (artan)
    sorted_lines = sorted(boundaries.items(), key=lambda x: x[1])

    # Fiyat tüm çizgilerin altındaysa
    if price < sorted_lines[0][1]:
        return (None, sorted_lines[0][0])

    # Fiyat tüm çizgilerin üstündeyse
    if price > sorted_lines[-1][1]:
        return (sorted_lines[-1][0], None)

    # İki çizgi arasında
    for i in range(len(sorted_lines) - 1):
        low_name, low_val = sorted_lines[i]
        high_name, high_val = sorted_lines[i + 1]
        if low_val <= price <= high_val:
            return (low_name, high_name)

    return (None, None)


# ---------------------------------------------------------------------------
# ZAMAN YARDIMCILARI
# ---------------------------------------------------------------------------

def now_ts():
    """Şu anki Unix timestamp (saniye)."""
    return int(time.time())


def now_str(tz=None):
    """Şu anki tarih/saat string."""
    return datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M:%S")


def utc_now():
    """UTC datetime."""
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# FORMATLAYICILAR
# ---------------------------------------------------------------------------

def safe_float(v, default=0.0):
    """Float'a güvenli dönüşüm."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pct_diff(entry, current, side):
    """
    Kaldıraçsız % değişim.
    side: "LONG" veya "SHORT"
    """
    if entry == 0 or entry is None or current is None:
        return 0.0
    if side == "LONG":
        return (current - entry) / entry * 100.0
    else:
        return (entry - current) / entry * 100.0


def fmt_money(value):
    """USDT formatla (4 ondalık)."""
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return "0.0000"


def fmt_pct(value):
    """Yüzde formatla (işaretli, 2 ondalık)."""
    try:
        v = float(value)
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.2f}%"
    except (TypeError, ValueError):
        return "0.00%"


def fmt_price(value, decimals=4):
    """Fiyat formatla (verilen ondalık)."""
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return "0.0000"
