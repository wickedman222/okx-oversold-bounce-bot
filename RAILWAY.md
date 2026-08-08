# Deploy on Railway — Bybit

## Variables (Raw Editor)

```env
BYBIT_API_KEY=your_key
BYBIT_API_SECRET=your_secret
AUTO_TRADE=true
POSITION_SIZE_USD=50
SCAN_INTERVAL_SEC=600
IN_TRADE_POLL_SEC=120
```

**Remove old MEXC keys** if still present (or leave them; bot prefers `BYBIT_*`).

### Bybit API key settings
- Permissions: **Contract / Unified trading** (read + trade)
- **Withdraw: OFF**
- IP whitelist: optional (Railway IPs change — often leave open)

### Funding
- Fund **USDT** on Bybit **Unified / Derivatives** wallet
- Need at least ~$50–60 free for one $50 margin trade

## After deploy
Logs should show:
```text
Exchange=Bybit linear
AUTO_TRADE ON — live BYBIT orders | margin=$50 isolated
```

Telegram: `Exchange: BYBIT USDT linear`

## Phase 1 signal-only
Omit keys or `AUTO_TRADE=false` — still scans Bybit public markets and sends TG.
