"""
Hyperliquid Arbitrage Framework — Configuration
================================================
Edit this file before running. Never commit private keys.
"""

import os
from dotenv import load_dotenv
load_dotenv()

# ── Node / API endpoints ──────────────────────────────────────────────────────
# Point these at your own node if you have one running, otherwise use public.
HYPERLIQUID_API_URL   = "https://api.hyperliquid.xyz"   # REST (HyperCore)
HYPERLIQUID_WS_URL    = "wss://api.hyperliquid.xyz/ws"  # WebSocket
HYPERLIQUID_EVM_RPC   = "https://rpc.hyperliquid.xyz/evm"  # HyperEVM JSON-RPC

# To use your own node instead, replace with e.g.:
#   HYPERLIQUID_API_URL = "http://<your-node-ip>:3000"
#   HYPERLIQUID_WS_URL  = "ws://<your-node-ip>:3000/ws"

# ── Wallet ────────────────────────────────────────────────────────────────────
# Use an API sub-wallet (generate on app.hyperliquid.xyz/API), not your main wallet.
WALLET_ADDRESS = os.getenv("HYPE_WALLET_ADDRESS") or os.getenv("HYPE_WALLET")
PRIVATE_KEY = os.getenv("API_KEY")  # keep secret — use env vars in prod

if not WALLET_ADDRESS or not PRIVATE_KEY:
    raise RuntimeError("HYPE_WALLET and API_KEY must be set in .env")

# ── Risk limits ───────────────────────────────────────────────────────────────
MAX_POSITION_USD        = 1000        # max notional per position in USD
MAX_TOTAL_EXPOSURE_USD  = 5000        # max total open notional across all positions
MAX_ORDER_USD           = 500         # max single order size
MAX_DAILY_LOSS_USD      = 100         # halt trading if daily PnL drops below this
MAX_DRAWDOWN_PCT        = 0.1          # 5% max drawdown before halt

# ── Arbitrage parameters ──────────────────────────────────────────────────────
MIN_SPREAD_BPS          = 5             # minimum spread in basis points to enter
CLOSE_SPREAD_BPS        = 1             # spread at which to close the arb
SLIPPAGE_TOLERANCE_BPS  = 1            # max acceptable slippage
ALLOW_SPOT_SELL         = False       # spot sells require the asset in the account

# Markets to watch — (perp_asset, spot_asset) pairs
# These are traded as a pair: long cheap side, short expensive side
ARB_PAIRS = [
    ("ETH", "ETH"),   # ETH perp vs ETH spot
    ("BTC", "BTC"),   # BTC perp vs BTC spot
    ("SOL", "SOL"),   # SOL perp vs SOL spot
    ("HYPE", "HYPE"), # HYPE perp vs HYPE spot
]

# Hyperliquid spot market identifiers for the wrapped assets above.
SPOT_COINS = {
    "ETH": "@151",
    "BTC": "@142",
    "SOL": "@156",
    "HYPE": "@107",
}

# Spot order asset indices are 10000 plus the spotMeta universe index.
SPOT_ASSET_INDICES = {
    "ETH": 10_151,
    "BTC": 10_142,
    "SOL": 10_156,
    "HYPE": 10_107,
}

# Shared perp/spot size precision uses the stricter precision of each pair.
SIZE_DECIMALS = {
    "ETH": 4,
    "BTC": 5,
    "SOL": 2,
    "HYPE": 2,
}

# ── Order management ──────────────────────────────────────────────────────────
ORDER_TIMEOUT_SECS      = 30            # cancel unfilled limit orders after this
REDUCE_ONLY_ON_CLOSE    = True
DEFAULT_LEVERAGE        = 1             # keep at 1x for pure arb (delta-neutral)

# ── Logging & alerting ────────────────────────────────────────────────────────
LOG_LEVEL               = "INFO"        # DEBUG | INFO | WARNING | ERROR
LOG_FILE                = "logs/hl_arb.log"
LOG_ROTATE_MB           = 50
LOG_BACKUP_COUNT        = 5

# Slack webhook (optional — leave empty to disable)
SLACK_WEBHOOK_URL       = ""

# Telegram bot (optional — leave both empty to disable)
TELEGRAM_BOT_TOKEN      = ""
TELEGRAM_CHAT_ID        = ""

# Alert thresholds
ALERT_ON_FILL           = True
ALERT_ON_ERROR          = True
ALERT_ON_RISK_BREACH    = True
ALERT_PNL_INTERVAL_SECS = 3600         # send PnL summary every N seconds
