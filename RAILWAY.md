# Deploy on Railway (GitHub)

Phase 1 needs **no MEXC keys**. Public market data + Telegram is enough.  
Add MEXC keys later in the **Variables** tab when you enable auto-trade.

---

## 1. Push this folder to GitHub

From the project root:

```powershell
cd C:\Users\Gebruiker\oversold_bounce_bot
git init
git add .
git commit -m "Oversold bounce signal bot — Railway ready"
# create a repo on GitHub, then:
git remote add origin https://github.com/YOUR_USER/oversold_bounce_bot.git
git branch -M main
git push -u origin main
```

Do **not** commit a filled `.env` with MEXC secrets (`.gitignore` already blocks `.env`).  
Telegram token is in `config.py` by design.

---

## 2. New Railway project

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Select `oversold_bounce_bot`
3. Railway detects `Dockerfile` + `railway.toml`
4. Deploy

Health check hits `/` on `PORT` (built-in). Logs should show:

```text
Health server on 0.0.0.0:XXXX (Railway)
Oversold Bounce Signal Bot starting
FIXED margin=$50 USDT per trade | max_open=1
```

Telegram should get: *Oversold Bounce scanner online…*

---

## 3. Railway → Variables tab

Open your service → **Variables** → add what you need.

### Required for Phase 1?
**None.** Defaults in code are enough (TG hardcoded, $50 fixed, signal-only).

### Optional now (tuning)

| Variable | Example | Meaning |
|----------|---------|---------|
| `POSITION_SIZE_USD` | `50` | **Fixed** USDT margin per trade (not % of balance) |
| `MAX_OPEN_TRADES` | `1` | Keep at 1 |
| `SCAN_INTERVAL_SEC` | `180` | Seconds between full scans |
| `MIN_CONFIDENCE` | `68` | Raise to 75 if too many signals |
| `RSI_OVERSOLD` | `32` | Lower = stricter (fewer signals) |
| `LEVERAGE_MIN` | `3` | Floor for recommended lev |
| `LEVERAGE_MAX` | `12` | Cap for recommended lev |
| `LOG_LEVEL` | `INFO` | or `DEBUG` |
| `DRY_RUN` | `false` | `true` = no Telegram sends |

### Phase 2 later (MEXC auto-trade) — add when ready

| Variable | Example | Meaning |
|----------|---------|---------|
| `MEXC_API_KEY` | `your_key` | Futures trade key (no withdraw) |
| `MEXC_API_SECRET` | `your_secret` | Secret |
| `AUTO_TRADE` | `false` | Set `true` only after `executor.py` is implemented |

Until `AUTO_TRADE=true` **and** executor is coded, keys are ignored and the bot stays **signal-only**.

### Optional TG override (usually leave unset)

| Variable | Default in code |
|----------|-----------------|
| `TELEGRAM_BOT_TOKEN` | already in `config.py` |
| `TELEGRAM_CHAT_ID` | `-1004326901305` |

---

## 4. Position size rule (confirmed)

- Every signal / future order uses **exactly `POSITION_SIZE_USD` USDT margin**
- Default **$50 per trade**
- **Not** a percentage of account equity
- **Only 1 trade** at a time (lock in `trade_state.json`)

To trade $50 forever: leave `POSITION_SIZE_USD` unset or set `50`.  
To change later: Variables → `POSITION_SIZE_USD=75` → redeploy/restart.

---

## 5. After deploy checklist

- [ ] Deploy status: **Active**
- [ ] Logs: health server + first scan
- [ ] Telegram online message received
- [ ] Wait for first real signal (can take hours/days — selective by design)
- [ ] When ready for auto-trade: add MEXC vars, implement executor, set `AUTO_TRADE=true`

---

## 6. Common issues

| Problem | Fix |
|---------|-----|
| Crash loop / no health | Ensure `Dockerfile` CMD is `python -u main.py` and health path is `/` |
| No Telegram | Check bot is in the group; token still valid |
| Redeploy clears lock | Normal on Railway disk — Phase 1 only; OK or add volume later |
| Want more signals | Lower `MIN_CONFIDENCE` carefully (not recommended early) |
| Want fewer signals | `MIN_CONFIDENCE=75` or `RSI_OVERSOLD=28` |

---

## 7. Local vs Railway

| | Local | Railway |
|--|--------|---------|
| Run | `python main.py` | auto from Docker |
| Secrets | optional `.env` | **Variables** tab |
| PORT | 8080 default | Railway injects |
| TG | hardcoded | same |
