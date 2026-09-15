# Hyperliquid Arbitrage Framework

A production-grade Python framework for perp-spot arbitrage trading on Hyperliquid.

---

## Architecture

```
hl_arb/
├── main.py                    # Entry point — wires everything together
├── config.py                  # All tunable parameters (edit this first)
├── requirements.txt
├── core/
│   ├── feed.py                # WebSocket market data feed
│   ├── order_manager.py       # Order placement, cancellation, modification
│   └── risk_manager.py        # Pre-trade risk checks + circuit breakers
├── strategies/
│   └── perp_spot_arb.py       # Perp-spot arbitrage strategy
└── utils/
    └── logger.py              # Rotating file logger + Slack/Telegram alerts
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

Edit `config.py`:

```python
WALLET_ADDRESS = "0xYOUR_WALLET_ADDRESS"
PRIVATE_KEY    = "0xYOUR_PRIVATE_KEY"
```

**Security tip:** use environment variables instead of hardcoding keys:

```bash
export HL_WALLET_ADDRESS="0x..."
export HL_PRIVATE_KEY="0x..."
```

Then in `config.py`:
```python
import os
WALLET_ADDRESS = os.environ["HL_WALLET_ADDRESS"]
PRIVATE_KEY    = os.environ["HL_PRIVATE_KEY"]
```

Generate a dedicated API sub-wallet on https://app.hyperliquid.xyz/API — never use your main wallet private key.

### 3. Configure your node endpoint (optional)

If you have a local Hyperliquid node running, update `config.py`:

```python
HYPERLIQUID_API_URL = "http://<your-node-ip>:3000"
HYPERLIQUID_WS_URL  = "ws://<your-node-ip>:3000/ws"
```

Otherwise, the public endpoints are used.

### 4. Run on testnet first

In `config.py`, temporarily change the URLs to testnet:

```python
HYPERLIQUID_API_URL = "https://api.hyperliquid-testnet.xyz"
HYPERLIQUID_WS_URL  = "wss://api.hyperliquid-testnet.xyz/ws"
```

Then run:

```bash
python main.py
```

### 5. Run on mainnet

Restore mainnet URLs and run:

```bash
python main.py

# Or with graceful position close on Ctrl-C:
python main.py --close-on-exit
```

---

## Strategy: Perp-Spot Arbitrage

The bot trades the spread between a perpetual contract and its spot equivalent.

**Entry signal:**
- If `perp_mid > spot_mid` by more than `MIN_SPREAD_BPS`: short perp, long spot
- If `perp_mid < spot_mid` by more than `MIN_SPREAD_BPS`: long perp, short spot

**Exit signal:**
- Spread compresses to <= `CLOSE_SPREAD_BPS`

**Additional P&L source:**
- When perp trades above spot, shorts on perp earn positive funding (paid every hour on Hyperliquid)

**Risk controls:**
- Both legs placed simultaneously to minimise leg risk
- Entry timeout cancels unfilled legs after `ORDER_TIMEOUT_SECS`
- Daily loss limit halts trading
- Max drawdown circuit breaker halts trading
- Per-position and total exposure caps

---

## Key Parameters (config.py)

| Parameter | Default | Description |
|---|---|---|
| `MIN_SPREAD_BPS` | 5 | Minimum spread to enter (basis points) |
| `CLOSE_SPREAD_BPS` | 1 | Spread to exit at (basis points) |
| `MAX_ORDER_USD` | $2,000 | Max single order notional |
| `MAX_POSITION_USD` | $10,000 | Max notional per position |
| `MAX_TOTAL_EXPOSURE_USD` | $50,000 | Max total exposure |
| `MAX_DAILY_LOSS_USD` | $1,000 | Daily loss circuit breaker |
| `MAX_DRAWDOWN_PCT` | 5% | Drawdown circuit breaker |
| `ORDER_TIMEOUT_SECS` | 30 | Cancel unfilled orders after this |

---

## Alerts

Configure Slack and/or Telegram in `config.py`:

```python
SLACK_WEBHOOK_URL  = "https://hooks.slack.com/services/..."
TELEGRAM_BOT_TOKEN = "123456:ABC..."
TELEGRAM_CHAT_ID   = "-100..."
```

You'll receive alerts on:
- Order fills
- Arb entry/exit signals
- Risk limit breaches
- Trading halts
- Hourly PnL summaries

---

## Extending with New Strategies

1. Create `strategies/my_strategy.py`
2. Import `MarketDataFeed`, `OrderManager`, `RiskManager`
3. Implement an `async def run(self)` loop
4. Register it in `main.py` alongside (or instead of) `PerpSpotArbStrategy`

---

## Warnings

- **This is not financial advice.** Arbitrage strategies carry real risk including execution risk, leg risk, and smart contract risk.
- **Test on testnet first.** Always.
- **Never use your main wallet private key.** Use a Hyperliquid API sub-wallet.
- **Monitor positions actively.** This bot does not guarantee safe exit under all market conditions.
