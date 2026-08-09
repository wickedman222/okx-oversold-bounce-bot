"""
ccxt wrapper — OKX Europe X-Perps (EEA).

EU accounts cannot trade global USDT-SWAP (API 50124).
We scan/trade X-Perps: FUTURES + ruleType=xperp, USDC/USD margin.
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
        """Watchlist of liquid **OKX Europe X-Perps** (not global USDT-SWAP)."""
        self.load_markets()
        now = time.time()
        if (
            self._watchlist_cache
            and (now - self._watchlist_cache_ts) < WATCHLIST_TTL_SEC
            and not force_refresh
        ):
            wl = [s for s in self._watchlist_cache if s not in self.blocked_symbols]
            log.info("X-Perp watchlist cache: %d (age %.0fs)", len(wl), now - self._watchlist_cache_ts)
            return wl

        from bot.xperp import build_xperp_watchlist

        top = build_xperp_watchlist(self.ex, top_n=config.TOP_N_PAIRS)
        top = [s for s in top if s not in self.blocked_symbols]
        if top:
            self._watchlist_cache = top
            self._watchlist_cache_ts = now
            return list(top)

        if self._watchlist_cache:
            log.warning("X-Perp build empty — using cache")
            return [s for s in self._watchlist_cache if s not in self.blocked_symbols]

        log.error("No X-Perp symbols found")
        return []

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
