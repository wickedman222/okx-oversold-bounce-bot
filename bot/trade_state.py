"""
Persistent "already-in-trade" lock + per-pair cooldowns.

Phase 1 has no exchange positions — this is a *conceptual* lock so the bot
never spams a second signal while one setup is still "open".

Release methods:
  1. Price hits SL or TP2 (auto, if enabled)
  2. Max lock age (TRADE_LOCK_MAX_HOURS)
  3. Manual: delete trade_state.json or set active=null
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import config

log = logging.getLogger("bot.state")


@dataclass
class OpenSignal:
    symbol: str
    direction: str
    entry: float
    stop: float
    tp1: float
    tp2: float
    tp3: float
    leverage: int
    confidence: float
    opened_at: float = field(default_factory=time.time)
    reason_short: str = ""

    def age_hours(self) -> float:
        return (time.time() - self.opened_at) / 3600.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OpenSignal":
        keys = cls.__dataclass_fields__
        return cls(**{k: d[k] for k in keys if k in d})


class TradeState:
    def __init__(self, path: str = config.STATE_FILE):
        self.path = Path(path)
        self.active: Optional[OpenSignal] = None
        self.cooldowns: dict[str, float] = {}  # symbol -> unix expiry
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("active"):
                self.active = OpenSignal.from_dict(data["active"])
            self.cooldowns = {k: float(v) for k, v in data.get("cooldowns", {}).items()}
            log.info(
                "State loaded | active=%s | cooldowns=%d",
                self.active.symbol if self.active else "none",
                len(self.cooldowns),
            )
        except Exception as e:
            log.warning("Failed to load state: %s", e)

    def save(self) -> None:
        payload = {
            "active": self.active.to_dict() if self.active else None,
            "cooldowns": self.cooldowns,
            "updated_at": time.time(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def is_locked(self) -> bool:
        if self.active is None:
            return False
        if self.active.age_hours() >= config.TRADE_LOCK_MAX_HOURS:
            log.info(
                "Trade lock expired (%.1fh) on %s — releasing",
                self.active.age_hours(),
                self.active.symbol,
            )
            self.clear_active("lock_expired")
            return False
        return True

    def on_cooldown(self, symbol: str) -> bool:
        exp = self.cooldowns.get(symbol, 0)
        if exp > time.time():
            return True
        if symbol in self.cooldowns:
            del self.cooldowns[symbol]
            self.save()
        return False

    def open_signal(self, sig: OpenSignal) -> None:
        self.active = sig
        self.cooldowns[sig.symbol] = time.time() + config.PAIR_COOLDOWN_HOURS * 3600
        self.save()
        log.info("LOCK ON | %s @ %.6g | lev %dx", sig.symbol, sig.entry, sig.leverage)

    def clear_active(self, reason: str = "manual") -> None:
        if self.active:
            log.info("LOCK OFF | %s | reason=%s", self.active.symbol, reason)
        self.active = None
        self.save()

    def maybe_release_on_price(self, last_price: float) -> Optional[str]:
        """Return release reason if SL/TP2 hit; else None."""
        if not config.AUTO_RELEASE_ON_SL_TP or not self.active:
            return None
        s = self.active
        if s.direction == "LONG":
            if last_price <= s.stop:
                self.clear_active("stop_hit")
                return "stop_hit"
            if last_price >= s.tp2:
                self.clear_active("tp2_hit")
                return "tp2_hit"
        else:  # SHORT future-proof
            if last_price >= s.stop:
                self.clear_active("stop_hit")
                return "stop_hit"
            if last_price <= s.tp2:
                self.clear_active("tp2_hit")
                return "tp2_hit"
        return None
