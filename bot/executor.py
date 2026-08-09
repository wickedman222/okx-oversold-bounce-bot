"""
OKX USDT swap auto execution.

- FIXED $50 margin / trade, isolated, variable leverage
- Max 1 trade + rehydrate after Railway restart
- Market LONG + attached stopLoss/takeProfit
- Software TP1 partial + TP2/SL backup

OKX needs: API key, secret, AND passphrase (password).
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
    """Import-compatible name; implements OKX trading."""

    def __init__(self) -> None:
        self.keys_ok = bool(
            config.OKX_API_KEY and config.OKX_API_SECRET and config.OKX_API_PASSWORD
        )
        self.enabled = bool(config.AUTO_TRADE and self.keys_ok)
        self.ex: Optional[ccxt.Exchange] = None
        if config.OKX_API_KEY and config.OKX_API_SECRET:
            if not config.OKX_API_PASSWORD:
                log.error(
                    "OKX_API_PASSWORD (passphrase) missing — required by OKX. "
                    "Set it on Railway Variables."
                )
            else:
                self.ex = make_exchange(private=True)
                try:
                    self.ex.load_markets()
                except Exception as e:
                    log.warning("Private markets: %s", e)
        if self.enabled:
            log.warning(
                "AUTO_TRADE ON — live OKX orders | margin=$%.0f isolated",
                config.POSITION_SIZE_USD,
            )
        elif self.keys_ok:
            log.info("OKX keys present but AUTO_TRADE=false")
        else:
            log.info("Executor idle (no full OKX credentials / signal-only)")

    def place_signal(self, sig: Signal) -> dict[str, Any]:
        if not self.enabled or self.ex is None:
            return {"ok": False, "reason": "auto_trade_disabled"}
        if sig.direction != "LONG":
            return {"ok": False, "reason": "long_only"}
        try:
            if self.has_open_position_on(sig.symbol):
                return {"ok": False, "reason": "position_already_open"}

            self._set_leverage(sig.symbol, sig.leverage)
            qty = self._size_contracts(sig.symbol, sig.entry, sig.leverage)
            try:
                sl_px = float(self.ex.price_to_precision(sig.symbol, sig.stop))
                tp_px = float(self.ex.price_to_precision(sig.symbol, sig.tp2))
            except Exception:
                sl_px, tp_px = float(sig.stop), float(sig.tp2)

            params: dict[str, Any] = {
                "tdMode": "isolated",
                "marginMode": "isolated",
                # one-way mode: omit posSide or net
                "stopLoss": {
                    "triggerPrice": sl_px,
                    "type": "market",
                },
                "takeProfit": {
                    "triggerPrice": tp_px,
                    "type": "market",
                },
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

            # Extra algo SL/TP if attach failed
            sl_id, tp_id = self._place_protective(sig.symbol, qty, sl_px, tp_px)
            warnings = []
            if not sl_id:
                warnings.append("SL_UNCONFIRMED")
            if not tp_id:
                warnings.append("TP_UNCONFIRMED")

            m = self.ex.market(sig.symbol)
            csize = float(m.get("contractSize") or 1)
            notional = qty * csize * fill
            margin = notional / max(sig.leverage, 1)

            trade = OpenSignal(
                symbol=sig.symbol,
                direction="LONG",
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
            log.error("place_signal: %s\n%s", e, traceback.format_exc())
            return {"ok": False, "reason": str(e)}

    def sync_open_trade(self, trade: OpenSignal) -> Optional[str]:
        if not self.keys_ok or self.ex is None or not trade.auto_trade:
            return None
        try:
            still, rem = self._position_remaining(trade.symbol)
        except Exception as e:
            log.warning("sync positions FAILED (not flat): %s", e)
            return None
        if not still or rem <= 0:
            return "exchange_flat"

        trade.contracts_remaining = rem
        try:
            t = self.ex.fetch_ticker(trade.symbol)
            px = float(t.get("last") or t.get("close") or 0)
        except Exception:
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
            and px >= trade.tp1 + buf
        ):
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
            if q >= mn and (rem - q) >= mn * 0.99:
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
                        sl_id, tp_id = self._place_protective(
                            trade.symbol, rem2, trade.stop, trade.tp2
                        )
                        trade.sl_order_id = sl_id
                        trade.tp2_order_id = tp_id
                    except Exception as e:
                        log.error("TP1 re-arm: %s", e)
                    return "tp1_partial"
            else:
                trade.tp1_done = True

        if trade.tp2 > 0 and px >= trade.tp2 + buf:
            if self.close_all_remaining(trade):
                return "tp2_hit"
            return None
        return None

    def close_all_remaining(self, trade: OpenSignal) -> bool:
        if not self.ex:
            return False
        try:
            still, rem = self._position_remaining(trade.symbol)
        except Exception:
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
        q = qty if qty is not None else trade.contracts_remaining or trade.contracts
        if not q or q <= 0:
            return True
        for attempt in range(1, 4):
            try:
                q = float(self.ex.amount_to_precision(trade.symbol, q))
                self.ex.create_order(
                    trade.symbol,
                    "market",
                    "sell",
                    q,
                    None,
                    {
                        "tdMode": "isolated",
                        "marginMode": "isolated",
                        "reduceOnly": True,
                    },
                )
                log.info("Closed %s qty=%s", trade.symbol, q)
                return True
            except Exception as e:
                log.error("close attempt %d: %s", attempt, e)
                time.sleep(0.4 * attempt)
                try:
                    still, rem = self._position_remaining(trade.symbol)
                    if not still:
                        return True
                    q = rem
                except Exception:
                    pass
        return False

    def fetch_balance_usdt(self) -> Optional[float]:
        if not self.ex:
            return None
        try:
            bal = self.ex.fetch_balance()
            usdt = bal.get("USDT") or {}
            free = usdt.get("free")
            if free is not None:
                return float(free)
            if usdt.get("total") is not None:
                return float(usdt["total"])
        except Exception as e:
            log.warning("balance: %s", e)
        return None

    def has_open_position_on(self, symbol: str) -> bool:
        try:
            still, rem = self._position_remaining(symbol)
            return still and rem > 0
        except Exception:
            return False

    def _set_leverage(self, symbol: str, leverage: int) -> None:
        assert self.ex is not None
        try:
            self.ex.set_margin_mode("isolated", symbol)
        except Exception as e:
            log.debug("set_margin_mode: %s", e)
        try:
            self.ex.set_leverage(leverage, symbol, {"mgnMode": "isolated"})
        except Exception as e:
            log.warning("set_leverage: %s", e)

    def _size_contracts(self, symbol: str, price: float, leverage: int) -> float:
        """OKX swap: amount = number of contracts; notional = contracts * ctVal * price."""
        assert self.ex is not None
        m = self.ex.market(symbol)
        csize = float(m.get("contractSize") or 1)
        if price <= 0:
            raise ValueError("bad price")
        notional = config.POSITION_SIZE_USD * leverage
        raw = notional / (csize * price)
        mn = float(m.get("limits", {}).get("amount", {}).get("min") or 1)
        # floor to precision then ensure min
        try:
            qty = float(self.ex.amount_to_precision(symbol, raw))
        except Exception:
            qty = math.floor(raw * 100) / 100
        if qty < mn:
            qty = mn
            qty = float(self.ex.amount_to_precision(symbol, qty))
        eff = (qty * csize * price) / max(leverage, 1)
        if eff > config.POSITION_SIZE_USD * 3.0:
            raise ValueError(f"margin ${eff:.1f} too large for target $50")
        if qty <= 0:
            raise ValueError("qty zero")
        log.info("Size %s: contracts=%s eff_margin≈$%.2f notional≈$%.2f", symbol, qty, eff, qty * csize * price)
        return qty

    def _place_protective(
        self, symbol: str, qty: float, sl_px: float, tp_px: float
    ) -> tuple[str, str]:
        """Place reduce-only conditional SL and TP (backup to attached orders)."""
        assert self.ex is not None
        sl_id, tp_id = "", ""
        try:
            o = self.ex.create_order(
                symbol,
                "market",
                "sell",
                qty,
                None,
                {
                    "tdMode": "isolated",
                    "reduceOnly": True,
                    "stopLossPrice": sl_px,
                    "slTriggerPxType": "last",
                },
            )
            sl_id = str(o.get("id") or "sl_ok")
        except Exception as e:
            log.warning("protective SL: %s", e)
        try:
            o = self.ex.create_order(
                symbol,
                "market",
                "sell",
                qty,
                None,
                {
                    "tdMode": "isolated",
                    "reduceOnly": True,
                    "takeProfitPrice": tp_px,
                    "tpTriggerPxType": "last",
                },
            )
            tp_id = str(o.get("id") or "tp_ok")
        except Exception as e:
            log.warning("protective TP: %s", e)
        return sl_id, tp_id

    def _position_remaining(self, symbol: str) -> tuple[bool, float]:
        assert self.ex is not None
        last_err = None
        positions = None
        for attempt in range(1, 4):
            try:
                try:
                    positions = self.ex.fetch_positions([symbol])
                except TypeError:
                    positions = self.ex.fetch_positions()
                break
            except Exception as e:
                last_err = e
                time.sleep(0.3 * attempt)
        if positions is None:
            raise RuntimeError(f"fetch_positions: {last_err}")

        for p in positions or []:
            info = p.get("info") or {}
            sym = str(p.get("symbol") or "")
            inst = str(info.get("instId") or "")
            m_id = ""
            try:
                m_id = self.ex.market(symbol).get("id") or ""
            except Exception:
                pass
            if symbol not in sym and inst not in (m_id, symbol) and m_id not in (inst, sym):
                if not (m_id and m_id == inst):
                    continue
            c = abs(float(p.get("contracts") or 0))
            if c == 0:
                try:
                    c = abs(float(info.get("pos") or info.get("availPos") or 0))
                except (TypeError, ValueError):
                    c = 0
            if c <= 0:
                continue
            side = (p.get("side") or info.get("posSide") or "").lower()
            if side in ("short", "sell"):
                continue
            # net mode long: pos > 0
            return True, c
        return False, 0.0


def build_executor() -> MexcExecutor:
    return MexcExecutor()


def rehydrate_from_exchange(executor: MexcExecutor) -> Optional[OpenSignal]:
    if not executor.keys_ok or not executor.ex:
        return None
    try:
        positions = executor.ex.fetch_positions()
    except Exception as e:
        log.warning("rehydrate: %s", e)
        return None

    for p in positions or []:
        info = p.get("info") or {}
        try:
            c = abs(float(p.get("contracts") or 0) or float(info.get("pos") or 0))
        except (TypeError, ValueError):
            c = 0
        if c <= 0:
            continue
        side = (p.get("side") or info.get("posSide") or "").lower()
        if side in ("short", "sell"):
            continue
        sym = str(p.get("symbol") or "")
        inst = str(info.get("instId") or "")
        if "/USDT" in sym:
            symbol = sym if ":USDT" in sym else (sym + ":USDT" if not sym.endswith(":USDT") else sym)
            if symbol.endswith("/USDT") and ":USDT" not in symbol:
                symbol = symbol + ":USDT"
        elif inst.endswith("-USDT-SWAP"):
            base = inst.split("-")[0]
            symbol = f"{base}/USDT:USDT"
        else:
            continue
        entry = float(p.get("entryPrice") or info.get("avgPx") or 0)
        lev = int(float(p.get("leverage") or info.get("lever") or config.LEVERAGE_DEFAULT))
        trade = OpenSignal(
            symbol=symbol,
            direction="LONG",
            entry=entry,
            stop=entry * 0.988 if entry else 0,
            tp1=entry * 1.012 if entry else 0,
            tp2=entry * 1.025 if entry else 0,
            tp3=entry * 1.025 if entry else 0,
            leverage=lev,
            confidence=0,
            reason_short="rehydrated_okx",
            contracts=c,
            contracts_remaining=c,
            margin_usd=config.POSITION_SIZE_USD,
            auto_trade=True,
        )
        log.warning("Rehydrated OKX LONG %s qty=%s entry=%s", symbol, c, entry)
        return trade
    return None


def format_trade_opened(sig: Signal, trade: OpenSignal, warnings: Optional[list] = None) -> str:
    inst = sig.symbol.replace("/USDT:USDT", "-USDT-SWAP").replace("/", "-")
    lines = [
        "🟢 LIVE TRADE OPENED | OKX Oversold Bounce Bot",
        f"Pair: {sig.symbol}",
        f"Direction: LONG",
        f"Fill entry: {_fmt_px(trade.entry)}",
        f"Stop-Loss: {_fmt_px(trade.stop)}",
        f"TP1 (soft): {_fmt_px(trade.tp1)} (~{config.TP1_SIZE_PCT}%)",
        f"TP2: {_fmt_px(trade.tp2)}",
        f"Leverage: {trade.leverage}x isolated",
        f"Margin≈ ${trade.margin_usd:.2f} (target ${config.POSITION_SIZE_USD:.0f})",
        f"Contracts: {trade.contracts:g} | Notional≈ ${trade.notional_usd:.2f}",
        f"Confidence: {sig.confidence:.0f}/100",
        f"Chart: https://www.okx.com/trade-swap/{inst.lower()}",
        "OKX SL/TP attached + software backup.",
    ]
    if warnings:
        lines.append("⚠ " + ", ".join(warnings) + " — verify SL/TP on OKX")
    return "\n".join(lines)
