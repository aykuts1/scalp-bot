"""
Configuration module - environment variables and strategy parameters.
"""
import os

# ============================================================
# API CREDENTIALS (from Railway environment variables)
# ============================================================
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Optional: testnet (default false)
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

# ============================================================
# COIN LIST (40 coins)
# ============================================================
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "1000PEPEUSDT", "SUIUSDT", "WIFUSDT", "AVAXUSDT", "NEARUSDT",
    "AAVEUSDT", "APTUSDT", "ADAUSDT", "LINKUSDT", "ORDIUSDT",
    "FETUSDT", "OPUSDT", "ARBUSDT", "FTMUSDT", "TIAUSDT",
    "1000BONKUSDT", "1000FLOKIUSDT", "WLDUSDT", "LTCUSDT", "BCHUSDT",
    "DOTUSDT", "TRXUSDT", "INJUSDT", "SEIUSDT", "RENDERUSDT",
    "ATOMUSDT", "POLUSDT", "STXUSDT", "LDOUSDT", "FILUSDT",
    "GALAUSDT", "GRTUSDT", "UNIUSDT", "ARKMUSDT", "ETCUSDT",
]

# Allow override from environment (comma-separated)
_env_symbols = os.getenv("SYMBOLS", "").strip()
if _env_symbols:
    SYMBOLS = [s.strip().upper() for s in _env_symbols.split(",") if s.strip()]
else:
    SYMBOLS = DEFAULT_SYMBOLS

# ============================================================
# STRATEGY PARAMETERS
# ============================================================
# Indicators
EMA_HIGH_PERIOD = 100      # EMA of highs
EMA_LOW_PERIOD = 100       # EMA of lows
EMA_TRIGGER_PERIOD = 7     # Trigger EMA on close
ATR_PERIOD = 14            # ATR length
CHANNEL_AVG_PERIOD = 100   # Window for average channel width

# Timeframe
TIMEFRAME = "5"            # Bybit kline interval: "5" = 5 minutes
KLINE_LIMIT = 300          # Number of candles to fetch (enough for EMA100 + averages)

# Risk / Position
LEVERAGE = 50              # 50x isolated
STAKE_PERCENT = 0.20       # 20% of total balance
MAX_POSITIONS = 5          # Max simultaneous open positions
MARGIN_MODE = "ISOLATED"   # Always isolated

# Stop Loss / Exit
INITIAL_SL_PERCENT = 0.01      # 1% safety net at entry (price-based, not PnL)

# Stage 1: +2 ATR peak profit → SL moves to +0.5% profit, CE 4 ATR trail starts
STAGE1_TRIGGER_ATR = 2.0
STAGE1_SL_PCT = 0.005

# Stage 2: +6 ATR peak profit → SL moves to +0.2 ATR profit, CE narrows to 3 ATR
STAGE2_TRIGGER_ATR = 6.0
STAGE2_SL_ATR = 0.2

# Scanning intervals
ENTRY_SCAN_INTERVAL = 300      # 5 minutes (aligned with candle close)
EXIT_SCAN_INTERVAL = 60        # 60 seconds

# ============================================================
# BYBIT API SETTINGS
# ============================================================
ACCOUNT_TYPE = "UNIFIED"       # Bybit Unified Trading Account
CATEGORY = "linear"            # USDT perpetuals

# ============================================================
# VALIDATION
# ============================================================
def validate_config():
    """Ensure required env vars exist."""
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
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
