"""
Phase 2 — MEXC USDT-M auto execution.

Rules:
  - FIXED margin = POSITION_SIZE_USD ($50) per trade (not % of balance)
  - Isolated margin
  - Variable leverage from signal
  - Max 1 trade managed by this bot (state lock)
  - Market entry LONG + trigger SL + partial TP1 + TP2 runner

MEXC trigger params mirror the working squeeze bot pattern.
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
            time.sleep(1.0)

            m = self.ex.market(sig.symbol)
            csize = float(m.get("contractSize") or 1)
            notional = qty * csize * fill
            margin = notional / max(sig.leverage, 1)

            # Partials: ~40% TP1, rest TP2 (TP3 merged into runner at TP2 for reliability)
            tp1_frac = max(0.2, min(0.6, config.TP1_SIZE_PCT / 100.0))
            if qty <= 1:
                tp1_qty, tp2_qty = qty, 0.0
            else:
                tp1_qty = max(1.0, math.floor(qty * tp1_frac))
                tp1_qty = min(tp1_qty, qty - 1)
                tp2_qty = qty - tp1_qty

            sl_id = self._place_trigger(
                sig.symbol, sig.direction, qty, sig.stop, is_stop=True, leverage=sig.leverage
            )
            tp1_id = self._place_trigger(
                sig.symbol, sig.direction, tp1_qty, sig.tp1, is_stop=False, leverage=sig.leverage
            )
            tp2_id = ""
            if tp2_qty >= 1:
                tp2_id = self._place_trigger(
                    sig.symbol,
                    sig.direction,
                    tp2_qty,
                    sig.tp2,
                    is_stop=False,
                    leverage=sig.leverage,
                )

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
                tp1_order_id=tp1_id,
                tp2_order_id=tp2_id,
                tp1_done=(tp2_qty < 1),
                auto_trade=True,
            )
            return {
                "ok": True,
                "trade": trade,
                "fill": fill,
                "qty": qty,
                "margin": margin,
                "notional": notional,
            }
        except Exception as e:
            log.error("place_signal failed: %s\n%s", e, traceback.format_exc())
            return {"ok": False, "reason": str(e)}

    def sync_open_trade(self, trade: OpenSignal) -> Optional[str]:
        """
        Poll exchange + price. Returns close reason if trade fully closed, else None.
        Mutates trade (remaining qty, tp1_done, stop → breakeven after TP1).
        """
        if not self.keys_ok or self.ex is None or not trade.auto_trade:
            return None

        try:
            still, rem = self._position_remaining(trade.symbol)
        except Exception as e:
            log.warning("sync positions: %s", e)
            return None

        if not still or rem <= 0:
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

        # Hard SL (backup if trigger missed)
        if trade.direction == "LONG" and px <= trade.stop:
            if self.market_close(trade):
                return "stop_hit"
            return None

        # TP1 partial
        if (
            not trade.tp1_done
            and trade.contracts_remaining > 1
            and trade.direction == "LONG"
            and px >= trade.tp1
        ):
            frac = max(0.2, min(0.6, config.TP1_SIZE_PCT / 100.0))
            q = max(1.0, math.floor(trade.contracts * frac))
            q = min(q, trade.contracts_remaining - 1)
            if self.market_close(trade, q):
                trade.tp1_done = True
                trade.contracts_remaining = max(0.0, trade.contracts_remaining - q)
                trade.stop = trade.entry  # breakeven runner
                return "tp1_partial"
            return None

        # TP2 full remainder
        if trade.direction == "LONG" and px >= trade.tp2:
            if self.market_close(trade):
                return "tp2_hit"
            return None

        return None

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
        try:
            q = float(self.ex.amount_to_precision(trade.symbol, q))
            self.ex.create_order(trade.symbol, "market", side, q, None, params)
            log.info("Closed %s qty=%s", trade.symbol, q)
            return True
        except Exception as e:
            log.error("market_close: %s", e)
            return False

    def fetch_balance_usdt(self) -> Optional[float]:
        if not self.ex:
            return None
        try:
            bal = self.ex.fetch_balance()
            # Prefer swap/futures free USDT
            usdt = bal.get("USDT") or {}
            free = usdt.get("free")
            if free is not None:
                return float(free)
            total = usdt.get("total")
            if total is not None:
                return float(total)
            return None
        except Exception as e:
            log.warning("balance: %s", e)
            return None

    def has_open_position_on(self, symbol: str) -> bool:
        still, rem = self._position_remaining(symbol)
        return still and rem > 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _has_any_bot_conflict(self, symbol: str) -> bool:
        """Block if this symbol already has a position."""
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
        """Contracts such that margin ≈ POSITION_SIZE_USD at given leverage."""
        assert self.ex is not None
        m = self.ex.market(symbol)
        csize = float(m.get("contractSize") or 1)
        if price <= 0 or csize <= 0:
            raise ValueError("bad price/contractSize")
        # notional = margin * lev ; contracts = notional / (csize * price)
        raw = (config.POSITION_SIZE_USD * leverage) / (csize * price)
        raw = math.floor(raw)  # whole contracts on MEXC
        mn = float(m.get("limits", {}).get("amount", {}).get("min") or 1)
        qty = max(mn, float(raw))
        qty = float(self.ex.amount_to_precision(symbol, qty))
        if qty < mn:
            raise ValueError(
                f"size too small for ${config.POSITION_SIZE_USD} @ {leverage}x "
                f"(need higher lev or cheaper pair)"
            )
        # Sanity: effective margin shouldn't be wildly over 2x target
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
        # LONG: stop = trigger below (2), take-profit = trigger above (1)
        ttype = (2 if is_stop else 1) if direction == "LONG" else (1 if is_stop else 2)
        params: dict[str, Any] = {
            "reduceOnly": True,
            "triggerPrice": trigger,
            "triggerType": ttype,
            "executeCycle": 2,
            "trend": 1,
            "orderType": 5,
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
                _fmt_px(trigger),
                oid,
            )
            return oid
        except Exception as e:
            log.error("trigger failed (%s @ %s): %s", symbol, trigger, e)
            return ""

    def _position_remaining(self, symbol: str) -> tuple[bool, float]:
        assert self.ex is not None
        try:
            positions = self.ex.fetch_positions([symbol])
        except TypeError:
            positions = self.ex.fetch_positions()
        except Exception:
            positions = self.ex.fetch_positions()

        for p in positions or []:
            info = p.get("info") or {}
            sym = p.get("symbol") or ""
            # Match unified or MEXC raw
            pair_mexc = symbol.replace("/USDT:USDT", "_USDT").replace("/", "_")
            if symbol not in sym and info.get("symbol") not in (pair_mexc, symbol):
                # also accept if market id matches
                if pair_mexc not in str(info.get("symbol") or "") and symbol != sym:
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


def format_trade_opened(sig: Signal, trade: OpenSignal) -> str:
    pair = sig.symbol.replace(":USDT", "").replace("/", "_")
    return "\n".join(
        [
            "🟢 LIVE TRADE OPENED | Oversold Bounce",
            f"Pair: {sig.symbol} ({pair})",
            f"Direction: LONG",
            f"Fill entry: {_fmt_px(trade.entry)}",
            f"Stop-Loss: {_fmt_px(trade.stop)}",
            f"TP1: {_fmt_px(trade.tp1)} (~{config.TP1_SIZE_PCT}%)",
            f"TP2: {_fmt_px(trade.tp2)} (runner)",
            f"Leverage: {trade.leverage}x isolated",
            f"Margin≈ ${trade.margin_usd:.2f} (target ${config.POSITION_SIZE_USD:.0f})",
            f"Contracts: {trade.contracts:g} | Notional≈ ${trade.notional_usd:.2f}",
            f"Confidence: {sig.confidence:.0f}/100",
            f"Chart: https://futures.mexc.com/exchange/{pair}",
            "1 trade max — bot will not open another until this is flat.",
        ]
    )
