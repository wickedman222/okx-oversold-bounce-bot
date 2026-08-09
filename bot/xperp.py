"""
OKX Europe X-Perps helpers.

EEA retail cannot trade global USDT-SWAP (API 50124).
EU derivatives are X-Perps: instType=FUTURES, ruleType=xperp,
instId like BTC-USD_UM_XPERP-310404, linear, settle USD (USDC margin).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import config

log = logging.getLogger("bot.xperp")


def load_crypto_xperps(ex) -> list[dict[str, Any]]:
    """Return live crypto X-Perp instrument dicts from OKX public API."""
    try:
        r = ex.publicGetPublicInstruments({"instType": "FUTURES"})
    except Exception as e:
        log.error("instruments FUTURES: %s", e)
        return []
    out = []
    for d in r.get("data") or []:
        if d.get("state") != "live":
            continue
        if d.get("ruleType") != "xperp" and "XPERP" not in str(d.get("instId") or ""):
            continue
        # 1 = crypto, 3 = equity, 4 = commodity
        if str(d.get("instCategory") or "1") not in ("1", ""):
            continue
        out.append(d)
    log.info("Loaded %d crypto X-Perp instruments", len(out))
    return out


def inst_to_ccxt_symbol(ex, inst: dict[str, Any]) -> Optional[str]:
    """Map OKX inst row → ccxt unified symbol if present in markets."""
    inst_id = inst.get("instId")
    if not inst_id:
        return None
    # Prefer markets_by_id
    try:
        markets_by_id = getattr(ex, "markets_by_id", None) or {}
        m = markets_by_id.get(inst_id)
        if isinstance(m, list) and m:
            return m[0].get("symbol")
        if isinstance(m, dict):
            return m.get("symbol")
    except Exception:
        pass
    # Fallback scan
    for s, m in (ex.markets or {}).items():
        if m.get("id") == inst_id:
            return s
    # Construct typical ccxt form: BTC/USD:USD-310404
    # instId BTC-USD_UM_XPERP-310404
    try:
        parts = inst_id.split("-")
        # BTC-USD_UM_XPERP-310404
        if len(parts) >= 3 and "XPERP" in inst_id:
            base = parts[0]
            expiry = parts[-1]
            return f"{base}/USD:USD-{expiry}"
    except Exception:
        pass
    return None


def build_xperp_watchlist(ex, top_n: int | None = None) -> list[str]:
    """
    Rank crypto X-Perps by 24h volume and return ccxt symbols.
    Always include major bases if listed.
    """
    top_n = top_n or config.TOP_N_PAIRS
    insts = load_crypto_xperps(ex)
    if not insts:
        return []

    # Volume from futures tickers
    vol_by_id: dict[str, float] = {}
    try:
        t = ex.publicGetMarketTickers({"instType": "FUTURES"})
        for row in t.get("data") or []:
            iid = row.get("instId")
            try:
                # volCcy24h often quote volume in USD
                v = float(row.get("volCcy24h") or row.get("vol24h") or 0)
            except (TypeError, ValueError):
                v = 0.0
            if iid:
                vol_by_id[iid] = v
    except Exception as e:
        log.warning("xperp tickers: %s", e)

    ranked: list[tuple[str, float, dict]] = []
    for inst in insts:
        iid = inst["instId"]
        base = (inst.get("ctValCcy") or iid.split("-")[0] or "").upper()
        if base in getattr(config, "EXCLUDE_BASES", ()):
            continue
        if any(k.strip("/").upper() == base for k in getattr(config, "EXCLUDE_KEYWORDS", ()) if False):
            pass
        # skip equity-like bases already filtered by instCategory
        sym = inst_to_ccxt_symbol(ex, inst)
        if not sym:
            continue
        if sym in getattr(config, "blocked", set()):
            continue
        ranked.append((sym, vol_by_id.get(iid, 0.0), inst))

    ranked.sort(key=lambda x: x[1], reverse=True)

    # Prefer one symbol per base (highest vol / first)
    seen_base: set[str] = set()
    out: list[str] = []
    force_bases = [
        "BTC", "ETH", "SOL", "XRP", "DOGE", "AVAX", "SUI", "LINK", "BNB", "ADA",
    ]
    # Map force bases first if available
    by_base: dict[str, str] = {}
    for sym, _v, inst in ranked:
        base = (inst.get("ctValCcy") or sym.split("/")[0]).upper()
        if base not in by_base:
            by_base[base] = sym

    for b in force_bases:
        if b in by_base and by_base[b] not in out:
            out.append(by_base[b])
            seen_base.add(b)

    for sym, _v, inst in ranked:
        base = (inst.get("ctValCcy") or sym.split("/")[0]).upper()
        if base in seen_base:
            continue
        out.append(sym)
        seen_base.add(base)
        if len(out) >= top_n:
            break

    log.info("X-Perp watchlist: %d symbols (eg %s)", len(out), out[:5])
    return out


def resolve_btc_symbol(ex) -> str:
    """BTC X-Perp ccxt symbol or fallback."""
    for s in build_xperp_watchlist(ex, top_n=50):
        if s.startswith("BTC/"):
            return s
    return "BTC/USD:USD-310404"
