"""
One-shot OKX connectivity + tiny open/close test.

Enable only via Railway:
  OKX_SMOKE_TEST=true

Then remove the variable (and we delete this file from the repo after).
Does NOT run during normal operation.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import config
from bot.executor import MexcExecutor
from bot.telegram_notifier import send_status

log = logging.getLogger("bot.smoke")

# Prefer liquid majors with small min notional
_SMOKE_SYMBOLS = (
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "DOGE/USDT:USDT",
)


def run_okx_smoke_test(executor: MexcExecutor) -> dict[str, Any]:
    """
    1) Balance
    2) Min-size market LONG on first workable symbol
    3) Immediate market close
    Reports result to logs + Telegram.
    """
    result: dict[str, Any] = {"ok": False, "steps": []}

    def step(msg: str) -> None:
        log.info("SMOKE | %s", msg)
        result["steps"].append(msg)

    if not executor.enabled or not executor.ex:
        step("FAIL: executor not enabled (keys / AUTO_TRADE)")
        send_status("🧪 OKX smoke test FAIL: auto-trade/keys not ready")
        return result

    ex = executor.ex
    step(f"host={getattr(config, 'OKX_HOSTNAME', '?')}")

    # --- balance ---
    try:
        mb = executor.fetch_margin_balances()
        step(f"balance: {mb}")
        if not mb or (mb.get("total_free") or 0) < 5:
            step("WARN: free margin looks low; open may fail")
    except Exception as e:
        step(f"FAIL balance: {e}")
        send_status(f"🧪 OKX smoke FAIL at balance:\n{e}")
        return result

    # --- pick symbol + min size ---
    symbol = None
    qty = None
    for sym in _SMOKE_SYMBOLS:
        try:
            if sym not in ex.markets:
                continue
            m = ex.market(sym)
            mn = float((m.get("limits") or {}).get("amount", {}).get("min") or 1)
            q = float(ex.amount_to_precision(sym, mn))
            if q <= 0:
                continue
            # rough notional check
            t = ex.fetch_ticker(sym)
            px = float(t.get("last") or t.get("close") or 0)
            csize = float(m.get("contractSize") or 1)
            notional = q * csize * px
            if notional > 80:
                # too big for a smoke on tight balance — try next
                step(f"skip {sym}: min notional≈${notional:.0f} too large")
                continue
            symbol, qty = sym, q
            step(f"chosen {sym} qty={q} notional≈${notional:.2f}")
            break
        except Exception as e:
            step(f"skip {sym}: {e}")

    if not symbol or not qty:
        step("FAIL: no suitable min-size symbol")
        send_status("🧪 OKX smoke FAIL: no small test market available")
        return result

    # --- leverage / mode ---
    try:
        executor._ensure_net_mode()
        executor._set_leverage(symbol, min(3, config.LEVERAGE_MAX))
        step("leverage/mode set (3x or less)")
    except Exception as e:
        step(f"WARN leverage: {e}")

    # --- open ---
    try:
        order = ex.create_order(
            symbol,
            "market",
            "buy",
            qty,
            None,
            {"tdMode": "isolated", "marginMode": "isolated"},
        )
        oid = order.get("id")
        step(f"OPEN ok id={oid}")
    except Exception as e:
        step(f"FAIL open: {e}")
        send_status(
            f"🧪 OKX smoke FAIL on OPEN\n"
            f"{symbol} qty={qty}\n{e}\n\n"
            f"If 50124: enable Trade + futures on the API key."
        )
        result["error"] = str(e)
        return result

    time.sleep(1.5)

    # --- close ---
    try:
        still, rem = executor._position_remaining(symbol)
        close_qty = rem if still and rem > 0 else qty
        close_qty = float(ex.amount_to_precision(symbol, close_qty))
        ex.create_order(
            symbol,
            "market",
            "sell",
            close_qty,
            None,
            {
                "tdMode": "isolated",
                "marginMode": "isolated",
                "reduceOnly": True,
            },
        )
        step(f"CLOSE ok qty={close_qty}")
        time.sleep(0.8)
        still2, rem2 = executor._position_remaining(symbol)
        if still2 and rem2 > 0:
            step(f"WARN residual position rem={rem2} — check OKX")
        else:
            step("flat — no residual position")
    except Exception as e:
        step(f"FAIL close: {e}")
        send_status(
            f"🧪 OKX smoke OPENED but CLOSE failed\n"
            f"{symbol}\n{e}\n"
            f"Check OKX and close manually if needed!"
        )
        result["error"] = str(e)
        return result

    result["ok"] = True
    result["symbol"] = symbol
    summary = "\n".join(f"• {s}" for s in result["steps"])
    send_status(
        f"🧪 OKX smoke test PASS\n"
        f"Opened+closed min size on {symbol}\n"
        f"Trading API works.\n\n{summary}\n\n"
        f"Now remove OKX_SMOKE_TEST from Railway variables."
    )
    log.info("SMOKE PASS %s", result)
    return result
