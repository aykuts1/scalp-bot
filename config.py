"""
Bot yapılandırması - Environment variables ve sabitler
"""
import os
from typing import List

# ============= API ANAHTARLARI =============
BYBIT_API_KEY: str = os.getenv("BYBIT_API_KEY", "").strip()
BYBIT_API_SECRET: str = os.getenv("BYBIT_API_SECRET", "").strip()
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ============= TESTNET / MAINNET =============
BYBIT_TESTNET: bool = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

# ============= VARSAYILAN SEMBOL LİSTESİ =============
DEFAULT_SYMBOLS: List[str] = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "SUIUSDT", "ZECUSDT", "DOGEUSDT", "TONUSDT", "TRXUSDT",
    "AAVEUSDT", "ADAUSDT", "LTCUSDT", "LINKUSDT", "APTUSDT",
    "INJUSDT", "AVAXUSDT", "NEARUSDT", "1000PEPEUSDT", "MEGAUSDT",
    "ONDOUSDT", "HYPEUSDT", "UNIUSDT", "ASTERUSDT", "WLDUSDT",
    "OPUSDT", "ARBUSDT", "STXUSDT", "JUPUSDT", "ENAUSDT",
    "TIAUSDT", "FETUSDT", "SEIUSDT", "EIGENUSDT",
]


def _parse_symbols() -> List[str]:
    """SYMBOLS env var'ından sembolleri parse et, boşsa varsayılan listeyi döndür."""
    raw = os.getenv("SYMBOLS", "").strip()
    if not raw:
        return DEFAULT_SYMBOLS
    parts = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return parts if parts else DEFAULT_SYMBOLS


SYMBOLS: List[str] = _parse_symbols()

# ============= STRATEJİ PARAMETRELERİ =============
# RSI
RSI_PERIOD: int = 14
RSI_LOOKBACK: int = 100         # Dinamik eşik için bakılacak RSI değer sayısı
RSI_EXTREME_COUNT: int = 10     # En düşük/yüksek kaç değerin ortalaması alınacak

# ATR
ATR_PERIOD: int = 14
ATR_LOOKBACK: int = 100         # ATR oranı için ortalama alınacak mum sayısı
ATR_RATIO_MIN: float = 0.7      # Minimum ATR oranı

# Trend filtresi (48 mum önceki fiyat)
TREND_LOOKBACK_BARS: int = 24           # 48 × 30dk = 24 saat öncesi
TREND_ATR_DISTANCE: float = 0.5         # Fiyat 48 mum öncesinden en az 0.5 ATR uzakta olmalı

# Chandelier Exit
CE_INITIAL_MULTIPLIER: float = 1.0      # 1 ATR geride trailing

# ============= POZİSYON YÖNETİMİ =============
STAKE_PERCENT: float = 0.20     # Bakiyenin %20'si
LEVERAGE: int = 10              # 10x kaldıraç
MAX_POSITIONS: int = 5          # Maksimum eş zamanlı açık pozisyon

# Stop ve kâr yönetimi
INITIAL_STOP_PERCENT: float = 0.01   # %1 sabit stop
BE_TRIGGER_ATR: float = 0.7          # 0.7 ATR kârda BE'ye taşı
BE_OFFSET_ATR: float = 0.2           # Entry + 0.2 ATR kâr garantile

# Emir tipi (limit emir, market gibi dolması için slip)
LIMIT_SLIPPAGE: float = 0.0005       # %0.05

# ============= ZAMANLAMA =============
KLINE_INTERVAL_30M: str = "30"
KLINE_LIMIT: int = 250
EXIT_SCAN_INTERVAL_SEC: int = 60

# ============= TEKNİK =============
HTTP_TIMEOUT: int = 30
RETRY_ATTEMPTS: int = 3
RETRY_DELAY: int = 2
SCAN_SLEEP_BETWEEN_SYMBOLS: float = 0.3


def validate() -> None:
    """Zorunlu environment variable'ların varlığını kontrol et."""
    missing = []
    if not BYBIT_API_KEY:
        missing.append("BYBIT_API_KEY")
    if not BYBIT_API_SECRET:
        missing.append("BYBIT_API_SECRET")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise RuntimeError(
            f"Eksik environment variable: {', '.join(missing)}"
        )
