# Liquid Oversold Bounce — Signal Bot (Phase 1)

High-selectivity **LONG-only** crypto futures **signal bot**.  
Scans liquid MEXC USDT-M perps → Telegram with entry / SL / multi-TP / **variable leverage**.

| Setting | Value |
|--------|--------|
| Position size | **Exactly $50 USDT margin per trade** (fixed, not % of account) |
| Max open trades | **1** (hard lock) |
| Leverage | **Variable 3–12×** (volatility + confidence) |
| Phase 1 | Signals only → Telegram |
| Phase 2 | MEXC auto-trade (stub; keys via Railway Variables) |
| Deploy | **Railway** — see [RAILWAY.md](RAILWAY.md) |
| Exchange data | MEXC public via **ccxt** |

**Deploy first:** open `START_HERE.txt` and `RAILWAY.md`.

---

## Honest strategy review (read before going live)

### What the edge claims
Mean-reversion **long** when:
1. **4h still in uptrend** (close > rising EMA50)  
2. **15m deeply oversold** (RSI + lower Bollinger)  
3. **Bounce clues** (RSI turning up, bullish/recovery candle, volume)  
4. **ATR stop** + multi-TP with minimum R:R  

Low frequency by design. Spam is a feature failure.

### Risks & weaknesses (leveraged futures, $50)

1. **Liquidation ≠ position size**  
   $50 margin at 10× = $500 notional. A ~10% adverse move can wipe the margin. The bot caps leverage so the **stop sits inside a rough liquidation buffer**, but funding, gaps, and exchange MMR still matter.

2. **Oversold can stay oversold**  
   In a real breakdown, RSI < 30 + lower BB is a **falling knife**. The 4h filter reduces this; it does not eliminate it. One news candle can gap through SL on alts.

3. **False “uptrend”**  
   Price above EMA50 on 4h can still be a bear-market rally. No higher-timeframe structure (HH/HL) is enforced beyond EMA slope — consider adding that if signals chop.

4. **Only LONGs**  
   In prolonged bear markets you get **few or zero** signals (good) or late bounce attempts that fail (bad). Do not force more signals by loosening RSI.

5. **Execution gap (Phase 1)**  
   You receive Telegram → you click MEXC. Slippage, missed fills, and emotional override are your risk. Treat signals as **plans**, not orders.

6. **Signal lock is conceptual**  
   Until Phase 2, the bot cannot see your real MEXC position. If you ignore a signal or close early, clear `trade_state.json` or wait for auto-release (SL/TP2 price or max hours).

7. **Survivorship / liquidity**  
   Top volume pairs still include narrative trash that mean-reverts poorly. Prefer majors when confidence is similar (bot already ranks by confidence).

8. **No edge guarantee**  
   This is a disciplined **rule set**, not a money printer. Expect losing streaks. Risk per stop at $50 × lev × SL% should be money you can lose.

### Recommended improvements (already partly baked in)

| Improvement | Status |
|-------------|--------|
| ATR-based SL (not fixed %) | ✅ |
| Variable leverage from ATR + confidence | ✅ |
| BTC dump filter for alts | ✅ |
| One-trade lock + pair cooldown | ✅ |
| Min confidence + min R:R | ✅ |
| Closed-candle only (no live-bar spoof) | ✅ |
| Higher-TF structure (swing HL) | Optional next |
| Funding-rate note in signal | Optional (API) |
| Session filter (avoid illiquid hours) | Optional |

### Practical leverage guidance for $50

| Setup quality | Typical lev | Rough notional |
|---------------|-------------|----------------|
| Choppy / high ATR | 3–5× | $150–250 |
| Average clean bounce | 5–7× | $250–350 |
| Very clean, tight SL, high conf | 8–12× | $400–600 |

**Never** raise leverage after entry. **Never** remove the stop.  
If SL% is ~1.5% and lev is 8×, full stop ≈ **$50 × 0.015 × 8 = $6** risk (plus fees) — that is the healthy frame. If risk approaches $20+ on a stop, lev or SL is wrong.

---

## Quick start

### 1. Install

