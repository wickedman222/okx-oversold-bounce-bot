"""
ccxt exchange wrapper — public data now, private trading later.

Phase 1: market load, tickers, OHLCV (no API keys).
Phase 2: drop in MEXC keys + use executor.py for orders.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import ccxt
import pandas as pd

import config

log = logging.getLogger("bot.exchange")


def make_exchange(private: bool = False) -> ccxt.Exchange:
    params: dict[str, Any] = {
        "enableRateLimit": True,
        "timeout": config.REQUEST_TIMEOUT * 1000,
        "options": {
            "defaultType": config.MARKET_TYPE,
            "fetchMarkets": {"types": [config.MARKET_TYPE]},
        },
    }
    if private and config.MEXC_API_KEY and config.MEXC_API_SECRET:
        params["apiKey"] = config.MEXC_API_KEY
        params["secret"] = config.MEXC_API_SECRET

    ex = getattr(ccxt, config.EXCHANGE_ID)(params)
    return ex


class MarketData:
    def __init__(self) -> None:
        self.ex = make_exchange(private=False)
        self._markets_loaded = False

    def load_markets(self, force: bool = False) -> dict:
        if self._markets_loaded and not force:
            return self.ex.markets
        for attempt in range(1, config.OHLCV_RETRIES + 1):
            try:
                markets = self.ex.load_markets(reload=force)
                self._markets_loaded = True
                log.info("Markets loaded: %d symbols", len(markets))
                return markets
            except Exception as e:
                log.warning("load_markets attempt %d failed: %s", attempt, e)
                time.sleep(1.5 * attempt)
        raise RuntimeError("Could not load markets from MEXC")

    def list_liquid_usdt_swaps(self) -> list[str]:
        """
        Return top liquid USDT-M perpetual symbols (ccxt unified format).
        Sorted by 24h quote volume descending.
        """
        self.load_markets()
        # Fetch tickers for volume ranking
        try:
            tickers = self.ex.fetch_tickers()
        except Exception as e:
            log.error("fetch_tickers failed: %s — falling back to FORCE_PAIRS", e)
            return [s for s in config.FORCE_PAIRS if s in self.ex.markets]

        candidates: list[tuple[str, float]] = []
        for symbol, m in self.ex.markets.items():
            if not m.get("active", True):
                continue
            if m.get("type") not in ("swap", "future", None) and not m.get("swap"):
                # MEXC ccxt: swap perps often type=swap or linear
                if not (m.get("linear") or m.get("contract")):
                    continue
            if not symbol.endswith(f"/{config.QUOTE}:{config.QUOTE}") and not (
                symbol.endswith(f"/{config.QUOTE}") and m.get("swap")
            ):
                # Prefer explicit :USDT settle format
                if m.get("settle") != config.QUOTE and m.get("quote") != config.QUOTE:
                    continue
                if config.QUOTE not in symbol:
                    continue

            # Filter non-USDT linear perps roughly
            if config.QUOTE not in symbol:
                continue
            if any(k in symbol.upper() for k in config.EXCLUDE_KEYWORDS):
                continue
            # Skip inverse / non-linear if flagged
            if m.get("inverse") is True:
                continue

            t = tickers.get(symbol) or {}
            qv = t.get("quoteVolume")
            if qv is None:
                # some venues only give baseVolume * last
                last = t.get("last") or t.get("close") or 0
                bv = t.get("baseVolume") or 0
                qv = float(bv) * float(last) if last else 0
            qv = float(qv or 0)
            if qv < config.MIN_QUOTE_VOLUME_USD:
                continue
            # Prefer symbols that look like USDT-M perps
            if ":USDT" not in symbol and not m.get("swap"):
                continue
            candidates.append((symbol, qv))

        candidates.sort(key=lambda x: x[1], reverse=True)
        top = [s for s, _ in candidates[: config.TOP_N_PAIRS]]

        # Ensure force list is present
        for s in config.FORCE_PAIRS:
            if s in self.ex.markets and s not in top:
                top.append(s)

        log.info("Watchlist: %d pairs (top by volume + majors)", len(top))
        return top

    def fetch_ohlcv_df(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
    ) -> Optional[pd.DataFrame]:
        for attempt in range(1, config.OHLCV_RETRIES + 1):
            try:
                raw = self.ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                time.sleep(config.REQUEST_SLEEP_SEC)
                if not raw or len(raw) < 50:
                    return None
                df = pd.DataFrame(
                    raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
                )
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                for c in ("open", "high", "low", "close", "volume"):
                    df[c] = df[c].astype(float)
                # Drop the still-forming candle for signal decisions
                if len(df) >= 2:
                    df = df.iloc[:-1].copy()
                return df.reset_index(drop=True)
            except ccxt.RateLimitExceeded:
                log.warning("Rate limit on %s — backoff", symbol)
                time.sleep(2.0 * attempt)
            except Exception as e:
                log.debug("OHLCV %s %s fail %d: %s", symbol, timeframe, attempt, e)
                time.sleep(0.5 * attempt)
        return None

    def last_price(self, symbol: str) -> Optional[float]:
        try:
            t = self.ex.fetch_ticker(symbol)
            time.sleep(config.REQUEST_SLEEP_SEC)
            p = t.get("last") or t.get("close")
            return float(p) if p is not None else None
        except Exception as e:
            log.debug("ticker %s: %s", symbol, e)
            return None
