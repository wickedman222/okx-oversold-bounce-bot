"""
ccxt exchange wrapper — public data now, private trading later.

Phase 1: market load, tickers, OHLCV (no API keys).
Phase 2: drop in MEXC keys + use executor.py for orders.

Watchlist resilience:
  - Cache last good top-N list (TTL) so a MEXC ticker blip does not drop to 10 pairs
  - Retry fetch_tickers
  - Fallback: direct MEXC contract ticker HTTP API
  - Last resort: cached list, then FORCE_PAIRS
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import ccxt
import pandas as pd
import requests

import config

log = logging.getLogger("bot.exchange")

# Refresh volume ranking at most this often (seconds). Scans use cache between.
WATCHLIST_TTL_SEC = 30 * 60  # 30 minutes
TICKER_RETRIES = 4
MEXC_CONTRACT_TICKER_URL = "https://contract.mexc.com/api/v1/contract/ticker"


def make_exchange(private: bool = False) -> ccxt.Exchange:
    params: dict[str, Any] = {
        "enableRateLimit": True,
        "timeout": config.REQUEST_TIMEOUT * 1000,
        "options": {
            "defaultType": config.MARKET_TYPE,
            "recvWindow": 10_000,
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
        self._watchlist_cache: list[str] = []
        self._watchlist_cache_ts: float = 0.0
        # Symbols that failed open due to geo/region (MEXC 8950 etc.) — session skip
        self.blocked_symbols: set[str] = set()

    def block_symbol(self, symbol: str, reason: str = "") -> None:
        self.blocked_symbols.add(symbol)
        # Drop from cache so we don't keep ranking it
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
                log.info("Markets loaded: %d symbols", len(markets))
                return markets
            except Exception as e:
                log.warning("load_markets attempt %d failed: %s", attempt, e)
                time.sleep(1.5 * attempt)
        raise RuntimeError("Could not load markets from MEXC")

    # -------------------------------------------------------------------------
    # Watchlist / volume ranking
    # -------------------------------------------------------------------------

    def list_liquid_usdt_swaps(self, force_refresh: bool = False) -> list[str]:
        """
        Top liquid USDT-M perps. Uses in-memory cache so MEXC ticker outages
        do not shrink the scan universe mid-session.
        """
        self.load_markets()
        now = time.time()
        cache_fresh = (
            self._watchlist_cache
            and (now - self._watchlist_cache_ts) < WATCHLIST_TTL_SEC
            and not force_refresh
        )
        if cache_fresh:
            wl = [s for s in self._watchlist_cache if s not in self.blocked_symbols]
            log.info(
                "Watchlist cache hit: %d pairs (age %.0fs)",
                len(wl),
                now - self._watchlist_cache_ts,
            )
            return wl

        tickers = self._fetch_tickers_resilient()
        if tickers:
            top = self._rank_from_tickers(tickers)
            if top:
                self._watchlist_cache = top
                self._watchlist_cache_ts = now
                log.info("Watchlist refreshed: %d pairs (top by volume + majors)", len(top))
                return list(top)

        # Ticker path failed — keep previous full list if we have one
        if self._watchlist_cache:
            age = now - self._watchlist_cache_ts
            log.warning(
                "Ticker refresh failed — reusing cached watchlist (%d pairs, age %.0fs)",
                len(self._watchlist_cache),
                age,
            )
            # Do not update timestamp: next cycle will retry refresh after TTL
            # But if cache is old and tickers keep failing, still use it forever
            # until a refresh succeeds (better than 10 majors only).
            return list(self._watchlist_cache)

        force = [s for s in config.FORCE_PAIRS if s in self.ex.markets]
        log.error(
            "No tickers and no cache — temporary FORCE_PAIRS only (%d). "
            "Will retry next cycle.",
            len(force),
        )
        # Seed cache with force list so we at least scan something stably
        if force:
            self._watchlist_cache = force
            self._watchlist_cache_ts = 0.0  # force retry next scan
        return force

    def _fetch_tickers_resilient(self) -> dict[str, Any]:
        """Try ccxt then direct MEXC contract API. Retries with backoff."""
        last_err: Optional[Exception] = None

        for attempt in range(1, TICKER_RETRIES + 1):
            try:
                # Prefer swap-scoped tickers when supported
                try:
                    tickers = self.ex.fetch_tickers(params={"type": "swap"})
                except TypeError:
                    tickers = self.ex.fetch_tickers()
                except Exception:
                    tickers = self.ex.fetch_tickers()
                if tickers and len(tickers) > 20:
                    log.debug("fetch_tickers OK (%d) attempt %d", len(tickers), attempt)
                    return tickers
                last_err = RuntimeError(f"empty/short tickers: {len(tickers or {})}")
            except Exception as e:
                last_err = e
                log.warning(
                    "fetch_tickers attempt %d/%d failed: %s",
                    attempt,
                    TICKER_RETRIES,
                    e,
                )
            time.sleep(1.2 * attempt)

        # Direct contract API (often more reliable than ccxt's mexc path)
        direct = self._fetch_tickers_mexc_contract_api()
        if direct:
            return direct

        log.error("All ticker sources failed (last: %s)", last_err)
        return {}

    def _fetch_tickers_mexc_contract_api(self) -> dict[str, Any]:
        """
        GET https://contract.mexc.com/api/v1/contract/ticker
        Map symbols like BTC_USDT → BTC/USDT:USDT for ranking.
        """
        try:
            r = requests.get(MEXC_CONTRACT_TICKER_URL, timeout=config.REQUEST_TIMEOUT)
            r.raise_for_status()
            payload = r.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not data:
                log.warning("MEXC contract ticker: empty data")
                return {}

            out: dict[str, Any] = {}
            for row in data:
                if not isinstance(row, dict):
                    continue
                raw_sym = str(row.get("symbol") or "")
                if not raw_sym.endswith(f"_{config.QUOTE}"):
                    continue
                base = raw_sym[: -len(config.QUOTE) - 1]
                if not base:
                    continue
                unified = f"{base}/{config.QUOTE}:{config.QUOTE}"
                # volume24 = quote volume on many MEXC contract responses
                qv = row.get("amount24") or row.get("volume24") or 0
                try:
                    qv_f = float(qv)
                except (TypeError, ValueError):
                    qv_f = 0.0
                # amount24 is often quote notional; volume24 base — prefer amount24
                last = row.get("lastPrice") or row.get("fairPrice") or 0
                try:
                    last_f = float(last)
                except (TypeError, ValueError):
                    last_f = 0.0
                # If only base volume, convert
                if qv_f > 0 and row.get("amount24") is None and last_f > 0:
                    qv_f = qv_f * last_f
                out[unified] = {
                    "symbol": unified,
                    "last": last_f,
                    "close": last_f,
                    "quoteVolume": qv_f,
                    "baseVolume": float(row.get("volume24") or 0) or None,
                    "info": row,
                }

            if out:
                log.info("MEXC contract ticker API OK: %d symbols", len(out))
            return out
        except Exception as e:
            log.warning("MEXC contract ticker API failed: %s", e)
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
        if ":USDT" in symbol:
            return True
        if m.get("swap") or m.get("linear") or m.get("type") in ("swap", "future"):
            return True
        return False

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

        # Also rank tickers that exist in API but key format matched via info
        if len(candidates) < 10:
            for symbol, t in tickers.items():
                if symbol in markets and self._is_scan_symbol(symbol, markets[symbol]):
                    if any(s == symbol for s, _ in candidates):
                        continue
                    try:
                        qv = float(t.get("quoteVolume") or 0)
                    except (TypeError, ValueError):
                        qv = 0.0
                    if qv >= config.MIN_QUOTE_VOLUME_USD:
                        candidates.append((symbol, qv))

        candidates.sort(key=lambda x: x[1], reverse=True)
        top = [s for s, _ in candidates[: config.TOP_N_PAIRS]]

        for s in config.FORCE_PAIRS:
            if s in markets and s not in top:
                top.append(s)

        # Drop session-blocked (geo) + keyword filters already applied
        top = [s for s in top if s not in self.blocked_symbols]
        return top

    # -------------------------------------------------------------------------
    # OHLCV / price
    # -------------------------------------------------------------------------

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
