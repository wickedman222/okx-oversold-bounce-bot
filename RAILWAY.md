# OKX Oversold Bounce Bot — Railway

## Variables (click the **service** → **Variables** → Raw Editor)

```env
OKX_API_KEY=your_key
OKX_API_SECRET=your_secret
OKX_API_PASSWORD=your_api_passphrase
OKX_HOSTNAME=eea.okx.com
AUTO_TRADE=true
POSITION_SIZE_USD=80
LEVERAGE_MAX=10
SCAN_INTERVAL_SEC=300
IN_TRADE_POLL_SEC=120
```

**Important:** OKX needs **3** secrets — key, secret, **and passphrase** (`OKX_API_PASSWORD`).  
**EEA/NL:** use host **`eea.okx.com`** (default). Error `50119 API key doesn't exist` = wrong region host.  
**Leverage:** hard-capped at **10x** (matches OKX perp activation).

Remove old `BYBIT_*` / `MEXC_*` variables.

### OKX API key
- **Trade** + **Read** (Trade is required — Read alone → error **50124**)
- Futures / swap / perpetual must be allowed if the UI lists them separately
- **Withdraw OFF**
- Passphrase = set when creating the key
- Recreate the key **after** you activated perps (old keys may lack market access)

### OKX Europe = X-Perps (not global USDT-SWAP)
- EEA accounts **cannot** trade global `BTC-USDT-SWAP` via API → error **50124**
- Bot uses **X-Perps**: e.g. `BTC-USD_UM_XPERP-…` / `BTC/USD:USD-…`
- Margin in **USDC** (USD-margined linear) — correct for EU
- You do **not** need USDT

### Funding (USDC is OK)
- On OKX multi-currency / EU accounts, **USDC is valid perp margin**
- You do **not** need to convert to USDT
- Keep USDC in **Trading / unified** with ≥ ~$50–60 free
- Bot still trades linear perps; collateral can be **USDC**

### After deploy
```text
OKX Oversold Bounce Bot starting
Exchange=OKX swap
AUTO_TRADE ON — live OKX orders
Margin free: USDC=... total≈$...
```

Telegram: `OKX Oversold Bounce Bot online` + balance including USDC
