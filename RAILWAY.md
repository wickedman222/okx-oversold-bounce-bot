# OKX Oversold Bounce Bot — Railway

## Variables (click the **service** → **Variables** → Raw Editor)

```env
OKX_API_KEY=your_key
OKX_API_SECRET=your_secret
OKX_API_PASSWORD=your_api_passphrase
AUTO_TRADE=true
POSITION_SIZE_USD=50
SCAN_INTERVAL_SEC=600
IN_TRADE_POLL_SEC=120
```

**Important:** OKX needs **3** secrets — key, secret, **and passphrase** (`OKX_API_PASSWORD`).

Remove old `BYBIT_*` / `MEXC_*` variables.

### OKX API key
- Trade / read futures (swap)
- **Withdraw OFF**
- Passphrase = the one you set when creating the key (not your login password)

### After deploy
```text
OKX Oversold Bounce Bot starting
Exchange=OKX swap
AUTO_TRADE ON — live OKX orders
```

Telegram: `OKX Oversold Bounce Bot online`
