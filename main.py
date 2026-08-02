#!/usr/bin/env python3
"""
Liquid Oversold Bounce — Phase 1 Signal Bot
===========================================

Continuous loop:
  1. Load top liquid MEXC USDT-M pairs
  2. If conceptual trade lock is free → scan for high-conviction LONG setups
  3. Send one Telegram signal max (best confidence if multiple)
  4. Lock until SL/TP2 or max lock hours
  5. Sleep SCAN_INTERVAL_SEC and repeat

Run from this directory:
  python main.py

Env:
  DRY_RUN=true          — print signals, no Telegram
  LOG_LEVEL=DEBUG
  AUTO_TRADE=false      — keep false until Phase 2 is implemented
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

# Ensure project root on path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from bot.exchange import MarketData
from bot.executor import build_executor
from bot.indicators import rsi
from bot.scanner import Signal, evaluate_pair
from bot.signal_log import append_signal
from bot.telegram_notifier import (
    send_signal,
    send_status,
    send_trade_closed,
    test_connection,
)
from bot.trade_state import OpenSignal, TradeState

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
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
    """One trade at a time → only the highest confidence setup."""
    return max(signals, key=lambda s: (s.confidence, s.rr_tp2, -s.atr_pct))


def check_active_release(md: MarketData, state: TradeState) -> None:
    if not state.active:
        return
    symbol = state.active.symbol
    # Max age handled inside is_locked / clear
    price = md.last_price(symbol)
    if price is None:
        return
    reason = state.maybe_release_on_price(price)
    if reason:
        send_trade_closed(symbol, reason, price)
        log.info("Released via price: %s on %s", reason, symbol)


def scan_once(md: MarketData, state: TradeState) -> None:
    # Refresh lock expiry
    if state.is_locked():
        assert state.active is not None
        log.info(
            "In-trade lock: %s | age=%.1fh | entry=%.6g",
            state.active.symbol,
            state.active.age_hours(),
            state.active.entry,
        )
        check_active_release(md, state)
        return

    symbols = md.list_liquid_usdt_swaps()
    if not symbols:
        log.warning("Empty watchlist")
        return

    # BTC filter RSI
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

    if not send_signal(best):
        log.error("Telegram failed — not locking trade state")
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
        )
    )

    # Phase 2 hook (stub unless AUTO_TRADE + keys + implemented)
    executor = build_executor()
    if executor.enabled:
        result = executor.place_signal(best)
        log.info("Executor result: %s", result)


def start_health_server() -> None:
    """
    Railway injects PORT and health-checks the service.
    Bind a tiny HTTP server so the deploy stays alive while the scan loop runs.
    """
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
            return  # silence access logs

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
    log.info("Oversold Bounce Signal Bot starting")
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
        "set" if (config.MEXC_API_KEY and config.MEXC_API_SECRET) else "not set (signal-only OK)",
    )
    log.info("Telegram chat=%s", config.TELEGRAM_CHAT_ID)
    log.info("=" * 60)

    # Railway requires something listening on PORT
    start_health_server()

    if not test_connection():
        log.error("Telegram connection failed — fix token/chat id and retry")
        if not config.DRY_RUN:
            sys.exit(1)

    try:
        send_status(
            f"Oversold Bounce scanner online "
            f"({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC). "
            f"FIXED ${config.POSITION_SIZE_USD:.0f} USDT per trade, 1 open max, signal-only."
        )
    except Exception:
        pass

    md = MarketData()
    state = TradeState()
    try:
        md.load_markets()
    except Exception as e:
        log.error("Initial market load failed: %s", e)
        sys.exit(1)

    consecutive_failures = 0
    while True:
        cycle_start = time.time()
        try:
            scan_once(md, state)
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
        sleep_for = max(5.0, config.SCAN_INTERVAL_SEC - elapsed)
        log.info("Sleeping %.0fs until next scan…", sleep_for)
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            log.info("Stopped by user")
            break


if __name__ == "__main__":
    main()
