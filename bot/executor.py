"""
Phase 2 — MEXC USDT-M auto execution (hardened).

Rules:
  - FIXED margin = POSITION_SIZE_USD ($50) per trade
  - Isolated + variable leverage
  - Max 1 trade managed by this bot

Exit design (avoids common MEXC "failed trigger" cases):
  - Exchange: ONE stop (full size) + ONE take-profit (full size @ TP2)
  - Software: TP1 partial optional, then re-arm SL for remaining
  - Always close using LIVE position size from exchange (not stale state)
  - Never treat position-fetch errors as "flat"
"""

from __future__ import annotations

import logging
import math
import time
import traceback
from typing import Any, Optional

import ccxt

import config
from bot.exchange import make_exchange
from bot.scanner import Signal
from bot.trade_state import OpenSignal

log = logging.getLogger("bot.executor")


def _fmt_px(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.6f}"
    return f"{p:.8f}".rstrip("0").rstrip(".")


class MexcExecutor:
    def __init__(self) -> None:
        self.keys_ok = bool(config.MEXC_API_KEY and config.MEXC_API_SECRET)
        self.enabled = bool(config.AUTO_TRADE and self.keys_ok)
        self.ex: Optional[ccxt.Exchange] = None
        if self.keys_ok:
            self.ex = make_exchange(private=True)
            try:
                self.ex.load_markets()
            except Exception as e:
                log.warning("Private markets load: %s", e)
        if self.enabled:
            log.warning(
                "AUTO_TRADE ON — live MEXC orders | margin=$%.0f isolated",
                config.POSITION_SIZE_USD,
            )
        elif self.keys_ok:
            log.info("MEXC keys present but AUTO_TRADE=false (signal-only)")
        else:
            log.info("Executor idle (no keys / signal-only)")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def place_signal(self, sig: Signal) -> dict[str, Any]:
        if not self.enabled or self.ex is None:
            return {"ok": False, "reason": "auto_trade_disabled"}
        if sig.direction != "LONG":
            return {"ok": False, "reason": "long_only"}

        try:
            if self._has_any_bot_conflict(sig.symbol):
                return {"ok": False, "reason": "position_already_open"}

            self._set_leverage(sig.symbol, sig.direction, sig.leverage)
            qty = self._size_contracts(sig.symbol, sig.entry, sig.leverage)
            params = {
                "openType": 1 if config.MARGIN_MODE == "isolated" else 2,
                "hedged": False,
                "leverage": sig.leverage,
            }
            log.info(
                "OPEN LONG %s qty=%s lev=%dx margin=$%.0f",
                sig.symbol,
                qty,
                sig.leverage,
                config.POSITION_SIZE_USD,
            )
            order = self.ex.create_order(sig.symbol, "market", "buy", qty, None, params)
            fill = float(order.get("average") or order.get("price") or sig.entry)
            time.sleep(1.2)

            # Re-read filled size from exchange if possible
            still, live_qty = self._position_remaining(sig.symbol)
            if still and live_qty > 0:
                qty = live_qty

            m = self.ex.market(sig.symbol)
            csize = float(m.get("contractSize") or 1)
            notional = qty * csize * fill
            margin = notional / max(sig.leverage, 1)

            # --- Protective orders: full-size SL + full-size TP2 only ---
            # Split TP1/TP2 triggers with overlapping SL qty is a top cause of
            # MEXC "trigger failed" after partial fills.
            sl_id = self._place_trigger(
                sig.symbol, sig.direction, qty, sig.stop, is_stop=True, leverage=sig.leverage
            )
            tp_id = self._place_trigger(
                sig.symbol, sig.direction, qty, sig.tp2, is_stop=False, leverage=sig.leverage
            )

            warnings = []
            if not sl_id:
                warnings.append("SL_TRIGGER_FAILED")
            if not tp_id:
                warnings.append("TP_TRIGGER_FAILED")

            trade = OpenSignal(
                symbol=sig.symbol,
                direction=sig.direction,
                entry=fill,
                stop=sig.stop,
                tp1=sig.tp1,
                tp2=sig.tp2,
                tp3=sig.tp3,
                leverage=sig.leverage,
                confidence=sig.confidence,
                reason_short="; ".join(sig.reasons[:3]),
                contracts=qty,
                contracts_remaining=qty,
                margin_usd=margin,
                notional_usd=notional,
                entry_order_id=str(order.get("id") or ""),
                sl_order_id=sl_id,
                tp1_order_id="",
                tp2_order_id=tp_id,
                tp1_done=False,
                auto_trade=True,
            )
            return {
                "ok": True,
                "trade": trade,
                "fill": fill,
                "qty": qty,
                "margin": margin,
                "notional": notional,
                "warnings": warnings,
            }
        except Exception as e:
            log.error("place_signal failed: %s\n%s", e, traceback.format_exc())
            return {"ok": False, "reason": str(e)}

    def sync_open_trade(self, trade: OpenSignal) -> Optional[str]:
        """
        Returns:
          exchange_flat | stop_hit | tp2_hit | tp1_partial | None
        Never returns flat on API error.
        """
        if not self.keys_ok or self.ex is None or not trade.auto_trade:
            return None

        try:
            still, rem = self._position_remaining(trade.symbol)
        except Exception as e:
            log.warning("sync positions FAILED (NOT treating as closed): %s", e)
            return None

        if not still or rem <= 0:
            # Position gone — try cancel leftover triggers
            self._cancel_symbol_orders(trade.symbol)
            return "exchange_flat"

        trade.contracts_remaining = rem

        try:
            t = self.ex.fetch_ticker(trade.symbol)
            px = float(t.get("last") or t.get("close") or 0)
        except Exception as e:
            log.debug("sync ticker: %s", e)
            return None
        if px <= 0:
            return None

        buf = abs(trade.entry) * 0.0003  # tiny noise buffer

        # --- Software SL backup (if exchange trigger failed/missed) ---
        if trade.direction == "LONG" and trade.stop > 0 and px <= trade.stop - buf:
            if self.close_all_remaining(trade):
                self._cancel_symbol_orders(trade.symbol)
                return "stop_hit"
            return None

        # --- Optional software TP1 partial (~40%), then re-arm full SL/TP on remainder ---
        if (
            not trade.tp1_done
            and rem > 1
            and trade.tp1 > 0
            and trade.direction == "LONG"
            and px >= trade.tp1 + buf
        ):
            frac = max(0.2, min(0.6, config.TP1_SIZE_PCT / 100.0))
            q = max(1.0, math.floor(trade.contracts * frac))
            q = min(q, rem - 1)
            if self.market_close(trade, q):
                time.sleep(0.8)
                still2, rem2 = self._position_remaining(trade.symbol)
                if not still2 or rem2 <= 0:
                    self._cancel_symbol_orders(trade.symbol)
                    return "tp2_hit"  # fully gone after partial attempt
                trade.tp1_done = True
                trade.contracts_remaining = rem2
                trade.contracts = rem2
                trade.stop = trade.entry  # breakeven for runner
                # Kill old full-size triggers (would fail on reduced size) and re-arm
                self._cancel_symbol_orders(trade.symbol)
                time.sleep(0.4)
                sl = self._place_trigger(
                    trade.symbol,
                    trade.direction,
                    rem2,
                    trade.stop,
                    is_stop=True,
                    leverage=trade.leverage,
                )
                tp = self._place_trigger(
                    trade.symbol,
                    trade.direction,
                    rem2,
                    trade.tp2,
                    is_stop=False,
                    leverage=trade.leverage,
                )
                trade.sl_order_id = sl
                trade.tp2_order_id = tp
                log.info("TP1 done; re-armed SL/TP for rem=%s sl=%s tp=%s", rem2, sl, tp)
                return "tp1_partial"
            return None

        # --- Software TP2 backup ---
        if trade.direction == "LONG" and trade.tp2 > 0 and px >= trade.tp2 + buf:
            if self.close_all_remaining(trade):
                self._cancel_symbol_orders(trade.symbol)
                return "tp2_hit"
            return None

        return None

    def close_all_remaining(self, trade: OpenSignal) -> bool:
        """Close 100% of LIVE exchange size for this symbol (retry)."""
        if not self.ex:
            return False
        try:
            still, rem = self._position_remaining(trade.symbol)
        except Exception as e:
            log.error("close_all: cannot fetch position: %s", e)
            rem = trade.contracts_remaining or trade.contracts
            still = rem > 0
        if not still or rem <= 0:
            return True
        ok = self.market_close(trade, rem)
        if ok:
            time.sleep(0.6)
            try:
                still2, rem2 = self._position_remaining(trade.symbol)
                if still2 and rem2 > 0:
                    log.warning("Dust remaining %s qty=%s — second close", trade.symbol, rem2)
                    ok2 = self.market_close(trade, rem2)
                    return ok2
            except Exception as e:
                log.warning("post-close check: %s", e)
        return ok

    def market_close(self, trade: OpenSignal, qty: Optional[float] = None) -> bool:
        if not self.ex:
            return False
        q = qty if qty is not None else trade.contracts_remaining
        if q is None or q <= 0:
            q = trade.contracts or 0
        if q <= 0:
            return True
        side = "sell" if trade.direction == "LONG" else "buy"
        params = {
            "reduceOnly": True,
            "openType": 1 if config.MARGIN_MODE == "isolated" else 2,
            "hedged": False,
            "leverage": trade.leverage,
        }
        for attempt in range(1, 4):
            try:
                q = float(self.ex.amount_to_precision(trade.symbol, q))
                if q <= 0:
                    return True
                self.ex.create_order(trade.symbol, "market", side, q, None, params)
                log.info("Closed %s qty=%s (attempt %d)", trade.symbol, q, attempt)
                return True
            except Exception as e:
                log.error("market_close attempt %d: %s", attempt, e)
                time.sleep(0.5 * attempt)
                # refresh live size
                try:
                    still, rem = self._position_remaining(trade.symbol)
                    if not still or rem <= 0:
                        return True
                    q = rem
                except Exception:
                    pass
        return False

    def fetch_balance_usdt(self) -> Optional[float]:
        if not self.ex:
            return None
        try:
            bal = self.ex.fetch_balance({"type": "swap"})
        except Exception:
            try:
                bal = self.ex.fetch_balance()
            except Exception as e:
                log.warning("balance: %s", e)
                return None
        try:
            usdt = bal.get("USDT") or {}
            free = usdt.get("free")
            if free is not None:
                return float(free)
            total = usdt.get("total")
            if total is not None:
                return float(total)
        except Exception:
            pass
        return None

    def has_open_position_on(self, symbol: str) -> bool:
        try:
            still, rem = self._position_remaining(symbol)
            return still and rem > 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _has_any_bot_conflict(self, symbol: str) -> bool:
        if self.has_open_position_on(symbol):
            log.warning("Already in position on %s — skip open", symbol)
            return True
        return False

    def _set_leverage(self, symbol: str, direction: str, leverage: int) -> None:
        assert self.ex is not None
        try:
            self.ex.set_leverage(
                leverage,
                symbol,
                {
                    "openType": 1 if config.MARGIN_MODE == "isolated" else 2,
                    "positionType": 1 if direction == "LONG" else 2,
                },
            )
        except Exception as e:
            log.warning("set_leverage: %s", e)
        try:
            self.ex.set_margin_mode(config.MARGIN_MODE, symbol)
        except Exception:
            pass

    def _size_contracts(self, symbol: str, price: float, leverage: int) -> float:
        assert self.ex is not None
        m = self.ex.market(symbol)
        csize = float(m.get("contractSize") or 1)
        if price <= 0 or csize <= 0:
            raise ValueError("bad price/contractSize")
        raw = (config.POSITION_SIZE_USD * leverage) / (csize * price)
        raw = math.floor(raw)
        mn = float(m.get("limits", {}).get("amount", {}).get("min") or 1)
        qty = max(mn, float(raw))
        qty = float(self.ex.amount_to_precision(symbol, qty))
        if qty < mn:
            raise ValueError(
                f"size too small for ${config.POSITION_SIZE_USD} @ {leverage}x"
            )
        eff_margin = (qty * csize * price) / max(leverage, 1)
        if eff_margin > config.POSITION_SIZE_USD * 2.5:
            raise ValueError(f"effective margin ${eff_margin:.1f} too large vs target")
        log.info(
            "Size %s: contracts=%s eff_margin≈$%.2f notional≈$%.2f",
            symbol,
            qty,
            eff_margin,
            qty * csize * price,
        )
        return qty

    def _place_trigger(
        self,
        symbol: str,
        direction: str,
        qty: float,
        trigger: float,
        is_stop: bool,
        leverage: int,
    ) -> str:
        assert self.ex is not None
        side = "sell" if direction == "LONG" else "buy"
        ttype = (2 if is_stop else 1) if direction == "LONG" else (1 if is_stop else 2)
        try:
            trigger_px = float(self.ex.price_to_precision(symbol, trigger))
        except Exception:
            trigger_px = float(trigger)
        params: dict[str, Any] = {
            "reduceOnly": True,
            "triggerPrice": trigger_px,
            "triggerType": ttype,
            "executeCycle": 1,  # GTC until cancel / fill
            "trend": 1,  # last price
            "orderType": 5,  # market on trigger
            "openType": 1 if config.MARGIN_MODE == "isolated" else 2,
            "hedged": False,
            "leverage": leverage,
        }
        try:
            q = float(self.ex.amount_to_precision(symbol, qty))
            o = self.ex.create_order(symbol, "market", side, q, None, params)
            oid = str(o.get("id") or "")
            log.info(
                "Trigger %s %s qty=%s @ %s id=%s",
                "SL" if is_stop else "TP",
                symbol,
                q,
                _fmt_px(trigger_px),
                oid or "(empty)",
            )
            return oid
        except Exception as e:
            log.error("trigger failed (%s @ %s): %s", symbol, trigger_px, e)
            return ""

    def _cancel_symbol_orders(self, symbol: str) -> None:
        """Best-effort cancel open + plan/stop orders so size mismatches clear."""
        if not self.ex:
            return
        try:
            self.ex.cancel_all_orders(symbol)
            log.info("cancel_all_orders %s", symbol)
        except Exception as e:
            log.debug("cancel_all_orders: %s", e)
        # MEXC sometimes needs planorder cancel_all via implicit API
        try:
            m = self.ex.market(symbol)
            raw = m.get("id") or symbol.replace("/USDT:USDT", "_USDT")
            if hasattr(self.ex, "contractPrivatePostPlanorderCancelAll"):
                self.ex.contractPrivatePostPlanorderCancelAll({"symbol": raw})
            if hasattr(self.ex, "contractPrivatePostStoporderCancelAll"):
                self.ex.contractPrivatePostStoporderCancelAll({"symbol": raw})
        except Exception as e:
            log.debug("plan/stop cancel_all: %s", e)

    def _position_remaining(self, symbol: str) -> tuple[bool, float]:
        """
        Raises on total API failure so callers do not treat as flat.
        Returns (in_position, contracts).
        """
        assert self.ex is not None
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                try:
                    positions = self.ex.fetch_positions([symbol])
                except TypeError:
                    positions = self.ex.fetch_positions()
                break
            except Exception as e:
                last_err = e
                time.sleep(0.4 * attempt)
        else:
            raise RuntimeError(f"fetch_positions failed: {last_err}")

        pair_mexc = symbol.replace("/USDT:USDT", "_USDT").replace("/", "_")
        base = pair_mexc.split("_")[0] if "_" in pair_mexc else symbol.split("/")[0]

        for p in positions or []:
            info = p.get("info") or {}
            sym = str(p.get("symbol") or "")
            info_sym = str(info.get("symbol") or "")
            if not (
                symbol in sym
                or pair_mexc == info_sym
                or pair_mexc in info_sym
                or (base and (info_sym.startswith(base + "_") or base + "/" in sym))
            ):
                continue
            c = abs(float(p.get("contracts") or 0))
            if c == 0:
                try:
                    c = abs(float(info.get("holdVol") or 0))
                except (TypeError, ValueError):
                    c = 0
            if c > 0:
                return True, c
        return False, 0.0


