"""
Phase 2 stub — MEXC automatic execution.

Currently a no-op interface. When you enable AUTO_TRADE and set API keys:

  1. Implement place_signal() using ccxt private methods
  2. Use isolated margin, set leverage, market/limit entry
  3. Place SL + TP reduce-only orders
  4. Wire into main.py after send_signal()

Keep Phase 1 pure-signal by leaving AUTO_TRADE=false.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import config
from bot.exchange import make_exchange
from bot.scanner import Signal

log = logging.getLogger("bot.executor")


class MexcExecutor:
    """
    Clean upgrade path for live trading.

    Design notes for $50 isolated:
      - set_leverage(symbol, lev)
      - set_margin_mode(symbol, 'isolated') if supported
      - amount = notional / price  (respect contract size / precision)
      - always place stop-loss immediately after fill
      - never average down without a new signal + lock rules
    """

    def __init__(self) -> None:
        self.enabled = bool(config.AUTO_TRADE and config.MEXC_API_KEY and config.MEXC_API_SECRET)
        self.ex = None
        if self.enabled:
            self.ex = make_exchange(private=True)
            log.warning("AUTO_TRADE is ON — real orders will be attempted")
        else:
            log.info("Executor idle (signal-only mode)")

    def place_signal(self, sig: Signal) -> dict[str, Any]:
        if not self.enabled or self.ex is None:
            return {"ok": False, "reason": "auto_trade_disabled"}

        # --- Implement carefully before going live ---
        # Example skeleton (DO NOT enable until tested on tiny size):
        #
        # self.ex.set_leverage(sig.leverage, sig.symbol)
        # try:
        #     self.ex.set_margin_mode('isolated', sig.symbol)
        # except Exception:
        #     pass
        # amount = self.ex.amount_to_precision(
        #     sig.symbol, sig.notional_usd / sig.entry
        # )
        # order = self.ex.create_order(sig.symbol, 'market', 'buy', amount)
        # ... place SL / TP reduce-only ...
        #
        log.error(
            "place_signal called but live order logic is intentionally stubbed. "
            "Implement after paper-testing signals."
        )
        return {"ok": False, "reason": "not_implemented"}

    def close_all(self, symbol: str) -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "reason": "auto_trade_disabled"}
        return {"ok": False, "reason": "not_implemented"}


def build_executor() -> MexcExecutor:
    return MexcExecutor()
