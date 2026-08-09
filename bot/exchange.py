"""
ccxt wrapper — OKX USDT perpetual swaps.

Public: markets, tickers, OHLCV.
Private: make_exchange(private=True) needs key + secret + password (passphrase).
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
    # EEA (Netherlands etc.): eea.okx.com — required or private calls return 50119
    hostname = getattr(config, "OKX_HOSTNAME", "eea.okx.com") or "eea.okx.com"
    params: dict[str, Any] = {
        "enableRateLimit": True,
        "timeout": config.REQUEST_TIMEOUT * 1000,
        "hostname": hostname,
        "options": {
            "defaultType": config.MARKET_TYPE,  # swap
            "defaultMarginMode": config.MARGIN_MODE,
        },
    }
    if private and config.OKX_API_KEY and config.OKX_API_SECRET:
        params["apiKey"] = config.OKX_API_KEY
        params["secret"] = config.OKX_API_SECRET
        # OKX requires the API passphrase
        if config.OKX_API_PASSWORD:
            params["password"] = config.OKX_API_PASSWORD
    ex = getattr(ccxt, config.EXCHANGE_ID)(params)
    log.info("OKX API host: %s (private=%s)", hostname, private)
    return ex


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
        log.warning("Blocked symbol: %s (%s)", symbol, reason or "restricted")

    def load_markets(self, force: bool = False) -> dict:
        if self._markets_loaded and not force:
            return self.ex.markets
        for attempt in range(1, config.OHLCV_RETRIES + 1):
            try:
                markets = self.ex.load_markets(reload=force)
                self._markets_loaded = True
                log.info("OKX markets loaded: %d symbols", len(markets))
                return markets
            except Exception as e:
                log.warning("load_markets attempt %d: %s", attempt, e)
                time.sleep(1.5 * attempt)
        raise RuntimeError("Could not load markets from OKX")

    def list_liquid_usdt_swaps(self, force_refresh: bool = False) -> list[str]:
        self.load_markets()
        now = time.time()
        if (
            self._watchlist_cache
            and (now - self._watchlist_cache_ts) < WATCHLIST_TTL_SEC
            and not force_refresh
        ):
            wl = [s for s in self._watchlist_cache if s not in self.blocked_symbols]
            log.info("Watchlist cache: %d pairs (age %.0fs)", len(wl), now - self._watchlist_cache_ts)
            return wl

        tickers = self._fetch_tickers_resilient()
        if tickers:
            top = self._rank_from_tickers(tickers)
            if top:
                self._watchlist_cache = top
                self._watchlist_cache_ts = now
                log.info("Watchlist refreshed: %d pairs (OKX swap)", len(top))
                return list(top)

        if self._watchlist_cache:
            log.warning("Ticker fail — using cache (%d)", len(self._watchlist_cache))
            return [s for s in self._watchlist_cache if s not in self.blocked_symbols]

        force = [s for s in config.FORCE_PAIRS if s in self.ex.markets]
        log.error("FORCE_PAIRS only (%d)", len(force))
        self._watchlist_cache = force
        self._watchlist_cache_ts = 0.0
        return force

    def _fetch_tickers_resilient(self) -> dict[str, Any]:
        last_err: Optional[Exception] = None
        for attempt in range(1, TICKER_RETRIES + 1):
            try:
                tickers = self.ex.fetch_tickers()
                if tickers and len(tickers) > 20:
                    return tickers
                last_err = RuntimeError(f"short tickers {len(tickers or {})}")
            except Exception as e:
                last_err = e
                log.warning("fetch_tickers %d/%d: %s", attempt, TICKER_RETRIES, e)
            time.sleep(1.0 * attempt)
        log.error("tickers failed: %s", last_err)
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
        if symbol in self.blocked_symbols:
            return False
        if not (m.get("swap") or m.get("linear") or m.get("type") == "swap"):
            return False
        return ":USDT" in symbol or symbol.endswith("/USDT")

    def _rank_from_tickers(self, tickers: dict[str, Any]) -> list[str]:
        candidates: list[tuple[str, float]] = []
        markets = self.ex.markets or {}
        for symbol, m in markets.items():
            if not self._is_scan_symbol(symbol, m):
                continue
            t = tickers.get(symbol) or {}
            qv = t.get("quoteVolume")
            if not qv:
                last = t.get("last") or t.get("close") or 0
                bv = t.get("baseVolume") or 0
                try:
                    qv = float(bv) * float(last) if last else 0
                except (TypeError, ValueError):
                    qv = 0
            # OKX sometimes puts vol in info
            if not qv:
                info = t.get("info") or {}
                try:
                    qv = float(info.get("volCcy24h") or info.get("vol24h") or 0)
                    if qv and not info.get("volCcy24h"):
                        last = float(t.get("last") or 0)
                        qv = qv * last if last else qv
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
        # If volume ranking empty (API zeros), still use force list + some liquid swaps
        if len(top) < 5:
            for s, m in markets.items():
                if self._is_scan_symbol(s, m) and s not in top:
                    top.append(s)
                if len(top) >= config.TOP_N_PAIRS:
                    break
        return [s for s in top if s not in self.blocked_symbols]

    def fetch_ohlcv_df(self, symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
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
                time.sleep(2.0 * attempt)
            except Exception as e:
                log.debug("OHLCV %s fail: %s", symbol, e)
                time.sleep(0.4 * attempt)
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
