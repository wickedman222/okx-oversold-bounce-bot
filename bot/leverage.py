"""
Variable leverage recommendation for $50 fixed-margin signals.

Philosophy for small accounts:
  - Higher leverage only on CLEAN, low-volatility, high-confidence setups.
  - Choppy / high ATR → lower leverage so the SL % does not liquidate you.
  - Cap hard — revenge-leverage kills small accounts faster than bad entries.

Notional exposure ≈ POSITION_SIZE_USD * leverage.
Liquidation is about % adverse move vs leverage + maintenance margin,
not about the $50 itself. Wide SL + high leverage = death.
"""

from __future__ import annotations

from dataclasses import dataclass

import config


@dataclass
class LeverageAdvice:
    leverage: int
    reason: str
    risk_score: float  # 0 = calm, 100 = dangerous


def recommend_leverage(
    atr_pct: float,
    sl_pct: float,
    confidence: float,
    rr_to_tp2: float,
) -> LeverageAdvice:
    """
    Pick integer leverage in [LEVERAGE_MIN, LEVERAGE_MAX].

    Rules of thumb:
      - ATR% low + high confidence + solid RR → higher lev
      - Wide stop relative to ATR or high ATR% → lower lev
      - Never recommend lev where SL is past ~70% of liquidation buffer
        (rough: liq ≈ 100/lev %; we want SL_pct * 1.3 < 100/lev)
    """
    lo, hi = config.LEVERAGE_MIN, min(10, config.LEVERAGE_MAX)  # OKX perp max 10x
    base = min(hi, config.LEVERAGE_DEFAULT)

    # Volatility score: 0 calm → 100 wild (ATR% typical crypto 0.5–4% on 15m)
    vol_score = min(100.0, max(0.0, (atr_pct - 0.4) / 3.0 * 100.0))

    # Confidence boost
    conf_boost = 0
    if confidence >= 85:
        conf_boost = 3
    elif confidence >= 75:
        conf_boost = 2
    elif confidence >= 68:
        conf_boost = 1

    # RR quality
    rr_boost = 0
    if rr_to_tp2 >= 2.5:
        rr_boost = 2
    elif rr_to_tp2 >= 2.0:
        rr_boost = 1

    # Volatility penalty
    if atr_pct >= 2.5:
        vol_pen = 4
    elif atr_pct >= 1.8:
        vol_pen = 3
    elif atr_pct >= 1.2:
        vol_pen = 2
    elif atr_pct >= 0.8:
        vol_pen = 1
    else:
        vol_pen = 0

    # Wide stop penalty (harder to hold with leverage)
    if sl_pct >= 2.5:
        sl_pen = 3
    elif sl_pct >= 1.8:
        sl_pen = 2
    elif sl_pct >= 1.2:
        sl_pen = 1
    else:
        sl_pen = 0

    raw = base + conf_boost + rr_boost - vol_pen - sl_pen
    lev = int(max(lo, min(hi, raw)))

    # Safety: SL should stay well inside rough liquidation distance
    # Isolated futures rough liq distance ≈ 100/lev % (ignoring fees/mmr)
    max_safe_lev = int(max(lo, min(hi, 100.0 / (sl_pct * 1.5 + 1e-9))))
    if lev > max_safe_lev:
        lev = max_safe_lev

    # Build human reason
    bits = []
    if atr_pct < 0.9:
        bits.append(f"low ATR ({atr_pct:.2f}%)")
    elif atr_pct > 1.8:
        bits.append(f"elevated ATR ({atr_pct:.2f}%) → reduced lev")
    else:
        bits.append(f"moderate ATR ({atr_pct:.2f}%)")

    bits.append(f"confidence {confidence:.0f}")
    bits.append(f"SL {sl_pct:.2f}%")
    bits.append(f"RR→TP2 {rr_to_tp2:.2f}")

    reason = (
        f"{lev}x isolated recommended — "
        + ", ".join(bits)
        + f". Use fixed ${config.POSITION_SIZE_USD:.0f} margin → notional ≈ ${config.POSITION_SIZE_USD * lev:.0f}."
    )

    risk_score = min(100.0, vol_score * 0.5 + sl_pct * 15 + max(0, lev - 5) * 5)
    return LeverageAdvice(leverage=lev, reason=reason, risk_score=risk_score)
