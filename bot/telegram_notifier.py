"""
Telegram delivery for signal messages.

Uses the Bot API HTTP endpoint (requests) — simple, no long-polling needed
for a send-only signal bot. python-telegram-bot is optional for future
interactive commands (/status, /close).
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

import config
from bot.scanner import Signal

log = logging.getLogger("bot.telegram")

API = "https://api.telegram.org/bot{token}/{method}"


def _send(text: str, parse_mode: str | None = "HTML") -> bool:
    if config.DRY_RUN:
        log.info("[DRY_RUN] Telegram message:\n%s", text)
        return True
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.error("Telegram credentials missing")
        return False

    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload: dict = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            log.error("Telegram HTTP %s: %s", r.status_code, r.text[:300])
            if parse_mode:
                payload.pop("parse_mode", None)
                r2 = requests.post(url, json=payload, timeout=30)
                if r2.status_code == 200:
                    return True
            return False
        return True
    except Exception as e:
        log.error("Telegram send failed: %s", e)
        return False


def _fmt_price(x: float) -> str:
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    if x >= 0.01:
        return f"{x:.6f}"
    return f"{x:.8f}"


def format_signal(sig: Signal) -> str:
    reasons = "\n".join(f"• {r}" for r in sig.reasons)
    notes = "\n".join(f"• {n}" for n in sig.notes)

    # MEXC-friendly pair label
    pair_label = sig.symbol.replace(":USDT", "").replace("/", "_")
    if not pair_label.endswith("_USDT") and "USDT" in sig.symbol:
        pair_label = sig.symbol

    text = f"""🟢 <b>OVERSOLD BOUNCE — LONG</b>
━━━━━━━━━━━━━━━━━━━━
<b>Pair:</b> <code>{sig.symbol}</code>
<b>MEXC style:</b> <code>{pair_label}</code>
<b>Direction:</b> LONG only
<b>Confidence:</b> {sig.confidence:.0f}/100

📍 <b>Entry</b>
• Price: <code>{_fmt_price(sig.entry)}</code>
• Zone: <code>{_fmt_price(sig.entry_zone_low)}</code> – <code>{_fmt_price(sig.entry_zone_high)}</code>
• Prefer limit near zone low; do not FOMO-chase.

⚙️ <b>Leverage</b>
• Recommended: <b>{sig.leverage}x</b> isolated
• {_escape(sig.leverage_reason)}
• Margin: <b>${sig.position_size_usd:.0f} USDC fixed</b> → notional ≈ <b>${sig.notional_usd:.0f}</b>

🛑 <b>Stop-Loss</b>
• Price: <code>{_fmt_price(sig.stop)}</code>
• Distance: <b>{sig.sl_pct:.2f}%</b> from entry
• Approx $ risk if full SL: <b>${sig.position_size_usd * (sig.sl_pct / 100) * sig.leverage:.2f}</b> (${sig.position_size_usd:.0f} × {sig.leverage}x × {sig.sl_pct:.2f}%)

🎯 <b>Take-Profits</b>
• TP1: <code>{_fmt_price(sig.tp1)}</code>  (+{sig.tp1_pct:.2f}%)  R:R {sig.rr_tp1:.2f}  — take {config.TP1_SIZE_PCT}%
• TP2: <code>{_fmt_price(sig.tp2)}</code>  (+{sig.tp2_pct:.2f}%)  R:R {sig.rr_tp2:.2f}  — take {config.TP2_SIZE_PCT}%
• TP3: <code>{_fmt_price(sig.tp3)}</code>  (+{sig.tp3_pct:.2f}%)  R:R {sig.rr_tp3:.2f}  — runner {config.TP3_SIZE_PCT}%

📐 <b>Risk : Reward</b> → TP2 = <b>1 : {sig.rr_tp2:.2f}</b>
🏔 After TP1: trail under <b>higher-lows</b>{'  ·  <b>RUNNER</b> past TP2' if getattr(sig, 'allow_runner', False) else ''}

💰 <b>Trade size:</b> exactly <b>${sig.position_size_usd:.0f} USDC</b> margin per trade (not % of account). Max 1 trade open.

📊 <b>Snapshot</b>
• RSI({config.RSI_PERIOD}): {sig.rsi:.1f}
• Vol vs avg: {sig.volume_ratio:.2f}x
• ATR%: {sig.atr_pct:.2f}%
• TF: {sig.timeframe} signal / {sig.trend_timeframe} trend

🧠 <b>Why this fired</b>
{reasons}

📝 <b>Notes</b>
{notes}

⚠️ <i>Not financial advice. Futures can liquidate. One trade at a time — bot will not send another signal until this lock clears (SL/TP2 or max lock time).</i>
"""
    return text


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def send_signal(sig: Signal) -> bool:
    ok = _send(format_signal(sig))
    if ok:
        log.info("Telegram signal sent: %s conf=%.0f lev=%dx", sig.symbol, sig.confidence, sig.leverage)
    return ok


def send_status(msg: str) -> bool:
    return _send(f"ℹ️ <b>Bot</b>\n{_escape(msg)}")


def send_trade_closed(symbol: str, reason: str, price: Optional[float] = None) -> bool:
    p = f" @ {_fmt_price(price)}" if price else ""
    emoji = "🛑" if "stop" in reason else ("🎯" if "tp" in reason else "🔓")
    return _send(
        f"{emoji} <b>Trade closed / lock free</b>\n"
        f"Pair: <code>{symbol}</code>\n"
        f"Reason: <code>{reason}</code>{p}\n"
        f"Scanner will accept new setups."
    )


def send_plain(text: str) -> bool:
    """Send multi-line plain text (auto-trade open notices)."""
    return _send(text, parse_mode=None)


def test_connection() -> bool:
    if config.DRY_RUN:
        log.info("[DRY_RUN] skip Telegram test")
        return True
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method="getMe")
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        if data.get("ok"):
            uname = data["result"].get("username", "?")
            log.info("Telegram bot OK: @%s", uname)
            return True
        log.error("Telegram getMe failed: %s", data)
        return False
    except Exception as e:
        log.error("Telegram test error: %s", e)
        return False
