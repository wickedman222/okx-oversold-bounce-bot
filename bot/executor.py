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
            try:
                sl_px = float(self.ex.price_to_precision(sig.symbol, sig.stop))
            except Exception:
                sl_px = float(sig.stop)

            # Entry: SL only on the parent order (immediate risk cover).
            # Full multi-TP ladder is armed after fill so sizes match live qty.
            params: dict[str, Any] = {
                "tdMode": "isolated",
                "marginMode": "isolated",
                "stopLoss": {
                    "triggerPrice": sl_px,
                    "type": "market",
                },
            }

            log.info(
                "OPEN LONG %s qty=%s lev=%dx (cap %dx) margin=$%.0f SL=%s "
                "then arm TP1/TP2/TP3 partials",
                sig.symbol,
                qty,
                lev,
                config.LEVERAGE_MAX,
                config.POSITION_SIZE_USD,
                sl_px,
            )
            order = self.ex.create_order(sig.symbol, "market", "buy", qty, None, params)
            fill = float(order.get("average") or order.get("price") or sig.entry)
            time.sleep(1.0)

            still, live_qty = self._position_remaining(sig.symbol)
            if still and live_qty > 0:
                qty = live_qty

            # Cancel any single attached SL and place clean SL + TP1/TP2/TP3 ladder
            arm = self._arm_full_exits(
                sig.symbol,
                qty,
                sl_px=float(sig.stop),
                tp1=float(sig.tp1),
                tp2=float(sig.tp2),
                tp3=float(sig.tp3),
                tp1_done=False,
            )
            warnings = list(arm.get("warnings") or [])
            if not arm.get("sl_id"):
                warnings.append("SL_UNCONFIRMED")
            if not arm.get("any_tp"):
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
                sl_order_id=str(arm.get("sl_id") or ""),
                tp1_order_id=str(arm.get("tp1_id") or ""),
                tp2_order_id=str(arm.get("tp2_id") or ""),
                tp3_order_id=str(arm.get("tp3_id") or ""),
                tp1_done=False,
                auto_trade=True,
                trail_peak=fill,
                trail_atr=float(getattr(sig, "trail_atr", 0) or 0),
                allow_runner=bool(getattr(sig, "allow_runner", False)),
                exits_armed=bool(arm.get("sl_id") and arm.get("any_tp")),
            )
            return {
                "ok": True,
                "trade": trade,
                "fill": fill,
                "qty": qty,
                "margin": margin,
                "notional": notional,
                "warnings": warnings,
                "arm": arm,
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

        # Detect exchange-side TP1 fill (size already reduced without our flag)
        orig = float(trade.contracts or 0)
        if (
            not trade.tp1_done
            and orig > 0
            and rem > 0
            and rem <= orig * 0.78
        ):
            log.info(
                "Exchange reduced size %s → %s — treating as TP1 done",
                orig,
                rem,
            )
            trade.tp1_done = True
            trade.contracts = rem
            if trade.stop > 0:
                trade.stop = max(trade.stop, trade.entry)
            else:
                trade.stop = trade.entry
            trade.exits_armed = False  # rebuild ladder for remainder

        trade.contracts_remaining = rem
        try:
            t = self.ex.fetch_ticker(trade.symbol)
            px = float(t.get("last") or t.get("close") or 0)
        except Exception:
            return None
        if px <= 0:
            return None

        buf = abs(trade.entry) * 0.0003

        # If SL/TPs never fully armed (restart, old code, or failed open) — fix once
        if not getattr(trade, "exits_armed", False) and rem > 0:
            try:
                arm = self._arm_full_exits(
                    trade.symbol,
                    rem,
                    sl_px=float(trade.stop),
                    tp1=float(trade.tp1),
                    tp2=float(trade.tp2),
                    tp3=float(trade.tp3),
                    tp1_done=bool(trade.tp1_done),
                )
                trade.sl_order_id = str(arm.get("sl_id") or trade.sl_order_id or "")
                trade.tp1_order_id = str(arm.get("tp1_id") or "")
                trade.tp2_order_id = str(arm.get("tp2_id") or "")
                trade.tp3_order_id = str(arm.get("tp3_id") or "")
                trade.exits_armed = bool(arm.get("sl_id") and arm.get("any_tp"))
                log.info(
                    "Exit ladder re-arm %s: SL=%s TP1=%s TP2=%s TP3=%s armed=%s",
                    trade.symbol,
                    bool(arm.get("sl_id")),
                    bool(arm.get("tp1_id")),
                    bool(arm.get("tp2_id")),
                    bool(arm.get("tp3_id")),
                    trade.exits_armed,
                )
            except Exception as e:
                log.warning("exit ladder re-arm failed: %s", e)

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
                # Re-ladder with higher SL + remaining TP sizes (cancel old, place clean)
                try:
                    arm = self._arm_full_exits(
                        trade.symbol,
                        rem,
                        sl_px=float(trade.stop),
                        tp1=float(trade.tp1),
                        tp2=float(trade.tp2),
                        tp3=float(trade.tp3),
                        tp1_done=True,
                    )
                    trade.sl_order_id = str(arm.get("sl_id") or "")
                    trade.tp2_order_id = str(arm.get("tp2_id") or "")
                    trade.tp3_order_id = str(arm.get("tp3_id") or "")
                    trade.exits_armed = bool(arm.get("sl_id") and arm.get("any_tp"))
                except Exception as e:
                    log.debug("trail SL re-arm: %s", e)

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
                        # Re-arm SL + remaining TP2/TP3 for leftover size
                        arm = self._arm_full_exits(
                            trade.symbol,
                            rem2,
                            sl_px=float(trade.stop),
                            tp1=float(trade.tp1),
                            tp2=float(trade.tp2),
                            tp3=float(trade.tp3),
                            tp1_done=True,
                        )
                        trade.sl_order_id = str(arm.get("sl_id") or "")
                        trade.tp1_order_id = ""
                        trade.tp2_order_id = str(arm.get("tp2_id") or "")
                        trade.tp3_order_id = str(arm.get("tp3_id") or "")
                        trade.exits_armed = bool(arm.get("sl_id") and arm.get("any_tp"))
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
                        try:
                            arm = self._arm_full_exits(
                                trade.symbol,
                                rem3,
                                sl_px=float(trade.stop),
                                tp1=float(trade.tp1),
                                tp2=float(trade.tp2),
                                tp3=float(trade.tp3),
                                tp1_done=True,
                                tp2_done=True,
                            )
                            trade.sl_order_id = str(arm.get("sl_id") or "")
                            trade.tp2_order_id = ""
                            trade.tp3_order_id = str(arm.get("tp3_id") or arm.get("tp2_id") or "")
                            trade.exits_armed = bool(arm.get("sl_id") and arm.get("any_tp"))
                        except Exception as e:
                            log.warning("TP2 runner re-arm: %s", e)
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

    def _min_amt(self, symbol: str) -> float:
        assert self.ex is not None
        try:
            return float(
                (self.ex.market(symbol).get("limits") or {})
                .get("amount", {})
                .get("min")
                or 1
            )
        except Exception:
            return 1.0

    def _prec_amt(self, symbol: str, qty: float) -> float:
        assert self.ex is not None
        try:
            return float(self.ex.amount_to_precision(symbol, qty))
        except Exception:
            return float(qty)

    def _prec_px(self, symbol: str, px: float) -> float:
        assert self.ex is not None
        try:
            return float(self.ex.price_to_precision(symbol, px))
        except Exception:
            return float(px)

    def _split_tp_sizes(
        self,
        symbol: str,
        total: float,
        tp1_done: bool = False,
        tp2_done: bool = False,
    ) -> tuple[float, float, float]:
        """
        Split position into TP1/TP2/TP3 lot sizes (sum == total when possible).
        Whole-contract friendly (X-Perp min often 1).
        """
        mn = self._min_amt(symbol)
        total = self._prec_amt(symbol, total)
        if total <= 0:
            return 0.0, 0.0, 0.0

        if tp2_done:
            # Runner only → full remainder on TP3 (or TP2 if no tp3)
            return 0.0, 0.0, total

        if tp1_done:
            # Remaining after TP1: split between TP2 and TP3
            if total < 2 * mn:
                return 0.0, total, 0.0
            f2 = max(
                0.35,
                min(
                    0.65,
                    config.TP2_SIZE_PCT
                    / max(1.0, config.TP2_SIZE_PCT + config.TP3_SIZE_PCT),
                ),
            )
            q2 = self._prec_amt(symbol, max(mn, total * f2))
            if total - q2 < mn:
                q2 = self._prec_amt(symbol, total - mn)
            q3 = self._prec_amt(symbol, total - q2)
            if q3 < mn:
                return 0.0, total, 0.0
            return 0.0, q2, q3

        # Full ladder
        if total < 2 * mn:
            # Can't partial — single full TP at TP2 (software still tracks TP1/TP3)
            return 0.0, total, 0.0

        f1 = config.TP1_SIZE_PCT / 100.0
        f2 = config.TP2_SIZE_PCT / 100.0
        q1 = self._prec_amt(symbol, max(mn, total * f1))
        q2 = self._prec_amt(symbol, max(mn, total * f2))
        # Keep at least mn for runner when possible
        if q1 + q2 >= total:
            if total >= 3 * mn:
                q1 = mn
                q2 = mn
            elif total >= 2 * mn:
                q1 = mn
                q2 = self._prec_amt(symbol, total - mn)
                return q1, q2, 0.0
            else:
                return 0.0, total, 0.0
        q3 = self._prec_amt(symbol, total - q1 - q2)
        if q3 < mn:
            # Fold runner into TP2
            q2 = self._prec_amt(symbol, total - q1)
            q3 = 0.0
            if q2 < mn:
                return 0.0, total, 0.0
        # Final sum check
        if q3 > 0:
            q3 = self._prec_amt(symbol, total - q1 - q2)
            if q3 < mn:
                q2 = self._prec_amt(symbol, total - q1)
                q3 = 0.0
        return q1, q2, q3

    def _cancel_protective_orders(self, symbol: str) -> int:
        """Cancel open reduce-only / conditional SL-TP algos so we can re-ladder cleanly."""
        assert self.ex is not None
        cancelled = 0
        seen: set[str] = set()

        def _cancel_one(oid: str, params: Optional[dict] = None) -> None:
            nonlocal cancelled
            if not oid or oid in seen:
                return
            seen.add(oid)
            try:
                if params:
                    self.ex.cancel_order(oid, symbol, params)
                else:
                    self.ex.cancel_order(oid, symbol)
                cancelled += 1
            except Exception:
                try:
                    self.ex.cancel_order(oid, symbol, {"stop": True})
                    cancelled += 1
                except Exception as e:
                    log.debug("cancel %s: %s", oid, e)

        param_sets: list[dict] = [
            {},
            {"stop": True},
            {"trigger": True},
            {"ordType": "conditional"},
            {"algoId": True},
        ]
        for params in param_sets:
            try:
                if params:
                    orders = self.ex.fetch_open_orders(symbol, params=params)
                else:
                    orders = self.ex.fetch_open_orders(symbol)
            except Exception:
                continue
            for o in orders or []:
                oid = str(o.get("id") or "")
                side = (o.get("side") or "").lower()
                # Only cancel sells / reduce-only protective legs
                reduce = bool((o.get("reduceOnly") is True) or (o.get("info") or {}).get("reduceOnly"))
                if side in ("sell", "short") or reduce or params:
                    _cancel_one(oid, params if params else None)

        # OKX native algo pending cancel (X-Perp = FUTURES, global swap = SWAP)
        for inst_type in ("FUTURES", "SWAP"):
            try:
                m = self.ex.market(symbol)
                inst_id = m.get("id") or ""
                if not inst_id:
                    continue
                resp = self.ex.privateGetTradeOrdersAlgoPending(
                    {
                        "instType": inst_type,
                        "instId": inst_id,
                        "ordType": "conditional",
                    }
                )
                data = (resp or {}).get("data") or []
                for row in data:
                    algo_id = str(row.get("algoId") or "")
                    if not algo_id:
                        continue
                    try:
                        self.ex.privatePostTradeCancelAlgos(
                            [{"algoId": algo_id, "instId": inst_id}]
                        )
                        cancelled += 1
                        seen.add(algo_id)
                    except Exception as e:
                        log.debug("cancel algo %s: %s", algo_id, e)
            except Exception as e:
                log.debug("algo pending %s: %s", inst_type, e)

        if cancelled:
            log.info("Cancelled %d protective/algo order(s) on %s", cancelled, symbol)
            time.sleep(0.4)
        return cancelled

    def _create_sl(self, symbol: str, qty: float, sl_px: float) -> str:
        assert self.ex is not None
        q = self._prec_amt(symbol, qty)
        px = self._prec_px(symbol, sl_px)
        if q <= 0 or px <= 0:
            return ""
        try:
            o = self.ex.create_order(
                symbol,
                "market",
                "sell",
                q,
                None,
                {
                    "tdMode": "isolated",
                    "reduceOnly": True,
                    "stopLossPrice": px,
                    "slTriggerPxType": "last",
                },
            )
            return str(o.get("id") or "sl_ok")
        except Exception as e:
            log.warning("protective SL @ %s qty=%s: %s", px, q, e)
            return ""

    def _create_tp(self, symbol: str, qty: float, tp_px: float, tag: str = "TP") -> str:
        assert self.ex is not None
        q = self._prec_amt(symbol, qty)
        px = self._prec_px(symbol, tp_px)
        if q <= 0 or px <= 0:
            return ""
        try:
            o = self.ex.create_order(
                symbol,
                "market",
                "sell",
                q,
                None,
                {
                    "tdMode": "isolated",
                    "reduceOnly": True,
                    "takeProfitPrice": px,
                    "tpTriggerPxType": "last",
                },
            )
            oid = str(o.get("id") or f"{tag}_ok")
            log.info("Armed %s %s qty=%s @ %s id=%s", tag, symbol, q, px, oid)
            return oid
        except Exception as e:
            log.warning("protective %s @ %s qty=%s: %s", tag, px, q, e)
            return ""

    def _arm_full_exits(
        self,
        symbol: str,
        qty: float,
        sl_px: float,
        tp1: float,
        tp2: float,
        tp3: float,
        tp1_done: bool = False,
        tp2_done: bool = False,
    ) -> dict[str, Any]:
        """
        Place exchange SL (full size) + partial TP1/TP2/TP3 reduce-only legs.
        Cancels prior protective algos first so the ladder is clean.
        """
        assert self.ex is not None
        out: dict[str, Any] = {
            "sl_id": "",
            "tp1_id": "",
            "tp2_id": "",
            "tp3_id": "",
            "any_tp": False,
            "q1": 0.0,
            "q2": 0.0,
            "q3": 0.0,
            "warnings": [],
        }
        if qty <= 0:
            out["warnings"].append("qty_zero")
            return out

        self._cancel_protective_orders(symbol)

        q1, q2, q3 = self._split_tp_sizes(
            symbol, qty, tp1_done=tp1_done, tp2_done=tp2_done
        )
        out["q1"], out["q2"], out["q3"] = q1, q2, q3

        # SL covers full remaining position
        out["sl_id"] = self._create_sl(symbol, qty, sl_px)
        if not out["sl_id"]:
            out["warnings"].append("SL_FAILED")

        # TP ladder
        if not tp1_done and not tp2_done and q1 > 0 and tp1 > 0:
            out["tp1_id"] = self._create_tp(symbol, q1, tp1, "TP1")
        if not tp2_done and q2 > 0 and tp2 > 0:
            out["tp2_id"] = self._create_tp(symbol, q2, tp2, "TP2")
        if q3 > 0 and tp3 > 0:
            out["tp3_id"] = self._create_tp(symbol, q3, tp3, "TP3")
        elif q3 > 0 and tp2 > 0:
            # no usable tp3 — put remainder on TP2
            out["tp2_id"] = self._create_tp(symbol, q3 + (q2 if not out["tp2_id"] else 0), tp2, "TP2")
            out["q2"] = q2 + q3
            out["q3"] = 0.0
        elif not out["tp1_id"] and not out["tp2_id"] and not out["tp3_id"] and tp2 > 0:
            out["tp2_id"] = self._create_tp(symbol, qty, tp2, "TP2")

        out["any_tp"] = bool(out["tp1_id"] or out["tp2_id"] or out["tp3_id"])
        if not out["any_tp"]:
            out["warnings"].append("ALL_TP_FAILED")

        log.info(
            "Exit ladder %s qty=%s SL=%s | TP1=%s@%s | TP2=%s@%s | TP3=%s@%s",
            symbol,
            qty,
            bool(out["sl_id"]),
            out["q1"],
            "done" if tp1_done else tp1,
            out["q2"],
            "done" if tp2_done else tp2,
            out["q3"],
            tp3,
        )
        return out

    def _place_protective(
        self, symbol: str, qty: float, sl_px: float, tp_px: float
    ) -> tuple[str, str]:
        """Legacy single SL+TP — prefer _arm_full_exits for multi-TP."""
        arm = self._arm_full_exits(
            symbol, qty, sl_px=sl_px, tp1=0, tp2=tp_px, tp3=0, tp1_done=True
        )
        return str(arm.get("sl_id") or ""), str(arm.get("tp2_id") or "")

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
    q1, q2, q3 = "?", "?", "?"
    try:
        # Sizes were computed at arm time; show % targets
        q1 = f"~{config.TP1_SIZE_PCT}%"
        q2 = f"~{config.TP2_SIZE_PCT}%"
        q3 = f"~{config.TP3_SIZE_PCT}%"
    except Exception:
        pass
    lines = [
        "🟢 LIVE TRADE OPENED | OKX Oversold Bounce Bot",
        f"Pair: {sig.symbol}",
        f"Direction: LONG",
        f"Fill entry: {_fmt_px(trade.entry)}",
        f"Stop-Loss: {_fmt_px(trade.stop)}",
        f"TP1: {_fmt_px(trade.tp1)} ({q1})"
        + (" ✓" if trade.tp1_order_id else " — check OKX"),
        f"TP2: {_fmt_px(trade.tp2)} ({q2})"
        + (" ✓" if trade.tp2_order_id else " — check OKX"),
        f"TP3: {_fmt_px(trade.tp3)} ({q3})"
        + (" ✓" if trade.tp3_order_id else " — check OKX"),
        f"Leverage: {trade.leverage}x isolated",
        f"Margin≈ ${trade.margin_usd:.2f} USDC (target ${config.POSITION_SIZE_USD:.0f})",
        f"Contracts: {trade.contracts:g} | Notional≈ ${trade.notional_usd:.2f}",
        f"Confidence: {sig.confidence:.0f}/100",
        f"Exits armed on OKX: {'YES' if trade.exits_armed else 'PARTIAL/NO — software backup on'}",
        f"Chart: https://www.okx.com/trade-swap/{inst.lower()}",
        "Margin: USDC. Exchange ladder = SL + partial TP1/TP2/TP3 + software backup.",
    ]
    if warnings:
        lines.append("⚠ " + ", ".join(warnings) + " — verify SL/TP on OKX")
    return "\n".join(lines)
