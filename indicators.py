"""
Technical indicators used by the oversold-bounce strategy.

Pure pandas/numpy — no TA-Lib dependency so install stays simple.
All functions expect OHLCV DataFrames with columns:
  open, high, low, close, volume
"""

from __future__ import annotations

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
