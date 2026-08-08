"""
Bybit USDT linear auto execution.

- FIXED margin = POSITION_SIZE_USD ($50)
- Isolated + variable leverage
- Max 1 trade (state lock + rehydrate)
- Entry market LONG with attached SL + TP2 (Bybit trading-stop / params)
- Software TP1 partial + re-arm; software SL/TP2 backup
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
    """Name kept for import compatibility; implements Bybit."""

    def __init__(self) -> None:
        self.keys_ok = bool(config.BYBIT_API_KEY and config.BYBIT_API_SECRET)
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
                "AUTO_TRADE ON — live BYBIT orders | margin=$%.0f isolated",
                config.POSITION_SIZE_USD,
            )
        elif self.keys_ok:
            log.info("Bybit keys present but AUTO_TRADE=false (signal-only)")
        else:
            log.info("Executor idle (no keys / signal-only)")

    # ------------------------------------------------------------------
    def place_signal(self, sig: Signal) -> dict[str, Any]:
        if not self.enabled or self.ex is None:
            return {"ok": False, "reason": "auto_trade_disabled"}
        if sig.direction != "LONG":
            return {"ok": False, "reason": "long_only"}

        try:
            if self._has_any_bot_conflict(sig.symbol):
                return {"ok": False, "reason": "position_already_open"}

            self._set_leverage(sig.symbol, sig.leverage)
            qty = self._size_contracts(sig.symbol, sig.entry, sig.leverage)

            try:
                sl_px = float(self.ex.price_to_precision(sig.symbol, sig.stop))
                tp_px = float(self.ex.price_to_precision(sig.symbol, sig.tp2))
            except Exception:
                sl_px, tp_px = float(sig.stop), float(sig.tp2)

            params: dict[str, Any] = {
                "category": "linear",
                "reduceOnly": False,
                # Attached SL/TP (Bybit v5 via ccxt)
                "stopLoss": {"triggerPrice": sl_px},
                "takeProfit": {"triggerPrice": tp_px},
            }

            log.info(
                "OPEN LONG %s qty=%s lev=%dx margin=$%.0f SL=%s TP2=%s",
                sig.symbol,
                qty,
                sig.leverage,
                config.POSITION_SIZE_USD,
                sl_px,
                tp_px,
            )
            order = self.ex.create_order(sig.symbol, "market", "buy", qty, None, params)
            fill = float(order.get("average") or order.get("price") or sig.entry)
            time.sleep(1.0)

            still, live_qty = self._position_remaining(sig.symbol)
            if still and live_qty > 0:
                qty = live_qty

            # Explicit trading-stop backup (full position SL + TP2)
            sl_id, tp_id = self._set_trading_stop(sig.symbol, sl_px, tp_px)
            warnings = []
            if not sl_id:
                warnings.append("SL_SET_UNCONFIRMED")
            if not tp_id:
                warnings.append("TP_SET_UNCONFIRMED")

            m = self.ex.market(sig.symbol)
            csize = float(m.get("contractSize") or 1)
            notional = qty * csize * fill
            margin = notional / max(sig.leverage, 1)

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
            # Region / product blocks
            return {"ok": False, "reason": str(e)}

    def sync_open_trade(self, trade: OpenSignal) -> Optional[str]:
        if not self.keys_ok or self.ex is None or not trade.auto_trade:
            return None

        try:
            still, rem = self._position_remaining(trade.symbol)
        except Exception as e:
            log.warning("sync positions FAILED (NOT treating as closed): %s", e)
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

        buf = abs(trade.entry) * 0.0003

        if trade.direction == "LONG" and trade.stop > 0 and px <= trade.stop - buf:
            if self.close_all_remaining(trade):
                return "stop_hit"
            return None

        if (
            not trade.tp1_done
            and rem > 0
            and trade.tp1 > 0
            and trade.direction == "LONG"
            and px >= trade.tp1 + buf
        ):
            # Partial ~40%; if amount too small for partial, skip to runner only
            frac = max(0.2, min(0.6, config.TP1_SIZE_PCT / 100.0))
            q = rem * frac
            try:
                q = float(self.ex.amount_to_precision(trade.symbol, q))
            except Exception:
                q = round(q, 4)
            mn = float(
                (self.ex.market(trade.symbol).get("limits") or {})
                .get("amount", {})
                .get("min")
                or 0
            )
            # Need remainder after partial to still be >= min
            if q >= mn and (rem - q) >= mn:
                if self.market_close(trade, q):
                    time.sleep(0.6)
                    still2, rem2 = self._position_remaining(trade.symbol)
                    if not still2 or rem2 <= 0:
                        return "tp2_hit"
                    trade.tp1_done = True
                    trade.contracts_remaining = rem2
                    trade.contracts = rem2
                    if trade.stop <= 0 or trade.stop >= trade.entry:
                        trade.stop = trade.entry
                    try:
                        sl_id, tp_id = self._set_trading_stop(
                            trade.symbol, trade.stop, trade.tp2
                        )
                        trade.sl_order_id = sl_id
                        trade.tp2_order_id = tp_id
                        log.info(
                            "TP1 done; re-armed SL/TP rem=%s sl=%s tp=%s",
                            rem2,
                            sl_id or "FAIL",
                            tp_id or "FAIL",
                        )
                    except Exception as e:
                        log.error("TP1 re-arm failed: %s", e)
                        trade.sl_order_id = ""
                        trade.tp2_order_id = ""
                    return "tp1_partial"
            else:
                # Can't partial cleanly — wait for full TP2
                trade.tp1_done = True
                log.info("TP1 skipped (size too small for partial) — hold for TP2")

        if trade.direction == "LONG" and trade.tp2 > 0 and px >= trade.tp2 + buf:
            if self.close_all_remaining(trade):
                return "tp2_hit"
            return None

        return None

    def close_all_remaining(self, trade: OpenSignal) -> bool:
        if not self.ex:
            return False
        try:
            still, rem = self._position_remaining(trade.symbol)
        except Exception as e:
            log.error("close_all: %s", e)
            rem = trade.contracts_remaining or trade.contracts
            still = rem > 0
        if not still or rem <= 0:
            return True
        ok = self.market_close(trade, rem)
        if ok:
            time.sleep(0.5)
            try:
                still2, rem2 = self._position_remaining(trade.symbol)
                if still2 and rem2 > 0:
                    return self.market_close(trade, rem2)
            except Exception:
                pass
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
        params = {"category": "linear", "reduceOnly": True}
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
                time.sleep(0.4 * attempt)
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
            bal = self.ex.fetch_balance({"type": "unified"})
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
    def _has_any_bot_conflict(self, symbol: str) -> bool:
        if self.has_open_position_on(symbol):
            log.warning("Already in position on %s — skip open", symbol)
            return True
        return False

    def _set_leverage(self, symbol: str, leverage: int) -> None:
        assert self.ex is not None
        try:
            self.ex.set_margin_mode(config.MARGIN_MODE, symbol)
        except Exception as e:
            log.debug("set_margin_mode: %s", e)
        try:
            self.ex.set_leverage(leverage, symbol)
        except Exception as e:
            # Bybit may error if leverage already set
            log.warning("set_leverage: %s", e)

    def _size_contracts(self, symbol: str, price: float, leverage: int) -> float:
        """Bybit linear: amount in base coin; notional ≈ amount * price."""
        assert self.ex is not None
        m = self.ex.market(symbol)
        csize = float(m.get("contractSize") or 1)
        if price <= 0:
            raise ValueError("bad price")
        notional = config.POSITION_SIZE_USD * leverage
        raw = notional / (csize * price)
        mn = float(m.get("limits", {}).get("amount", {}).get("min") or 0.001)
        qty = max(mn, float(raw))
        qty = float(self.ex.amount_to_precision(symbol, qty))
        if qty < mn:
            raise ValueError(
                f"size too small for ${config.POSITION_SIZE_USD} @ {leverage}x on {symbol}"
            )
        eff_margin = (qty * csize * price) / max(leverage, 1)
        if eff_margin > config.POSITION_SIZE_USD * 2.5:
            raise ValueError(f"effective margin ${eff_margin:.1f} too large vs target")
        log.info(
            "Size %s: qty=%s eff_margin≈$%.2f notional≈$%.2f",
            symbol,
            qty,
            eff_margin,
            qty * csize * price,
        )
        return qty

    def _set_trading_stop(
        self, symbol: str, stop_loss: float, take_profit: float
    ) -> tuple[str, str]:
        """
        Set position TP/SL via Bybit trading-stop.
        Returns (sl_ok_marker, tp_ok_marker) non-empty on success.
        """
        assert self.ex is not None
        sl_ok, tp_ok = "", ""
        try:
            sl_px = float(self.ex.price_to_precision(symbol, stop_loss))
            tp_px = float(self.ex.price_to_precision(symbol, take_profit))
        except Exception:
            sl_px, tp_px = float(stop_loss), float(take_profit)

        # Method 1: create_order with stopLossPrice / takeProfitPrice on trading-stop path
        try:
            # Full position trading stop via unified helper if present
            if hasattr(self.ex, "private_post_v5_position_trading_stop"):
                m = self.ex.market(symbol)
                body = {
                    "category": "linear",
                    "symbol": m["id"],
                    "tpslMode": "Full",
                    "positionIdx": 0,  # one-way
                    "stopLoss": str(sl_px),
                    "takeProfit": str(tp_px),
                    "slTriggerBy": "LastPrice",
                    "tpTriggerBy": "LastPrice",
                }
                resp = self.ex.private_post_v5_position_trading_stop(body)
                ret = str(resp.get("retCode") or resp.get("ret_code") or "")
                if ret in ("0", "0.0", ""):
                    log.info("trading-stop OK %s SL=%s TP=%s", symbol, sl_px, tp_px)
                    return "sl_ok", "tp_ok"
                log.warning("trading-stop ret: %s", resp)
            else:
                # ccxt path
                self.ex.create_order(
                    symbol,
                    "market",
                    "sell",
                    0,
                    None,
                    {
                        "stopLossPrice": sl_px,
                        "takeProfitPrice": tp_px,
                        "tradingStopEndpoint": True,
                        "category": "linear",
                    },
                )
                return "sl_ok", "tp_ok"
        except Exception as e:
            log.warning("trading-stop batch failed: %s — try separate", e)

        # Method 2: separate stop-loss and take-profit reduce orders
        try:
            o1 = self.ex.create_order(
                symbol,
                "market",
                "sell",
                None,
                None,
                {
                    "stopLossPrice": sl_px,
                    "reduceOnly": True,
                    "category": "linear",
                    "triggerDirection": "descending",
                },
            )
            sl_ok = str(o1.get("id") or "sl_ok")
        except Exception as e:
            log.error("SL order failed: %s", e)

        try:
            o2 = self.ex.create_order(
                symbol,
                "market",
                "sell",
                None,
                None,
                {
                    "takeProfitPrice": tp_px,
                    "reduceOnly": True,
                    "category": "linear",
                    "triggerDirection": "ascending",
                },
            )
            tp_ok = str(o2.get("id") or "tp_ok")
        except Exception as e:
            log.error("TP order failed: %s", e)

        return sl_ok, tp_ok

    def _position_remaining(self, symbol: str) -> tuple[bool, float]:
        assert self.ex is not None
        last_err: Optional[Exception] = None
        positions = None
        for attempt in range(1, 4):
            try:
                try:
                    positions = self.ex.fetch_positions([symbol], {"category": "linear"})
                except TypeError:
                    positions = self.ex.fetch_positions([symbol])
                except Exception:
                    positions = self.ex.fetch_positions({"category": "linear"})
                break
            except Exception as e:
                last_err = e
                time.sleep(0.3 * attempt)
        if positions is None:
            raise RuntimeError(f"fetch_positions failed: {last_err}")

        base = symbol.split("/")[0] if "/" in symbol else symbol
        for p in positions or []:
            info = p.get("info") or {}
            sym = str(p.get("symbol") or "")
            info_sym = str(info.get("symbol") or "")
            if not (
                symbol in sym
                or base in sym
                or info_sym in (base + "USDT", symbol, base)
                or sym.endswith(base)
            ):
                # strict: must match this market
                try:
                    if self.ex.market(symbol)["symbol"] not in (sym, symbol):
                        if info_sym != self.ex.market(symbol).get("id"):
                            continue
                except Exception:
                    continue
            c = abs(float(p.get("contracts") or 0))
            if c == 0:
                try:
                    c = abs(float(info.get("size") or info.get("qty") or 0))
                except (TypeError, ValueError):
                    c = 0
            if c > 0:
                # only count long for this bot
                side = (p.get("side") or info.get("side") or "").lower()
                if side in ("short", "sell"):
                    continue
                return True, c
        return False, 0.0


def build_executor() -> MexcExecutor:
    return MexcExecutor()


def rehydrate_from_exchange(executor: MexcExecutor) -> Optional[OpenSignal]:
    if not executor.keys_ok or not executor.ex:
        return None
    try:
        try:
            positions = executor.ex.fetch_positions(params={"category": "linear"})
        except TypeError:
            positions = executor.ex.fetch_positions()
    except Exception as e:
        log.warning("rehydrate: %s", e)
        return None

    for p in positions or []:
        info = p.get("info") or {}
        try:
            c = abs(float(p.get("contracts") or 0))
            if c == 0:
                c = abs(float(info.get("size") or info.get("qty") or 0))
        except (TypeError, ValueError):
            c = 0
        if c <= 0:
            continue

        side = (p.get("side") or info.get("side") or "").lower()
        if side in ("short", "sell"):
            continue

        sym = str(p.get("symbol") or "")
        info_sym = str(info.get("symbol") or "")
        if "/USDT" in sym:
            symbol = sym if ":USDT" in sym else f"{sym}:USDT" if not sym.endswith(":USDT") else sym
            if symbol.endswith("/USDT") and ":USDT" not in symbol:
                symbol = symbol + ":USDT"
        elif info_sym.endswith("USDT") and "/" not in info_sym:
            base = info_sym.replace("USDT", "")
            symbol = f"{base}/USDT:USDT"
        else:
            continue

        entry = float(
            p.get("entryPrice") or info.get("avgPrice") or info.get("entryPrice") or 0
        )
        lev = int(float(p.get("leverage") or info.get("leverage") or config.LEVERAGE_DEFAULT))
        stop = entry * 0.988 if entry > 0 else 0.0
        tp2 = entry * 1.025 if entry > 0 else 0.0
        tp1 = entry * 1.012 if entry > 0 else 0.0

        trade = OpenSignal(
            symbol=symbol,
            direction="LONG",
            entry=entry,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            tp3=tp2,
            leverage=lev,
            confidence=0,
            reason_short="rehydrated_from_bybit",
            contracts=c,
            contracts_remaining=c,
            margin_usd=config.POSITION_SIZE_USD,
            auto_trade=True,
        )
        log.warning("Rehydrated Bybit LONG %s qty=%s entry=%s", symbol, c, entry)
        return trade
    return None


def format_trade_opened(sig: Signal, trade: OpenSignal, warnings: Optional[list] = None) -> str:
    pair = sig.symbol.replace(":USDT", "").replace("/", "")
    lines = [
        "🟢 LIVE TRADE OPENED | Oversold Bounce | BYBIT",
        f"Pair: {sig.symbol}",
        f"Direction: LONG",
        f"Fill entry: {_fmt_px(trade.entry)}",
        f"Stop-Loss: {_fmt_px(trade.stop)}",
        f"TP1 (soft partial): {_fmt_px(trade.tp1)} (~{config.TP1_SIZE_PCT}%)",
        f"TP2: {_fmt_px(trade.tp2)}",
        f"Leverage: {trade.leverage}x isolated",
        f"Margin≈ ${trade.margin_usd:.2f} (target ${config.POSITION_SIZE_USD:.0f})",
        f"Size: {trade.contracts:g} | Notional≈ ${trade.notional_usd:.2f}",
        f"Confidence: {sig.confidence:.0f}/100",
        f"Chart: https://www.bybit.com/trade/usdt/{pair}",
        "Bybit SL/TP attached + software backup every poll.",
    ]
    if warnings:
        lines.append("⚠ " + ", ".join(warnings) + " — verify SL/TP on Bybit UI")
    return "\n".join(lines)
