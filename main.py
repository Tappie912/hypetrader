"""
Hyperliquid Arbitrage Bot — Entry Point
========================================
Wires together:
  - MarketDataFeed  (WebSocket)
  - OrderManager    (REST + signing)
  - RiskManager     (pre-trade checks + circuit breakers)
  - PerpSpotArbStrategy

Run with:
    python main.py

Stop with Ctrl-C — open positions are NOT automatically closed on exit.
Add --close-on-exit flag to close all positions before shutting down.
"""

import asyncio
import argparse
import signal
import sys

from bot_runtime import BotRuntime
from dashboard import Dashboard
from logger import logger, alert
import config


async def main(close_on_exit: bool = False) -> None:
    logger.info("=" * 60)
    logger.info("  Hyperliquid Arb Bot starting")
    logger.info(f"  Account : {config.ACCOUNT_ADDRESS}")
    logger.info(f"  API URL : {config.HYPERLIQUID_API_URL}")
    logger.info(f"  Pairs   : {config.ARB_PAIRS}")
    logger.info("=" * 60)

    await alert("🤖 Hyperliquid Arb Bot started")

    runtime = BotRuntime()
    dashboard = Dashboard(runtime)
    dashboard_runner = await dashboard.run()
    await runtime.start()
    logger.info("Dashboard available at http://127.0.0.1:8080")

    # Graceful shutdown on SIGINT / SIGTERM
    stop_event = asyncio.Event()

    def _shutdown(sig):
        logger.info(f"Received {sig.name} — shutting down…")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        if sys.platform == "win32":
            signal.signal(
                s,
                lambda _signum, _frame, sig=s: loop.call_soon_threadsafe(_shutdown, sig),
            )
        else:
            loop.add_signal_handler(s, _shutdown, s)

    await stop_event.wait()

    logger.info("Stopping bot runtime…")

    if close_on_exit:
        logger.info("Closing all open positions before exit…")
        await alert("🛑 Bot shutting down — closing all positions")
        cancelled = await runtime.orders.cancel_all()
        logger.info(f"Cancelled {cancelled} open orders")

    await runtime.shutdown()
    await dashboard_runner.cleanup()
    await alert("🛑 Hyperliquid Arb Bot stopped")
    logger.info("Shutdown complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hyperliquid Arb Bot")
    parser.add_argument(
        "--close-on-exit",
        action="store_true",
        help="Cancel all open orders when the bot shuts down",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(close_on_exit=args.close_on_exit))
    except KeyboardInterrupt:
        pass
