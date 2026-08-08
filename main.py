#!/usr/bin/env python3
"""
Liquid Oversold Bounce — Signal + optional Bybit auto-trade
===========================================================

- FIXED $50 USDT margin per trade (POSITION_SIZE_USD)
- Max 1 open trade
- AUTO_TRADE=true + BYBIT keys → live isolated linear orders with SL/TP
- AUTO_TRADE=false → Telegram signals only
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from bot.exchange import MarketData
from bot.executor import (
    MexcExecutor,
    build_executor,
    format_trade_opened,
    rehydrate_from_exchange,
)
from bot.indicators import rsi
from bot.scanner import Signal, evaluate_pair
from bot.signal_log import append_signal
from bot.telegram_notifier import (
    send_plain,
    send_signal,
    send_status,
    send_trade_closed,
    test_connection,
)
from bot.trade_state import OpenSignal, TradeState

Path(config.LOG_DIR).mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(config.LOG_DIR) / "bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("bot.main")


def pick_best(signals: list[Signal]) -> Signal:
    return max(signals, key=lambda s: (s.confidence, s.rr_tp2, -s.atr_pct))


def manage_open_trade(
    md: MarketData,
    state: TradeState,
    executor: MexcExecutor,
) -> None:
    """While locked: sync live position or release signal-only lock on price.

    No full market scan here — only a few MEXC calls (position + ticker).
    """
    if not state.active:
        return
    trade = state.active
    symbol = trade.symbol
    prev_rem = trade.contracts_remaining
    prev_tp1 = trade.tp1_done

    log.info(
        "In-trade check: %s age=%.1fh entry=%.6g rem=%s auto=%s",
        symbol,
        trade.age_hours(),
        trade.entry,
        trade.contracts_remaining,
        trade.auto_trade,
    )

    if trade.auto_trade and executor.keys_ok:
        try:
            reason = executor.sync_open_trade(trade)
        except Exception as e:
            log.error("sync_open_trade error: %s", e)
            send_status(f"⚠ Sync error on {symbol}: {e}")
            return
        if reason == "tp1_partial":
            state.save()
            arm = []
            if not trade.sl_order_id:
                arm.append("SL re-arm failed")
            if not trade.tp2_order_id:
                arm.append("TP2 re-arm failed — set TP manually on MEXC")
            extra = (" | " + "; ".join(arm)) if arm else " | SL/TP2 re-armed for runner"
            send_status(
                f"🎯 TP1 partial on {symbol}\n"
                f"Closed ~{config.TP1_SIZE_PCT}% | remaining {trade.contracts_remaining:g}\n"
                f"Runner toward TP2={trade.tp2}{extra}"
            )
            return
        if reason in ("exchange_flat", "stop_hit", "tp2_hit"):
            px = None
            try:
                if executor.ex:
                    t = executor.ex.fetch_ticker(symbol)
                    px = float(t.get("last") or t.get("close") or 0) or None
            except Exception:
                px = md.last_price(symbol)
            state.clear_active(reason)
            send_trade_closed(symbol, reason, px)
            return
        # Only write disk if size/flags changed (saves Railway FS churn)
        if trade.contracts_remaining != prev_rem or trade.tp1_done != prev_tp1:
            state.save()
        return

    # Signal-only conceptual lock
    price = md.last_price(symbol)
    if price is None:
        return
    reason = state.maybe_release_on_price(price)
    if reason:
        send_trade_closed(symbol, reason, price)
        log.info("Released via price: %s on %s", reason, symbol)


def scan_once(
    md: MarketData,
    state: TradeState,
    executor: MexcExecutor,
) -> None:
    # After Railway redeploy, lock file is gone — pick up open MEXC positions
    if not state.is_locked() and executor.enabled:
        recovered = rehydrate_from_exchange(executor)
        if recovered:
            state.open_signal(recovered)
            send_status(
                f"♻️ Reconnected to open position {recovered.symbol}\n"
                f"Entry≈{recovered.entry} qty={recovered.contracts:g}\n"
                f"Monitoring (no new trades until flat). "
                f"Set SL/TP on MEXC if missing."
            )
            manage_open_trade(md, state, executor)
            return

    if state.is_locked():
        manage_open_trade(md, state, executor)
        return

    symbols = md.list_liquid_usdt_swaps()
    if not symbols:
        log.warning("Empty watchlist")
        return

    btc_rsi = None
    if config.BTC_FILTER_ENABLED:
        btc_df = md.fetch_ohlcv_df(
            config.BTC_SYMBOL, config.SIGNAL_TIMEFRAME, config.CANDLE_LIMIT_SIGNAL
        )
        if btc_df is not None and len(btc_df) > config.RSI_PERIOD + 2:
            btc_rsi = float(rsi(btc_df["close"], config.RSI_PERIOD).iloc[-1])
            log.info("BTC %s RSI=%.1f", config.SIGNAL_TIMEFRAME, btc_rsi)

    candidates: list[Signal] = []
    scanned = 0
    errors = 0

    for symbol in symbols:
        if symbol in md.blocked_symbols:
            continue
        if state.on_cooldown(symbol):
            continue
        try:
            df_sig = md.fetch_ohlcv_df(
                symbol, config.SIGNAL_TIMEFRAME, config.CANDLE_LIMIT_SIGNAL
            )
            if df_sig is None:
                continue
            df_tr = md.fetch_ohlcv_df(
                symbol, config.TREND_TIMEFRAME, config.CANDLE_LIMIT_TREND
            )
            if df_tr is None:
                continue
            scanned += 1
            sig = evaluate_pair(symbol, df_sig, df_tr, btc_rsi=btc_rsi)
            if sig:
                candidates.append(sig)
                log.info(
                    "CANDIDATE %s conf=%.0f rsi=%.1f lev=%dx",
                    symbol,
                    sig.confidence,
                    sig.rsi,
                    sig.leverage,
                )
        except Exception as e:
            errors += 1
            log.debug("Scan error %s: %s", symbol, e)

    log.info(
        "Scan done | pairs=%d scanned=%d candidates=%d errors=%d",
        len(symbols),
        scanned,
        len(candidates),
        errors,
    )

    if not candidates:
        return

    best = pick_best(candidates)
    log.info(
        "BEST %s conf=%.0f entry=%.6g SL=%.6g TP2=%.6g lev=%dx",
        best.symbol,
        best.confidence,
        best.entry,
        best.stop,
        best.tp2,
        best.leverage,
    )

    # ---- LIVE OPEN first (avoid TG spam on geo-blocked stock pairs) ----
    if executor.enabled:
        result = executor.place_signal(best)
        reason = str(result.get("reason") or "")
        log.info("Executor result ok=%s reason=%s", result.get("ok"), reason)

        if not result.get("ok"):
            geo = any(
                x in reason.lower()
                for x in (
                    "8950",
                    "country or region",
                    "unavailable in your country",
                    "risk management reasons",
                    "not available in your",
                )
            )
            if geo:
                md.block_symbol(best.symbol, "geo/region restricted")
                state.cooldowns[best.symbol] = time.time() + 7 * 24 * 3600
                state.save()
                send_status(
                    f"⏭ Skipped {best.symbol}\n"
                    f"Exchange blocks opening this pair (region/product rule).\n"
                    f"Pair blacklisted this session — waiting for next setup."
                )
                return

            send_signal(best)
            append_signal(best)
            send_status(
                f"❌ AUTO OPEN FAILED {best.symbol}\n"
                f"{reason}\n"
                f"Signal sent — manage manually if you want, or wait for next setup."
            )
            if "position_already_open" in reason:
                state.open_signal(
                    OpenSignal(
                        symbol=best.symbol,
                        direction=best.direction,
                        entry=best.entry,
                        stop=best.stop,
                        tp1=best.tp1,
                        tp2=best.tp2,
                        tp3=best.tp3,
                        leverage=best.leverage,
                        confidence=best.confidence,
                        reason_short="blocked_existing_pos",
                        auto_trade=True,
                    )
                )
            return

        send_signal(best)
        append_signal(best)
        trade: OpenSignal = result["trade"]
        state.open_signal(trade)
        warnings = result.get("warnings") or []
        send_plain(format_trade_opened(best, trade, warnings))
        if warnings:
            # Often a false alarm (ccxt dropped plan ids) OR plan failed but entry
            # still carried stopLossPrice/takeProfitPrice. Software backup always on.
            send_status(
                f"⚠ Plan-order ids unconfirmed on {best.symbol}: {', '.join(warnings)}.\n"
                f"Entry was sent with native SL={trade.stop} / TP2={trade.tp2}.\n"
                f"Check MEXC position for SL/TP. Bot also manages exits in software "
                f"(~every {config.IN_TRADE_POLL_SEC}s).\n"
                f"If no SL on Bybit UI → set manually now."
            )
        return

    # ---- SIGNAL ONLY ----
    if not send_signal(best):
        log.error("Telegram failed — abort this cycle")
        return
    append_signal(best)
    state.open_signal(
        OpenSignal(
            symbol=best.symbol,
            direction=best.direction,
            entry=best.entry,
            stop=best.stop,
            tp1=best.tp1,
            tp2=best.tp2,
            tp3=best.tp3,
            leverage=best.leverage,
            confidence=best.confidence,
            reason_short="; ".join(best.reasons[:3]),
            auto_trade=False,
            margin_usd=config.POSITION_SIZE_USD,
        )
    )


def start_health_server() -> None:
    port = int(os.environ.get("PORT", str(config.PORT)))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = (
                f"ok\noversold-bounce-bot\n"
                f"position_usd={config.POSITION_SIZE_USD}\n"
                f"max_open={config.MAX_OPEN_TRADES}\n"
                f"auto_trade={config.AUTO_TRADE}\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A003
            return

    def _serve() -> None:
        try:
            httpd = HTTPServer(("0.0.0.0", port), Handler)
            log.info("Health server on 0.0.0.0:%s (Railway)", port)
            httpd.serve_forever()
        except Exception as exc:
            log.warning("Health server failed (bot still runs): %s", exc)

    threading.Thread(target=_serve, name="health", daemon=True).start()


def main() -> None:
    log.info("=" * 60)
    log.info("Oversold Bounce Bot starting")
    log.info(
        "FIXED margin=$%.0f USDT per trade | max_open=%d | lev=%d–%d | scan=%ss",
        config.POSITION_SIZE_USD,
        config.MAX_OPEN_TRADES,
        config.LEVERAGE_MIN,
        config.LEVERAGE_MAX,
        config.SCAN_INTERVAL_SEC,
    )
    log.info(
        "dry_run=%s | auto_trade=%s | mexc_keys=%s",
        config.DRY_RUN,
        config.AUTO_TRADE,
        "set" if (config.BYBIT_API_KEY and config.BYBIT_API_SECRET) else "NOT SET",
    )
    log.info("Exchange=Bybit linear | Telegram chat=%s", config.TELEGRAM_CHAT_ID)
    log.info("=" * 60)

    start_health_server()

    if not test_connection():
        log.error("Telegram connection failed")
        if not config.DRY_RUN:
            sys.exit(1)

    executor = build_executor()
    bal = executor.fetch_balance_usdt() if executor.keys_ok else None
    mode = "LIVE AUTO-TRADE (BYBIT)" if executor.enabled else "SIGNAL-ONLY"
    bal_txt = f"${bal:.2f} free USDT" if bal is not None else "n/a"

    try:
        send_status(
            f"Oversold Bounce online ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC)\n"
            f"Exchange: BYBIT USDT linear\n"
            f"Mode: {mode}\n"
            f"Fixed ${config.POSITION_SIZE_USD:.0f}/trade | 1 open max\n"
            f"Balance peek: {bal_txt}"
        )
    except Exception:
        pass

    if config.AUTO_TRADE and not executor.keys_ok:
        log.error("AUTO_TRADE=true but BYBIT keys missing — refusing to run live")
        send_status(
            "⚠️ AUTO_TRADE=true but BYBIT_API_KEY/SECRET missing. "
            "Set keys on Railway or set AUTO_TRADE=false."
        )
        sys.exit(1)

    if executor.enabled and bal is not None and bal < config.POSITION_SIZE_USD:
        log.warning(
            "Free USDT $%.2f < position size $%.0f — opens may fail",
            bal,
            config.POSITION_SIZE_USD,
        )
        send_status(
            f"⚠️ Free balance ${bal:.2f} is below ${config.POSITION_SIZE_USD:.0f} margin. "
            f"Top up futures wallet or opens will fail."
        )

    md = MarketData()
    state = TradeState()
    try:
        md.load_markets()
    except Exception as e:
        log.error("Initial market load failed: %s", e)
        sys.exit(1)

    # Immediate reconnect if MEXC already has a position
    if executor.enabled and not state.active:
        recovered = rehydrate_from_exchange(executor)
        if recovered:
            state.open_signal(recovered)
            send_status(
                f"♻️ Startup: found open {recovered.symbol} on MEXC — monitoring until flat."
            )

    consecutive_failures = 0
    while True:
        cycle_start = time.time()
        try:
            scan_once(md, state, executor)
            consecutive_failures = 0
        except KeyboardInterrupt:
            log.info("Stopped by user")
            break
        except Exception as e:
            consecutive_failures += 1
            log.error("Cycle error: %s\n%s", e, traceback.format_exc())
            if consecutive_failures >= 5:
                try:
                    send_status(f"⚠️ Bot errors x{consecutive_failures}: {e}")
                except Exception:
                    pass
                consecutive_failures = 0

        elapsed = time.time() - cycle_start
        # Faster poll while a live auto-trade is open (SL/TP software backup)
        if state.active and state.active.auto_trade:
            target = float(config.IN_TRADE_POLL_SEC)
        else:
            target = float(config.SCAN_INTERVAL_SEC)
        sleep_for = max(5.0, target - elapsed)
        log.info("Sleeping %.0fs until next cycle…", sleep_for)
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            log.info("Stopped by user")
            break


if __name__ == "__main__":
    main()
