"""
Central configuration — Liquid Oversold Bounce bot (Bybit USDT linear).

POSITION SIZE
  Fixed USDT margin per trade (default $50). Not a % of balance.
  Max 1 open trade.

TELEGRAM — hardcoded (override via env if needed).

RAILWAY VARIABLES:
  BYBIT_API_KEY, BYBIT_API_SECRET, AUTO_TRADE, POSITION_SIZE_USD,
  SCAN_INTERVAL_SEC, IN_TRADE_POLL_SEC, ...
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: str = "false") -> bool:
    return _env(key, default).lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: str) -> float:
    try:
        return float(_env(key, default))
    except ValueError:
        return float(default)


def _env_int(key: str, default: str) -> int:
    try:
        return int(float(_env(key, default)))
    except ValueError:
        return int(default)


# =============================================================================
# TELEGRAM
# =============================================================================
TELEGRAM_BOT_TOKEN = _env(
    "TELEGRAM_BOT_TOKEN",
    "7945248659:AAGCGlHn8fyjkYWX8CV6recyLeSSLp3OjI0",
)
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", "-1004326901305")

# =============================================================================
# POSITION / RISK
# =============================================================================
POSITION_SIZE_USD = _env_float("POSITION_SIZE_USD", "50")
MAX_OPEN_TRADES = _env_int("MAX_OPEN_TRADES", "1")

LEVERAGE_MIN = _env_int("LEVERAGE_MIN", "3")
LEVERAGE_MAX = _env_int("LEVERAGE_MAX", "12")
LEVERAGE_DEFAULT = _env_int("LEVERAGE_DEFAULT", "5")

# =============================================================================
# EXCHANGE — Bybit USDT linear perpetuals
# =============================================================================
EXCHANGE_ID = "bybit"
MARKET_TYPE = "linear"  # USDT perpetual
QUOTE = "USDT"

TOP_N_PAIRS = _env_int("TOP_N_PAIRS", "40")
FORCE_PAIRS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "XRP/USDT:USDT",
    "DOGE/USDT:USDT",
    "AVAX/USDT:USDT",
    "SUI/USDT:USDT",
    "LINK/USDT:USDT",
    "BNB/USDT:USDT",
    "ADA/USDT:USDT",
]

MIN_QUOTE_VOLUME_USD = _env_float("MIN_QUOTE_VOLUME_USD", "5000000")
EXCLUDE_KEYWORDS = (
    "UP/", "DOWN/", "BULL", "BEAR", "3L", "3S", "5L", "5S",
    "XAU/", "XAG/", "USOIL", "UKOIL", "WTI", "BRENT", "NATGAS",
    "SP500", "NAS100", "US30", "GER40", "HK50", "JP225",
    "EURUSD", "GBPUSD", "USDJPY", "BTCDOM",
    "STOCK", "SPCX", "SPY/", "QQQ/", "IWM/", "DIA/", "SPX/",
    "TSLA/", "AAPL/", "NVDA/", "AMZN/", "META/", "MSFT/", "GOOGL/",
    "COIN/", "MSTR/", "HOOD/", "PLTR/", "NFLX/", "AMD/",
)
EXCLUDE_BASES = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "SPX", "SPCXSTOCK",
    "TSLA", "AAPL", "NVDA", "AMZN", "META", "MSFT", "GOOGL", "GOOG",
    "COIN", "MSTR", "HOOD", "PLTR", "NFLX", "AMD", "INTC", "BABA",
})

# =============================================================================
# TIMEFRAMES
# =============================================================================
SIGNAL_TIMEFRAME = _env("SIGNAL_TIMEFRAME", "15m") or "15m"
TREND_TIMEFRAME = _env("TREND_TIMEFRAME", "4h") or "4h"
CANDLE_LIMIT_SIGNAL = 120
CANDLE_LIMIT_TREND = 120

SCAN_INTERVAL_SEC = _env_int("SCAN_INTERVAL_SEC", "600")
_in_trade = _env_int("IN_TRADE_POLL_SEC", "120")
IN_TRADE_POLL_SEC = max(60, min(600, _in_trade))
REQUEST_SLEEP_SEC = 0.12
OHLCV_RETRIES = 3
REQUEST_TIMEOUT = 25

# =============================================================================
# STRATEGY
# =============================================================================
TREND_EMA_PERIOD = 50
TREND_REQUIRE_SLOPE_UP = True
TREND_SLOPE_BARS = 5

RSI_PERIOD = 14
RSI_OVERSOLD = _env_float("RSI_OVERSOLD", "32")
RSI_EXIT_CROSS = 40

BB_PERIOD = 20
BB_STD = 2.0
REQUIRE_NEAR_LOWER_BB = True
BB_TOUCH_PCT = 0.35

VOLUME_MA_PERIOD = 20
VOLUME_MIN_RATIO = 0.9

REQUIRE_BULLISH_CLOSE = True
REQUIRE_RSI_TURNING_UP = True

BTC_SYMBOL = "BTC/USDT:USDT"
BTC_DUMP_RSI_MAX = 28
BTC_FILTER_ENABLED = True

ATR_PERIOD = 14
SL_ATR_MULT = 1.4
MIN_SL_PCT = 0.6
MAX_SL_PCT = 3.5
TP1_R = 1.2
TP2_R = 2.2
TP3_R = 3.5
TP1_SIZE_PCT = 40
TP2_SIZE_PCT = 40
TP3_SIZE_PCT = 20

MIN_RR_TO_TP2 = 1.8
MIN_CONFIDENCE = _env_float("MIN_CONFIDENCE", "68")

# =============================================================================
# SIGNAL HYGIENE
# =============================================================================
PAIR_COOLDOWN_HOURS = _env_float("PAIR_COOLDOWN_HOURS", "8")
TRADE_LOCK_MAX_HOURS = _env_float("TRADE_LOCK_MAX_HOURS", "12")
AUTO_RELEASE_ON_SL_TP = True

# =============================================================================
# BYBIT API (Railway Variables)
# Also accept legacy MEXC_* names as fallback so old env doesn't crash import.
# =============================================================================
BYBIT_API_KEY = _env("BYBIT_API_KEY", "") or _env("MEXC_API_KEY", "")
BYBIT_API_SECRET = _env("BYBIT_API_SECRET", "") or _env("MEXC_API_SECRET", "")
# Keep old names as aliases for any leftover references during migration
MEXC_API_KEY = BYBIT_API_KEY
MEXC_API_SECRET = BYBIT_API_SECRET

AUTO_TRADE = _env_bool("AUTO_TRADE", "false")
MARGIN_MODE = "isolated"

# =============================================================================
# PATHS / LOGGING / RAILWAY
# =============================================================================
STATE_FILE = str(_ROOT / "trade_state.json")
SIGNAL_LOG_CSV = str(_ROOT / "logs" / "signals.csv")
LOG_DIR = str(_ROOT / "logs")
LOG_LEVEL = _env("LOG_LEVEL", "INFO") or "INFO"
DRY_RUN = _env_bool("DRY_RUN", "false")
PORT = _env_int("PORT", "8080")