def build_executor() -> MexcExecutor:
    return MexcExecutor()


def format_trade_opened(sig: Signal, trade: OpenSignal, warnings: Optional[list] = None) -> str:
    pair = sig.symbol.replace(":USDT", "").replace("/", "_")
    lines = [
        "🟢 LIVE TRADE OPENED | Oversold Bounce",
        f"Pair: {sig.symbol} ({pair})",
        f"Direction: LONG",
        f"Fill entry: {_fmt_px(trade.entry)}",
        f"Stop-Loss: {_fmt_px(trade.stop)}",
        f"TP1 (soft partial): {_fmt_px(trade.tp1)} (~{config.TP1_SIZE_PCT}%)",
        f"TP2 (exchange + soft): {_fmt_px(trade.tp2)}",
        f"Leverage: {trade.leverage}x isolated",
        f"Margin≈ ${trade.margin_usd:.2f} (target ${config.POSITION_SIZE_USD:.0f})",
        f"Contracts: {trade.contracts:g} | Notional≈ ${trade.notional_usd:.2f}",
        f"Confidence: {sig.confidence:.0f}/100",
        f"Chart: https://futures.mexc.com/exchange/{pair}",
        "Exchange arms: full SL + full TP2. Software backs up every poll.",
    ]
    if warnings:
        lines.append("⚠ " + ", ".join(warnings) + " — set SL/TP on MEXC manually if needed")
    return "\n".join(lines)
