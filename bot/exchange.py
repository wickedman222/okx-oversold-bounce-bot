"""
ccxt exchange wrapper — Bybit USDT linear perpetuals.

Public: markets, tickers, OHLCV (no keys).
Private: via make_exchange(private=True) for executor.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import ccxt
import pandas as pd

import config

log = logging.getLogger("bot.exchange")

WATCHLIST_TTL_SEC = 30 * 60
TICKER_RETRIES = 4


def make_exchange(private: bool = False) -> ccxt.Exchange:
    params: dict[str, Any] = {
        "enableRateLimit": True,
        "timeout": config.REQUEST_TIMEOUT * 1000,
        "options": {
            "defaultType": config.MARKET_TYPE,  # linear
            "defaultSubType": "linear",
            "recvWindow": 20_000,
        },
    }
    if private and config.BYBIT_API_KEY and config.BYBIT_API_SECRET:
        params["apiKey"] = config.BYBIT_API_KEY
        params["secret"] = config.BYBIT_API_SECRET

    return getattr(ccxt, config.EXCHANGE_ID)(params)


class MarketData:
    def __init__(self) -> None:
        self.ex = make_exchange(private=False)
        self._markets_loaded = False
        self._watchlist_cache: list[str] = []
        self._watchlist_cache_ts: float = 0.0
        self.blocked_symbols: set[str] = set()

    def block_symbol(self, symbol: str, reason: str = "") -> None:
        self.blocked_symbols.add(symbol)
        if self._watchlist_cache:
            self._watchlist_cache = [s for s in self._watchlist_cache if s != symbol]
        log.warning("Blocked symbol for this session: %s (%s)", symbol, reason or "restricted")

    def load_markets(self, force: bool = False) -> dict:
        if self._markets_loaded and not force:
            return self.ex.markets
        for attempt in range(1, config.OHLCV_RETRIES + 1):
            try:
                markets = self.ex.load_markets(reload=force)
                self._markets_loaded = True
                log.info("Bybit markets loaded: %d symbols", len(markets))
                return markets
            except Exception as e:
                log.warning("load_markets attempt %d failed: %s", attempt, e)
                time.sleep(1.5 * attempt)
        raise RuntimeError("Could not load markets from Bybit")

    def list_liquid_usdt_swaps(self, force_refresh: bool = False) -> list[str]:
        self.load_markets()
        now = time.time()
        cache_fresh = (
            self._watchlist_cache
            and (now - self._watchlist_cache_ts) < WATCHLIST_TTL_SEC
            and not force_refresh
        )
        if cache_fresh:
            wl = [s for s in self._watchlist_cache if s not in self.blocked_symbols]
            log.info("Watchlist cache hit: %d pairs (age %.0fs)", len(wl), now - self._watchlist_cache_ts)
            return wl

        tickers = self._fetch_tickers_resilient()
        if tickers:
            top = self._rank_from_tickers(tickers)
            if top:
                self._watchlist_cache = top
                self._watchlist_cache_ts = now
                log.info("Watchlist refreshed: %d pairs (Bybit linear)", len(top))
                return list(top)

        if self._watchlist_cache:
            log.warning(
                "Ticker refresh failed — reusing cache (%d pairs)",
                len(self._watchlist_cache),
            )
            return [s for s in self._watchlist_cache if s not in self.blocked_symbols]

        force = [s for s in config.FORCE_PAIRS if s in self.ex.markets]
        log.error("No tickers/cache — FORCE_PAIRS only (%d)", len(force))
        self._watchlist_cache = force
        self._watchlist_cache_ts = 0.0
        return force

    def _fetch_tickers_resilient(self) -> dict[str, Any]:
        last_err: Optional[Exception] = None
        for attempt in range(1, TICKER_RETRIES + 1):
            try:
                try:
                    tickers = self.ex.fetch_tickers(params={"category": "linear"})
                except Exception:
                    tickers = self.ex.fetch_tickers()
                if tickers and len(tickers) > 20:
                    return tickers
                last_err = RuntimeError(f"short tickers: {len(tickers or {})}")
            except Exception as e:
                last_err = e
                log.warning("fetch_tickers attempt %d/%d: %s", attempt, TICKER_RETRIES, e)
            time.sleep(1.0 * attempt)
        log.error("All ticker fetches failed: %s", last_err)
        return {}

    def _is_scan_symbol(self, symbol: str, m: dict) -> bool:
        if not m.get("active", True):
            return False
        if m.get("inverse") is True:
            return False
        if config.QUOTE not in symbol:
            return False
        if any(k in symbol.upper() for k in config.EXCLUDE_KEYWORDS):
            return False
        base = symbol.split("/")[0].upper() if "/" in symbol else symbol.upper()
        if base in getattr(config, "EXCLUDE_BASES", ()):
            return False
        if "STOCK" in base:
            return False
        if symbol in self.blocked_symbols:
            return False
        # Linear USDT perps
        if m.get("linear") or m.get("swap") or m.get("type") in ("swap", "future"):
            if ":USDT" in symbol or symbol.endswith("/USDT"):
                return True
        return ":USDT" in symbol

    def _rank_from_tickers(self, tickers: dict[str, Any]) -> list[str]:
        candidates: list[tuple[str, float]] = []
        markets = self.ex.markets or {}

        for symbol, m in markets.items():
            if not self._is_scan_symbol(symbol, m):
                continue
            t = tickers.get(symbol) or {}
            qv = t.get("quoteVolume")
            if qv is None:
                last = t.get("last") or t.get("close") or 0
                bv = t.get("baseVolume") or 0
                try:
                    qv = float(bv) * float(last) if last else 0
                except (TypeError, ValueError):
                    qv = 0
            try:
                qv = float(qv or 0)
            except (TypeError, ValueError):
                qv = 0.0
            if qv < config.MIN_QUOTE_VOLUME_USD:
                continue
            candidates.append((symbol, qv))

        candidates.sort(key=lambda x: x[1], reverse=True)
        top = [s for s, _ in candidates[: config.TOP_N_PAIRS]]
        for s in config.FORCE_PAIRS:
            if s in markets and s not in top:
                top.append(s)
        return [s for s in top if s not in self.blocked_symbols]

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
