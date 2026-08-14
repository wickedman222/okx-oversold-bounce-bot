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
import pandas as pd

import config
from bot.exchange import make_exchange
from bot.indicators import atr as atr_series
from bot.indicators import structure_trail_stop
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
                "AUTO_TRADE ON — live OKX Europe X-Perps | margin=$%.0f USDC isolated",
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

            # Respect OKX account max (10x)
            lev = int(max(config.LEVERAGE_MIN, min(config.LEVERAGE_MAX, sig.leverage)))
            self._ensure_net_mode()
            self._set_leverage(sig.symbol, lev)
            qty = self._size_contracts(sig.symbol, sig.entry, lev)
            # Runners: don't hard-arm exchange TP2 (would kill the trail). Soft TP3 only.
            hard_tp = (
                sig.tp3
                if bool(getattr(sig, "allow_runner", False)) and sig.tp3 > 0
                else sig.tp2
            )
            try:
                sl_px = float(self.ex.price_to_precision(sig.symbol, sig.stop))
                tp_px = float(self.ex.price_to_precision(sig.symbol, hard_tp))
            except Exception:
                sl_px, tp_px = float(sig.stop), float(hard_tp)

            params: dict[str, Any] = {
                "tdMode": "isolated",
                "marginMode": "isolated",
                # X-Perps are instType FUTURES (ccxt market.type usually 'future')
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
                "OPEN LONG %s qty=%s lev=%dx (cap %dx) margin=$%.0f SL=%s TP=%s%s",
                sig.symbol,
                qty,
                lev,
                config.LEVERAGE_MAX,
                config.POSITION_SIZE_USD,
                sl_px,
                tp_px,
                " (runner soft-TP3)" if getattr(sig, "allow_runner", False) else "",
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
            margin = notional / max(lev, 1)

            trade = OpenSignal(
                symbol=sig.symbol,
                direction="LONG",
                entry=fill,
                stop=sig.stop,
                tp1=sig.tp1,
                tp2=sig.tp2,
                tp3=sig.tp3,
                leverage=lev,
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
                trail_peak=fill,
                trail_atr=float(getattr(sig, "trail_atr", 0) or 0),
                allow_runner=bool(getattr(sig, "allow_runner", False)),
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

        # --- Trail after TP1: structure (higher-lows) + ATR giveback cap ---
        if (
            trade.tp1_done
            and getattr(config, "TRAIL_AFTER_TP1", True)
            and trade.direction == "LONG"
        ):
            peak = max(float(getattr(trade, "trail_peak", 0) or 0), px, trade.entry)
            trade.trail_peak = peak
            trail_dist = float(getattr(trade, "trail_atr", 0) or 0)
            if trail_dist <= 0:
                trail_dist = abs(trade.entry) * 0.012  # ~1.2% fallback

            new_stop = peak - trail_dist
            trail_why = "ATR trail"
            # Real support trail when we can fetch recent candles
            if getattr(config, "STRUCTURE_TRAIL", True):
                try:
                    ohlcv = self.ex.fetch_ohlcv(
                        trade.symbol,
                        timeframe=getattr(config, "SIGNAL_TIMEFRAME", "15m") or "15m",
                        limit=60,
                    )
                    if ohlcv and len(ohlcv) >= 15:
                        df = pd.DataFrame(
                            ohlcv,
                            columns=["ts", "open", "high", "low", "close", "volume"],
                        )
                        # Drop forming bar
                        if len(df) > 1:
                            df = df.iloc[:-1]
                        atr_col = atr_series(df, getattr(config, "ATR_PERIOD", 14))
                        atr_live = float(atr_col.iloc[-1]) if len(atr_col) and pd.notna(atr_col.iloc[-1]) else trail_dist / max(
                            getattr(config, "TRAIL_ATR_MULT", 1.75), 0.5
                        )
                        struct_stop, trail_why = structure_trail_stop(
                            df,
                            price=px,
                            peak=peak,
                            entry=trade.entry,
                            atr_v=atr_live,
                            swing_buffer_atr=getattr(config, "TRAIL_SWING_BUFFER_ATR", 0.25),
                            max_giveback_atr=getattr(config, "TRAIL_MAX_GIVEBACK_ATR", 2.4),
                            atr_trail_mult=getattr(config, "TRAIL_ATR_MULT", 1.75),
                        )
                        new_stop = struct_stop
                        # Keep trail_atr fresh for restarts / fallbacks
                        trade.trail_atr = atr_live * getattr(config, "TRAIL_ATR_MULT", 1.75)
                except Exception as e:
                    log.debug("structure trail OHLCV: %s", e)
                    trail_why = "ATR trail (ohlcv fail)"

            # Never trail below breakeven after TP1
            new_stop = max(new_stop, trade.entry)
            if trade.stop <= 0 or new_stop > trade.stop + buf:
                old = trade.stop
                trade.stop = new_stop
                log.info(
                    "Trail SL %s: %.6g → %.6g (peak=%.6g | %s)",
                    trade.symbol,
                    old,
                    new_stop,
                    peak,
                    trail_why,
                )
                # Best-effort update exchange SL; soft TP stays TP3 for runners
                try:
                    tp_arm = (
                        trade.tp3
                        if trade.allow_runner and trade.tp3 > 0
                        else trade.tp2
                    )
                    self._place_protective(trade.symbol, rem, trade.stop, tp_arm)
                except Exception as e:
                    log.debug("trail protective: %s", e)

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
            # X-Perps are often whole contracts (minSz=1). Don't use raw 40% float
            # that floors to 0 or leaves no runner — take max(1, floor(rem*frac)).
            frac = max(0.2, min(0.6, config.TP1_SIZE_PCT / 100.0))
            mn = float(
                (self.ex.market(trade.symbol).get("limits") or {})
                .get("amount", {})
                .get("min")
                or 1
            )
            # Prefer integer-friendly partial
            raw_q = rem * frac
            try:
                q = float(self.ex.amount_to_precision(trade.symbol, raw_q))
            except Exception:
                q = math.floor(raw_q) if rem >= 2 else 0.0

            # Ensure at least 1 lot if we have room for a runner
            if rem >= 2 * mn:
                if q < mn:
                    q = mn
                # Keep at least min remaining for runner
                if rem - q < mn:
                    q = rem - mn
                try:
                    q = float(self.ex.amount_to_precision(trade.symbol, q))
                except Exception:
                    pass

            can_partial = q >= mn and (rem - q) >= mn * 0.99
            if can_partial:
                log.info(
                    "TP1 hit px=%.6g >= %.6g — partial close qty=%s of rem=%s",
                    px,
                    trade.tp1,
                    q,
                    rem,
                )
                if self.market_close(trade, q):
                    time.sleep(0.6)
                    still2, rem2 = self._position_remaining(trade.symbol)
                    if not still2 or rem2 <= 0:
                        return "tp2_hit"
                    trade.tp1_done = True
                    trade.contracts_remaining = rem2
                    trade.contracts = rem2
                    # Breakeven + start trail peak
                    trade.stop = max(trade.stop, trade.entry) if trade.stop > 0 else trade.entry
                    trade.trail_peak = max(px, trade.entry)
                    try:
                        # Runners: soft TP3 so exchange won't full-exit at TP2
                        tp_arm = (
                            trade.tp3
                            if trade.allow_runner and trade.tp3 > 0
                            else trade.tp2
                        )
                        sl_id, tp_id = self._place_protective(
                            trade.symbol, rem2, trade.stop, tp_arm
                        )
                        trade.sl_order_id = sl_id
                        trade.tp2_order_id = tp_id
                    except Exception as e:
                        log.error("TP1 re-arm: %s", e)
                    return "tp1_partial"
                log.warning("TP1 partial close failed for %s", trade.symbol)
            else:
                # Cannot partial (tiny position) — hold full size for TP2; do NOT
                # mark tp1_done forever without logging (old bug skipped TP1 silently)
                log.info(
                    "TP1 level reached but size too small to partial "
                    "(rem=%s q=%s min=%s) — holding for TP2",
                    rem,
                    q,
                    mn,
                )
                trade.tp1_done = True  # avoid retry spam; full size runs to TP2
                return "tp1_skip_hold"

        # TP2: full exit for normal setups; high-conf runners take another
        # partial at TP2 and keep trailing to TP3 / trail stop
        if trade.tp2 > 0 and px >= trade.tp2 + buf:
            if trade.allow_runner and trade.tp1_done:
                # Scale out more at TP2, leave runner for trail / TP3
                mn = float(
                    (self.ex.market(trade.symbol).get("limits") or {})
                    .get("amount", {})
                    .get("min")
                    or 1
                )
                if rem >= 2 * mn:
                    q2 = max(mn, rem * 0.5)
                    try:
                        q2 = float(self.ex.amount_to_precision(trade.symbol, q2))
                    except Exception:
                        q2 = math.floor(rem * 0.5) or mn
                    if rem - q2 >= mn and self.market_close(trade, q2):
                        time.sleep(0.5)
                        still3, rem3 = self._position_remaining(trade.symbol)
                        if not still3 or rem3 <= 0:
                            return "tp2_hit"
                        trade.contracts_remaining = rem3
                        trade.trail_peak = max(trade.trail_peak, px)
                        log.info("TP2 scale-out runner rem=%s", rem3)
                        return "tp2_partial"
                # else fall through to full close if can't leave runner
            if self.close_all_remaining(trade):
                return "tp2_hit"
            return None

        # Soft TP3 for runners (optional full exit)
        if (
            trade.allow_runner
            and trade.tp1_done
            and trade.tp3 > 0
            and px >= trade.tp3 + buf
        ):
            if self.close_all_remaining(trade):
                return "tp3_hit"
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
        """Total free stable margin (USDC+USDT+…) for multi-currency OKX accounts."""
        detail = self.fetch_margin_balances()
        if not detail:
            return None
        return detail.get("total_free")

    def fetch_margin_balances(self) -> Optional[dict[str, Any]]:
        """
        OKX multi-ccy / EU: parse trading account balance (USDC etc.).

        OKX shape:
          info.data[0].details[] = { ccy, availEq, availBal, eq, eqUsd, ... }
          info.data[0].totalEq = account equity in USD
        """
        if not self.ex:
            return None

        free_by: dict[str, float] = {}
        total_eq_usd: Optional[float] = None
        last_err: Optional[Exception] = None
        assets = getattr(config, "MARGIN_ASSETS", ("USDC", "USDT", "USDG", "USD"))

        # Try trading account first, then funding
        for bal_type in ("trading", "funding", None):
            try:
                if bal_type:
                    bal = self.ex.fetch_balance({"type": bal_type})
                else:
                    bal = self.ex.fetch_balance()
            except Exception as e:
                last_err = e
                log.warning("balance type=%s: %s", bal_type, e)
                continue

            # Unified ccxt currency keys
            for asset in assets:
                row = bal.get(asset) or {}
                try:
                    free = row.get("free")
                    if free is None:
                        free = row.get("total")
                    if free is not None and float(free) > 0:
                        free_by[asset] = max(free_by.get(asset, 0.0), float(free))
                except (TypeError, ValueError):
                    pass

            # Raw OKX payload (nested details)
            try:
                info = bal.get("info") or {}
                data = info.get("data") or []
                if isinstance(data, dict):
                    data = [data]
                for acct in data:
                    if not isinstance(acct, dict):
                        continue
                    te = acct.get("totalEq") or acct.get("adjEq")
                    if te not in (None, ""):
                        try:
                            total_eq_usd = float(te)
                        except (TypeError, ValueError):
                            pass
                    details = acct.get("details") or []
                    if isinstance(details, dict):
                        details = [details]
                    for d in details:
                        if not isinstance(d, dict):
                            continue
                        ccy = str(d.get("ccy") or "").upper()
                        if ccy not in assets:
                            continue
                        # Prefer available for trading, then equity
                        for key in (
                            "availEq",
                            "availBal",
                            "cashBal",
                            "eq",
                            "disEq",
                            "eqUsd",
                        ):
                            raw = d.get(key)
                            if raw in (None, ""):
                                continue
                            try:
                                val = float(raw)
                            except (TypeError, ValueError):
                                continue
                            if val > 0:
                                free_by[ccy] = max(free_by.get(ccy, 0.0), val)
                                break
            except Exception as e:
                log.debug("parse balance info: %s", e)

            if free_by or total_eq_usd:
                break

        # Direct REST fallback (sometimes cleaner on EEA)
        if not free_by and self.ex is not None:
            try:
                raw = self.ex.privateGetAccountBalance({})
                data = raw.get("data") or []
                for acct in data:
                    te = acct.get("totalEq")
                    if te not in (None, ""):
                        total_eq_usd = float(te)
                    for d in acct.get("details") or []:
                        ccy = str(d.get("ccy") or "").upper()
                        if ccy not in assets:
                            continue
                        for key in ("availEq", "availBal", "cashBal", "eq", "eqUsd"):
                            raw_v = d.get(key)
                            if raw_v in (None, ""):
                                continue
                            val = float(raw_v)
                            if val > 0:
                                free_by[ccy] = max(free_by.get(ccy, 0.0), val)
                                break
            except Exception as e:
                last_err = e
                log.warning("privateGetAccountBalance: %s", e)

        if not free_by and total_eq_usd is None:
            if last_err:
                log.warning("balance unavailable: %s", last_err)
            return None

        total = sum(free_by.values())
        # If per-ccy empty but totalEq exists, use that for readiness check
        if total <= 0 and total_eq_usd is not None:
            total = total_eq_usd
            free_by["TOTAL_EQ_USD"] = total_eq_usd

        preferred = getattr(config, "MARGIN_ASSET", "USDC")
        log.info(
            "Margin free: %s | total≈$%.2f (prefer %s)",
            " ".join(f"{k}={v:.2f}" for k, v in free_by.items()) or "none",
            total,
            preferred,
        )
        return {
            "total_free": total,
            "by_asset": free_by,
            "preferred": preferred,
            "total_eq_usd": total_eq_usd,
        }

    def has_open_position_on(self, symbol: str) -> bool:
        try:
            still, rem = self._position_remaining(symbol)
            return still and rem > 0
        except Exception:
            return False

    def _ensure_net_mode(self) -> None:
        """Prefer one-way (net) position mode — simpler for this bot."""
        assert self.ex is not None
        try:
            self.ex.set_position_mode(False)  # False = one-way / net
            log.info("OKX position mode: one-way (net)")
        except Exception as e:
            log.debug("set_position_mode: %s (ok if already set or open positions)", e)

    def _set_leverage(self, symbol: str, leverage: int) -> None:
        assert self.ex is not None
        lev = int(max(1, min(10, leverage)))  # account hard cap 10x
        try:
            self.ex.set_margin_mode("isolated", symbol, {"lever": lev})
        except Exception as e:
            log.debug("set_margin_mode: %s", e)
        try:
            self.ex.set_leverage(lev, symbol, {"mgnMode": "isolated"})
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

        m_id = ""
        try:
            m_id = str(self.ex.market(symbol).get("id") or "")
        except Exception:
            m_id = ""
        # X-Perp family e.g. BTC-USD_UM_XPERP from BTC-USD_UM_XPERP-310404
        family = ""
        if "XPERP" in m_id:
            family = m_id.rsplit("-", 1)[0] if m_id.count("-") >= 2 else m_id

        for p in positions or []:
            info = p.get("info") or {}
            sym = str(p.get("symbol") or "")
            inst = str(info.get("instId") or "")
            matched = (
                symbol == sym
                or symbol in sym
                or (m_id and m_id == inst)
                or (m_id and m_id in inst)
                or (family and family in inst)
                or (inst and inst in symbol)
            )
            if not matched:
                # base match for X-Perp: BTC from BTC/USD:USD-310404
                base = symbol.split("/")[0] if "/" in symbol else ""
                if not (base and (inst.startswith(base + "-") or base + "/" in sym)):
                    continue
                if "XPERP" not in inst.upper() and "XPERP" not in m_id.upper():
                    if m_id and inst != m_id:
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
        f"Margin≈ ${trade.margin_usd:.2f} USDC (target ${config.POSITION_SIZE_USD:.0f})",
        f"Contracts: {trade.contracts:g} | Notional≈ ${trade.notional_usd:.2f}",
        f"Confidence: {sig.confidence:.0f}/100",
        f"Chart: https://www.okx.com/trade-swap/{inst.lower()}",
        "Margin currency: USDC (OKX multi-ccy). Pair name may still show USDT.",
        "OKX SL/TP attached + software backup.",
    ]
    if warnings:
        lines.append("⚠ " + ", ".join(warnings) + " — verify SL/TP on OKX")
    return "\n".join(lines)
