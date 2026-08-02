"""CSV audit log of every emitted signal."""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

import config
from bot.scanner import Signal

log = logging.getLogger("bot.signal_log")

HEADERS = [
    "utc_time",
    "symbol",
    "direction",
    "entry",
    "stop",
    "tp1",
    "tp2",
    "tp3",
    "sl_pct",
    "rr_tp2",
    "leverage",
    "confidence",
    "rsi",
    "atr_pct",
    "volume_ratio",
    "position_size_usd",
]


def append_signal(sig: Signal) -> None:
    path = Path(config.SIGNAL_LOG_CSV)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    try:
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HEADERS)
            if new_file:
                w.writeheader()
            w.writerow(
                {
                    "utc_time": datetime.now(timezone.utc).isoformat(),
                    "symbol": sig.symbol,
                    "direction": sig.direction,
                    "entry": sig.entry,
                    "stop": sig.stop,
                    "tp1": sig.tp1,
                    "tp2": sig.tp2,
                    "tp3": sig.tp3,
                    "sl_pct": round(sig.sl_pct, 4),
                    "rr_tp2": round(sig.rr_tp2, 4),
                    "leverage": sig.leverage,
                    "confidence": round(sig.confidence, 1),
                    "rsi": round(sig.rsi, 2),
                    "atr_pct": round(sig.atr_pct, 4),
                    "volume_ratio": round(sig.volume_ratio, 4),
                    "position_size_usd": sig.position_size_usd,
                }
            )
    except Exception as e:
        log.warning("Could not write signal CSV: %s", e)
