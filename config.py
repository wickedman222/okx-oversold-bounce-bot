"""
Central configuration for the Liquid Oversold Bounce signal bot.

POSITION SIZE
  Always a FIXED USDT margin per trade (default $50).
  Never a % of account balance. One trade at a time.

TELEGRAM
  Hardcoded in this file (as requested). Railway Variables can still
  override if you set TELEGRAM_* later.

RAILWAY VARIABLES (set later in Railway → Variables tab):
  MEXC_API_KEY, MEXC_API_SECRET, AUTO_TRADE, POSITION_SIZE_USD, etc.
  See RAILWAY.md for the full list.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load local .env if present (optional; Railway injects env vars directly)
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
# TELEGRAM — hardcoded (you said this is fine in code)
# Railway Variables TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID still override if set.
# =============================================================================
TELEGRAM_BOT_TOKEN = _env(
    "TELEGRAM_BOT_TOKEN",
    "7945248659:AAGCGlHn8fyjkYWX8CV6recyLeSSLp3OjI0",
)
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", "-1004326901305")

# =============================================================================
# POSITION / RISK
# FIXED $50 USDT margin PER TRADE — not a percentage of balance.
# When Phase 2 auto-trades: order uses exactly this many USDT as margin.
# =============================================================================
POSITION_SIZE_USD = _env_float("POSITION_SIZE_USD", "50")  # fixed margin $
MAX_OPEN_TRADES = _env_int("MAX_OPEN_TRADES", "1")  # hard: 1 only

# Leverage bounds — bot recommends inside this range (notional = $50 * lev)
LEVERAGE_MIN = _env_int("LEVERAGE_MIN", "3")
LEVERAGE_MAX = _env_int("LEVERAGE_MAX", "12")
LEVERAGE_DEFAULT = _env_int("LEVERAGE_DEFAULT", "5")

# =============================================================================
# EXCHANGE / SCAN (public MEXC data — no keys needed for Phase 1 signals)
# =============================================================================
EXCHANGE_ID = "mexc"
MARKET_TYPE = "swap"
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
)

# =============================================================================
# TIMEFRAMES
# =============================================================================
SIGNAL_TIMEFRAME = _env("SIGNAL_TIMEFRAME", "15m") or "15m"
TREND_TIMEFRAME = _env("TREND_TIMEFRAME", "4h") or "4h"
CANDLE_LIMIT_SIGNAL = 120
CANDLE_LIMIT_TREND = 120

# How often to scan the market (seconds).
# Strategy uses closed 15m candles → no need to poll every 2–3 min.
# Default 10 minutes. Override on Railway Variables: SCAN_INTERVAL_SEC=900 (15m), etc.
SCAN_INTERVAL_SEC = _env_int("SCAN_INTERVAL_SEC", "600")
# While a live trade is open, poll much more often (software SL/TP backup).
IN_TRADE_POLL_SEC = _env_int("IN_TRADE_POLL_SEC", "45")
REQUEST_SLEEP_SEC = 0.15
OHLCV_RETRIES = 3
REQUEST_TIMEOUT = 25

# =============================================================================
# STRATEGY: Liquid Oversold Bounce
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
# PHASE 2 — MEXC keys from Railway Variables tab (leave empty for signals-only)
# =============================================================================
MEXC_API_KEY = _env("MEXC_API_KEY", "")
MEXC_API_SECRET = _env("MEXC_API_SECRET", "")
# Set AUTO_TRADE=true on Railway when keys are set and you want live orders.
AUTO_TRADE = _env_bool("AUTO_TRADE", "false")
MARGIN_MODE = "isolated"

# =============================================================================
# PATHS / LOGGING / RAILWAY
# =============================================================================
# On Railway the filesystem is ephemeral; lock resets on redeploy (acceptable
# for Phase 1). For durable lock later, use a volume or Redis.
STATE_FILE = str(_ROOT / "trade_state.json")
SIGNAL_LOG_CSV = str(_ROOT / "logs" / "signals.csv")
LOG_DIR = str(_ROOT / "logs")
LOG_LEVEL = _env("LOG_LEVEL", "INFO") or "INFO"

DRY_RUN = _env_bool("DRY_RUN", "false")

# Railway injects PORT — health server binds here
PORT = _env_int("PORT", "8080")
