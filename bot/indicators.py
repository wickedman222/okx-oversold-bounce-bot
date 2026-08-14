"""
Technical indicators used by the oversold-bounce strategy.

Pure pandas/numpy — no TA-Lib dependency so install stays simple.
All functions expect OHLCV DataFrames with columns:
  open, high, low, close, volume
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def bollinger(
    close: pd.Series, period: int = 20, std_mult: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, period)
    std = close.rolling(period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return upper, mid, lower


def atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    """Latest ATR as % of price — used for leverage sizing."""
    a = atr(df, period)
    if a.empty or pd.isna(a.iloc[-1]) or df["close"].iloc[-1] <= 0:
        return 2.0
    return float(a.iloc[-1] / df["close"].iloc[-1] * 100.0)


def enrich(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """Attach all indicators needed for signal evaluation."""
    out = df.copy()
    out["ema_trend"] = ema(out["close"], cfg.TREND_EMA_PERIOD)
    out["rsi"] = rsi(out["close"], cfg.RSI_PERIOD)
    out["atr"] = atr(out, cfg.ATR_PERIOD)
    upper, mid, lower = bollinger(out["close"], cfg.BB_PERIOD, cfg.BB_STD)
    out["bb_upper"] = upper
    out["bb_mid"] = mid
    out["bb_lower"] = lower
    out["vol_ma"] = sma(out["volume"], cfg.VOLUME_MA_PERIOD)
    out["vol_ratio"] = out["volume"] / out["vol_ma"].replace(0, np.nan)
    return out


def recent_swing_low(df: pd.DataFrame, lookback: int = 20, left: int = 2, right: int = 2) -> Optional[float]:
    """Lowest confirmed swing low in the last `lookback` bars (excluding forming)."""
    swings = recent_swing_lows(df, lookback=lookback, left=left, right=right)
    if not swings:
        if len(df) == 0:
            return None
        return float(df["low"].iloc[-min(10, len(df)) :].min())
    return min(swings)


def recent_swing_lows(
    df: pd.DataFrame, lookback: int = 40, left: int = 2, right: int = 2
) -> list[float]:
    """Confirmed swing lows (local unique mins), oldest → newest."""
    if df is None or len(df) < left + right + 3:
        return []
    if len(df) < lookback + left + right + 1:
        lookback = max(5, len(df) - left - right - 1)
    if lookback < 5:
        return [float(df["low"].iloc[-min(10, len(df)) :].min())]

    window = df.iloc[-(lookback + right) : -right if right else None]
    lows = window["low"].values
    swings: list[float] = []
    for i in range(left, len(lows) - right):
        seg = lows[i - left : i + right + 1]
        if lows[i] == seg.min() and list(seg).count(lows[i]) == 1:
            swings.append(float(lows[i]))
    if not swings:
        swings.append(float(window["low"].min()))
    return swings


def last_higher_low(
    df: pd.DataFrame,
    price: float,
    lookback: int = 40,
    left: int = 2,
    right: int = 2,
) -> Optional[float]:
    """
    Most recent confirmed swing low that is still structural support
    (below last price). Prefer the latest HL so trail ratchets up.
    """
    swings = recent_swing_lows(df, lookback=lookback, left=left, right=right)
    if not swings:
        return None
    # newest first among lows that sit under price
    under = [s for s in reversed(swings) if s < price * 0.9995]
    if not under:
        return None
    return under[0]


def structure_trail_stop(
    df: pd.DataFrame,
    price: float,
    peak: float,
    entry: float,
    atr_v: float,
    swing_buffer_atr: float = 0.25,
    max_giveback_atr: float = 2.4,
    atr_trail_mult: float = 1.75,
) -> tuple[float, str]:
    """
    Trail long under real support (last higher-low), with ATR fallback.

    Philosophy: let winners run while higher-lows hold; only force a tighter
    stop if structure would give back more than max_giveback_atr under peak.

    Returns (stop_price, reason). Caller enforces BE (never below entry).
    """
    atr_v = max(float(atr_v or 0), abs(price) * 0.002)
    peak = max(float(peak), float(price), float(entry))
    atr_stop = peak - atr_v * atr_trail_mult
    max_giveback_stop = peak - atr_v * max_giveback_atr

    hl = last_higher_low(df, price)
    if hl is not None and hl < price:
        struct = hl - atr_v * swing_buffer_atr
        # Prefer structure (often looser → longer run); floor at max giveback
        stop = max(struct, max_giveback_stop)
        if struct >= max_giveback_stop:
            reason = f"HL support {hl:.6g}"
        else:
            reason = f"HL {hl:.6g} capped (max giveback {max_giveback_atr:.1f} ATR)"
        return stop, reason

    return atr_stop, "ATR trail (no HL)"


def resistance_levels(
    df: pd.DataFrame, price: float, lookback: int = 40, left: int = 2, right: int = 2
) -> list[float]:
    """Swing highs above current price, nearest first."""
    if len(df) < lookback + left + right + 1:
        lookback = max(8, len(df) - left - right - 1)
    window = df.iloc[-(lookback + right) : -right if right else None]
    highs = window["high"].values
    levels: list[float] = []
    for i in range(left, len(highs) - right):
        seg = highs[i - left : i + right + 1]
        h = float(highs[i])
        if h == seg.max() and h > price * 1.001:
            levels.append(h)
    # also recent range high
    rh = float(window["high"].max())
    if rh > price * 1.001:
        levels.append(rh)
    # unique, nearest first
    levels = sorted(set(round(x, 10) for x in levels))
    levels = [x for x in levels if x > price]
    levels.sort()
    return levels
