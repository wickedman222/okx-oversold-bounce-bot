"""
Liquid Oversold Bounce Scanner.

Logic (LONG only):
  1. Higher TF (4h): price above rising EMA50 → uptrend still intact
  2. Signal TF (15m): deeply oversold RSI + near/below lower Bollinger
  3. Bounce clues: RSI turning up, bullish (or recovering) close, volume OK
  4. ATR-based SL / multi-TP, min RR filter
  5. Confidence score; only fire if >= MIN_CONFIDENCE
  6. BTC dump filter for alts

Uses *closed* candles only (exchange layer drops the live bar).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

import config
from bot.indicators import (
    atr_pct,
    enrich,
    recent_swing_low,
    resistance_levels,
)
from bot.leverage import LeverageAdvice, recommend_leverage

log = logging.getLogger("bot.scanner")


@dataclass
class Signal:
    symbol: str
    direction: str  # "LONG"
    entry: float
    entry_zone_low: float
    entry_zone_high: float
    stop: float
    sl_pct: float
    tp1: float
    tp2: float
    tp3: float
    tp1_pct: float
    tp2_pct: float
    tp3_pct: float
    rr_tp1: float
    rr_tp2: float
    rr_tp3: float
    leverage: int
    leverage_reason: str
    position_size_usd: float
    notional_usd: float
    confidence: float
    atr_pct: float
    rsi: float
    volume_ratio: float
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    timeframe: str = config.SIGNAL_TIMEFRAME
    trend_timeframe: str = config.TREND_TIMEFRAME
    allow_runner: bool = False  # trail past TP2 on high-confidence structure
    trail_atr: float = 0.0


def _pct(a: float, b: float) -> float:
    """Percent change from a → b."""
    if a == 0:
        return 0.0
    return (b - a) / a * 100.0


def _htf_uptrend(df4h: pd.DataFrame) -> tuple[bool, list[str]]:
    d = enrich(df4h, config)
    if len(d) < config.TREND_EMA_PERIOD + config.TREND_SLOPE_BARS + 2:
        return False, ["insufficient 4h history"]

    last = d.iloc[-1]
    reasons = []
    if last["close"] <= last["ema_trend"]:
        return False, ["4h close below EMA50"]
    reasons.append(
        f"4h uptrend: close {last['close']:.6g} > EMA{config.TREND_EMA_PERIOD} {last['ema_trend']:.6g}"
    )

    if config.TREND_REQUIRE_SLOPE_UP:
        ema_now = float(d["ema_trend"].iloc[-1])
        ema_prev = float(d["ema_trend"].iloc[-1 - config.TREND_SLOPE_BARS])
        if ema_now < ema_prev:
            return False, ["4h EMA50 sloping down"]
        reasons.append("4h EMA50 rising")

    return True, reasons


def _score_setup(
    rsi_v: float,
    near_bb: bool,
    vol_ratio: float,
    rsi_up: bool,
    bullish: bool,
    atr_p: float,
    rr_tp2: float,
    sl_pct: float,
) -> tuple[float, list[str]]:
    """0–100 confidence. Selective by design."""
    score = 0.0
    bits: list[str] = []

    # RSI depth
    if rsi_v <= 22:
        score += 28
        bits.append(f"RSI extremely oversold ({rsi_v:.1f})")
    elif rsi_v <= 28:
        score += 22
        bits.append(f"RSI deeply oversold ({rsi_v:.1f})")
    elif rsi_v <= config.RSI_OVERSOLD:
        score += 15
        bits.append(f"RSI oversold ({rsi_v:.1f})")
    else:
        return 0.0, ["RSI not oversold"]

    if near_bb:
        score += 18
        bits.append("price at/under lower Bollinger")
    else:
        score += 5
        bits.append("oversold but not at lower BB (weaker)")

    if rsi_up:
        score += 14
        bits.append("RSI turning up")
    else:
        score -= 8

    if bullish:
        score += 12
        bits.append("bullish/recovering closed candle")
    else:
        score -= 6

    if vol_ratio >= 1.5:
        score += 12
        bits.append(f"strong volume ({vol_ratio:.2f}x avg)")
    elif vol_ratio >= config.VOLUME_MIN_RATIO:
        score += 7
        bits.append(f"adequate volume ({vol_ratio:.2f}x)")
    else:
        score -= 10
        bits.append(f"weak volume ({vol_ratio:.2f}x)")

    # Structure quality
    if rr_tp2 >= 2.5:
        score += 10
    elif rr_tp2 >= 2.0:
        score += 6
    elif rr_tp2 >= config.MIN_RR_TO_TP2:
        score += 3

    if atr_p < 1.0:
        score += 6
        bits.append("low volatility favors bounce clarity")
    elif atr_p > 2.5:
        score -= 8
        bits.append("high volatility — bounce less reliable")

    if sl_pct > 2.8:
        score -= 10

    score = max(0.0, min(100.0, score))
    return score, bits


def evaluate_pair(
    symbol: str,
    df_sig: pd.DataFrame,
    df_trend: pd.DataFrame,
    btc_rsi: Optional[float] = None,
) -> Optional[Signal]:
    """Return a Signal if this pair qualifies; else None."""

    # BTC dump filter for non-BTC
    if (
        config.BTC_FILTER_ENABLED
        and symbol != config.BTC_SYMBOL
        and btc_rsi is not None
        and btc_rsi < config.BTC_DUMP_RSI_MAX
    ):
        log.debug("Skip %s — BTC RSI %.1f dump filter", symbol, btc_rsi)
        return None

    ok_trend, trend_reasons = _htf_uptrend(df_trend)
    if not ok_trend:
        return None

    d = enrich(df_sig, config)
    if len(d) < max(config.BB_PERIOD, config.RSI_PERIOD, config.ATR_PERIOD) + 5:
        return None

    last = d.iloc[-1]
    prev = d.iloc[-2]
    price = float(last["close"])
    rsi_v = float(last["rsi"])
    rsi_prev = float(prev["rsi"])
    atr_v = float(last["atr"])
    bb_lower = float(last["bb_lower"])
    vol_ratio = float(last["vol_ratio"]) if pd.notna(last["vol_ratio"]) else 0.0

    # Hard gates
    if rsi_v > config.RSI_OVERSOLD:
        return None

    near_bb = price <= bb_lower * (1 + config.BB_TOUCH_PCT / 100.0)
    if config.REQUIRE_NEAR_LOWER_BB and not near_bb:
        return None

    rsi_up = rsi_v > rsi_prev
    if config.REQUIRE_RSI_TURNING_UP and not rsi_up:
        return None

    bullish = float(last["close"]) >= float(last["open"])
    # Allow slight red if long lower wick (recovery from lows)
    wick_recovery = (float(last["close"]) - float(last["low"])) > 0.55 * (
        float(last["high"]) - float(last["low"]) + 1e-12
    )
    if config.REQUIRE_BULLISH_CLOSE and not (bullish or wick_recovery):
        return None

    if vol_ratio < config.VOLUME_MIN_RATIO * 0.75:
        # too dead — skip early
        return None

    # --- Stops & targets: ATR floor + real structure (swing low / resistance) ---
    if atr_v <= 0 or price <= 0:
        return None

    atr_sl_dist = atr_v * config.SL_ATR_MULT
    atr_stop = price - atr_sl_dist
    structure_notes: list[str] = []

    if getattr(config, "USE_STRUCTURE_TARGETS", True):
        swing_lo = recent_swing_low(d, lookback=getattr(config, "SWING_LOOKBACK", 24))
        # SL just under swing low (support), but not wider than max SL
        if swing_lo is not None and swing_lo < price:
            struct_stop = swing_lo - atr_v * 0.15
            # Prefer structural support if it's not absurdly far
            if struct_stop < price:
                # use the tighter of ATR stop and structure? No — structure support
                # is better: place stop under support (may be slightly wider than ATR)
                stop_candidate = min(atr_stop, struct_stop)
                structure_notes.append(
                    f"SL under swing support {swing_lo:.6g} (ATR stop was {atr_stop:.6g})"
                )
            else:
                stop_candidate = atr_stop
        else:
            stop_candidate = atr_stop
    else:
        stop_candidate = atr_stop

    sl_pct = (price - stop_candidate) / price * 100.0
    sl_pct = max(config.MIN_SL_PCT, min(config.MAX_SL_PCT, sl_pct))
    if (price - stop_candidate) / price * 100.0 > config.MAX_SL_PCT + 0.05:
        # structure too wide — fall back to ATR clamp
        stop_candidate = price * (1 - config.MAX_SL_PCT / 100.0)
        sl_pct = config.MAX_SL_PCT
        structure_notes.append("structure SL capped by MAX_SL_PCT")

    stop = price * (1 - sl_pct / 100.0) if stop_candidate >= price else stop_candidate
    # re-clamp stop to sl_pct bounds
    stop = min(stop, price * (1 - config.MIN_SL_PCT / 100.0))
    stop = max(stop, price * (1 - config.MAX_SL_PCT / 100.0))
    sl_pct = (price - stop) / price * 100.0
    risk = price - stop
    if risk <= 0:
        return None

    # R-based floors
    tp1_r = price + risk * config.TP1_R
    tp2_r = price + risk * config.TP2_R
    tp3_r = price + risk * config.TP3_R

    bb_mid = float(last["bb_mid"]) if pd.notna(last["bb_mid"]) else price
    bb_upper = float(last["bb_upper"]) if pd.notna(last["bb_upper"]) else tp3_r

    # Structure targets: nearest resistance / BB mid / prior swing highs
    if getattr(config, "USE_STRUCTURE_TARGETS", True):
        resists = resistance_levels(
            d, price, lookback=getattr(config, "RESISTANCE_LOOKBACK", 48)
        )
        # TP1: bank at BB mid mean-reversion or first resistance, at least 1R
        tp1_candidates = [tp1_r]
        if bb_mid > price + risk * 0.8:
            tp1_candidates.append(bb_mid)
        if resists:
            # first resistance if not too close/far
            r0 = resists[0]
            if r0 >= price + risk * 0.9:
                tp1_candidates.append(r0)
        # Take earliest reasonable bank (min above 0.9R)
        tp1 = min(x for x in tp1_candidates if x >= price + risk * 0.9)
        structure_notes.append(f"TP1 structure/mean-rev @ {tp1:.6g}")

        # TP2: next resistance or 2.2R floor — allow structure to push higher
        tp2 = tp2_r
        if resists:
            for r in resists:
                if r >= price + risk * config.MIN_RR_TO_TP2:
                    # extend toward structure if better than pure R
                    tp2 = max(tp2_r, min(r, price + risk * config.TP3_R * 1.15))
                    structure_notes.append(f"TP2 toward resistance {r:.6g}")
                    break
        if bb_upper > tp2:
            # allow stretch to BB upper as runner zone
            tp3 = max(tp3_r, bb_upper)
        else:
            tp3 = max(tp3_r, tp2 + risk * 0.8)
        if len(resists) >= 2 and resists[1] > tp2:
            tp3 = max(tp3, resists[1])
            structure_notes.append(f"TP3/runner resistance {tp3:.6g}")
    else:
        tp1, tp2, tp3 = tp1_r, tp2_r, tp3_r

    # Ensure order TP1 < TP2 < TP3
    if tp2 <= tp1:
        tp2 = tp1 + risk * 0.5
    if tp3 <= tp2:
        tp3 = tp2 + risk * 0.5

    rr1 = (tp1 - price) / risk
    rr2 = (tp2 - price) / risk
    rr3 = (tp3 - price) / risk

    if rr2 < config.MIN_RR_TO_TP2:
        return None

    atr_p = atr_pct(df_sig, config.ATR_PERIOD)
    conf, score_bits = _score_setup(
        rsi_v=rsi_v,
        near_bb=near_bb,
        vol_ratio=vol_ratio,
        rsi_up=rsi_up,
        bullish=bullish or wick_recovery,
        atr_p=atr_p,
        rr_tp2=rr2,
        sl_pct=sl_pct,
    )

    # Bonus for clean structure room to run
    if rr2 >= 2.8:
        conf = min(100.0, conf + 4)
        score_bits.append(f"room to run (RR→TP2 {rr2:.2f})")

    if conf < config.MIN_CONFIDENCE:
        log.debug("%s conf %.0f < min %d", symbol, conf, config.MIN_CONFIDENCE)
        return None

    lev_adv: LeverageAdvice = recommend_leverage(atr_p, sl_pct, conf, rr2)
    allow_runner = conf >= getattr(config, "RUNNER_IF_CONF_GE", 78)
    trail_atr = atr_v * getattr(config, "TRAIL_ATR_MULT", 1.6)

    # Entry zone: current close ± small band (limit-friendly)
    zone_pad = max(price * 0.0015, atr_v * 0.15)
    entry_low = price - zone_pad
    entry_high = price + zone_pad * 0.35  # prefer not chasing up

    reasons = list(trend_reasons) + score_bits + structure_notes
    notes = [
        f"Signal TF: {config.SIGNAL_TIMEFRAME} | Trend TF: {config.TREND_TIMEFRAME}",
        f"ATR(14)={atr_v:.6g} ({atr_p:.2f}% of price)",
        f"Partials: TP1 {config.TP1_SIZE_PCT}% / TP2 {config.TP2_SIZE_PCT}% / runner {config.TP3_SIZE_PCT}%",
        "Targets blend R-multiples + swing resistance / BB mid (structure).",
        "Isolated margin (USDC). After TP1, runner trails under peak.",
    ]
    if allow_runner:
        notes.append(
            f"High conf ({conf:.0f}) — after TP1, trail past TP2 (let winners run)."
        )
    if btc_rsi is not None and not symbol.startswith("BTC/"):
        notes.append(f"BTC 15m RSI={btc_rsi:.1f} (filter passed)")
    if atr_p > 2.0:
        notes.append("⚠ Elevated volatility — leverage reduced; watch funding.")
    if conf >= 80:
        notes.append("High-conviction setup within this model.")

    return Signal(
        symbol=symbol,
        direction="LONG",
        entry=price,
        entry_zone_low=entry_low,
        entry_zone_high=entry_high,
        stop=stop,
        sl_pct=sl_pct,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        tp1_pct=_pct(price, tp1),
        tp2_pct=_pct(price, tp2),
        tp3_pct=_pct(price, tp3),
        rr_tp1=rr1,
        rr_tp2=rr2,
        rr_tp3=rr3,
        leverage=lev_adv.leverage,
        leverage_reason=lev_adv.reason,
        position_size_usd=config.POSITION_SIZE_USD,
        notional_usd=config.POSITION_SIZE_USD * lev_adv.leverage,
        confidence=conf,
        atr_pct=atr_p,
        rsi=rsi_v,
        volume_ratio=vol_ratio,
        reasons=reasons,
        notes=notes,
        allow_runner=allow_runner,
        trail_atr=trail_atr,
    )