```powershell
cd C:\Users\Gebruiker\oversold_bounce_bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Config

Telegram is **hardcoded** in `config.py` (as requested).  
Position size is **fixed $50 per trade** (`POSITION_SIZE_USD`), never a % of balance.

Optional local `.env` (or Railway **Variables** tab):

```env
POSITION_SIZE_USD=50
SCAN_INTERVAL_SEC=180
DRY_RUN=false
AUTO_TRADE=false
# Phase 2 later:
# MEXC_API_KEY=
# MEXC_API_SECRET=
```

### 3. Dry run (recommended first)

```powershell
$env:DRY_RUN="true"
python main.py
```

You should see scan logs; signals print to console without Telegram.

### 4. Live signals

```powershell
$env:DRY_RUN="false"
python main.py
```

Leave the terminal open (or use Task Scheduler / Railway later).  
On start you get a short “scanner online” Telegram message.

### 5. Manual unlock

If you closed a trade early and want new signals:

- Delete `trade_state.json`, or  
- Set `"active": null` inside it.

---

## What each Telegram signal contains

- Pair (ccxt + MEXC-style label)  
- Direction: LONG  
- Entry price + zone  
- Recommended leverage + short justification  
- Exact SL price and %  
- TP1 / TP2 / TP3 with partial % suggestions  
- R:R to TP2  
- $50 position size reminder  
- Why it triggered (bullet reasons)  
- Notes (volatility, BTC filter, Phase-1 warning)  

---

## Default parameters (smart starting point)

| Param | Default | Notes |
|-------|---------|--------|
| Signal TF | 15m | Bounce timing |
| Trend TF | 4h | EMA50 uptrend |
| RSI oversold | ≤ 32 | Selective; lower = fewer signals |
| Near lower BB | within 0.35% | Mean-reversion anchor |
| SL | 1.4 × ATR, clamp 0.6–3.5% | Skip if too wide |
| TP | 1.2R / 2.2R / 3.5R | Partials 40/40/20 |
| Min conf | 68 | Raise to 75 if any spam |
| Pair cooldown | 8h | Anti-repeat |
| Trade lock | until SL/TP2 or 12h | One at a time |
| Top pairs | 40 by volume | + forced majors |

Tune **only after** 20+ paper signals. Do not loosen three knobs at once.

---

## Testing plan ($50 + leverage)

### Stage A — Paper / dry run (3–7 days)
1. `DRY_RUN=true`, run continuously.  
2. Log every candidate in `logs/signals.csv`.  
3. For each signal, **simulate** fill at signal entry, SL, TPs on the chart later.  
4. Track: win rate to TP1, full SL rate, average R, max losing streak.  
5. If >1 signal/day average → raise `MIN_CONFIDENCE` or tighten `RSI_OVERSOLD`.

### Stage B — Manual MEXC (1–2 weeks)
1. Live Telegram, **you** enter on MEXC.  
2. Always: isolated, bot’s leverage or **lower**, SL in immediately.  
3. Size **$50 margin only** — no “this one looks good” size-up.  
4. Record real fill vs signal entry (slippage).  
5. Stop if 4 full SL in a row without TP2 offset — review market regime.

### Stage C — Auto-trade (Phase 2)
1. Implement `bot/executor.py` (currently stubbed on purpose).  
2. API key: **trade + read**, withdraw disabled.  
3. First week: max 3× leverage override, still $50.  
4. Compare bot fills vs Stage B slippage.

**Kill criteria (stop the bot):**  
- Exchange outage / desync  
- Equity drawdown you pre-define (e.g. −30% of the $50 risk budget stack)  
- Regime change: BTC aggressive lower highs + most alts failing 4h EMA  

---

## Project layout

```
oversold_bounce_bot/
  main.py                 # loop
  config.py               # all knobs + secrets from env
  requirements.txt
  .env.example
  bot/
    exchange.py           # ccxt MEXC public (+ private later)
    indicators.py         # RSI, EMA, ATR, BB
    scanner.py            # strategy + Signal model
    leverage.py           # variable lev recommender
    trade_state.py        # 1-trade lock + cooldowns
    telegram_notifier.py  # formatted TG messages
    executor.py           # Phase 2 stub
    signal_log.py         # CSV audit
  logs/                   # bot.log, signals.csv
  trade_state.json        # created at runtime
```

---

## Railway Variables tab (MEXC later)

On Railway → your service → **Variables**, you can add anytime:

| Variable | When |
|----------|------|
| `POSITION_SIZE_USD=50` | Optional (already default) |
| `MEXC_API_KEY` | Phase 2 |
| `MEXC_API_SECRET` | Phase 2 |
| `AUTO_TRADE=true` | Phase 2 only, after executor is live |

Full list and deploy steps: **[RAILWAY.md](RAILWAY.md)**

## Phase 2 — Adding MEXC API trading cleanly

1. Create MEXC API key (futures enabled, **no withdraw**).  
2. Railway → **Variables** → set `MEXC_API_KEY`, `MEXC_API_SECRET`.  
3. Implement `bot/executor.py` → `place_signal()` using **exactly `POSITION_SIZE_USD` ($50)** as margin, not balance %.  
4. Set `AUTO_TRADE=true` and redeploy.  
5. Keep Telegram as the alert layer.

---

## Security notes

- Telegram token is intentionally in `config.py` per your request.  
- **Never** put MEXC secrets in the repo — only Railway Variables / local `.env`.  
- `.gitignore` excludes `.env` and state files.

---

## Commands cheat sheet

```powershell
cd C:\Users\Gebruiker\oversold_bounce_bot
.\.venv\Scripts\Activate.ps1

# Paper
$env:DRY_RUN="true"; python main.py

# Live signals
$env:DRY_RUN="false"; python main.py

# More verbose
$env:LOG_LEVEL="DEBUG"; python main.py
```

---

## Disclaimer

Educational / automation tooling only. Crypto futures are high risk. You can lose the entire margin. Not financial advice.
