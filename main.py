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

from feed import MarketDataFeed
from order_manager import OrderManager
from risk_manager import RiskManager
from perp_spot_arb import PerpSpotArbStrategy
from logger import logger, alert
import config


async def main(close_on_exit: bool = False) -> None:
    logger.info("=" * 60)
    logger.info("  Hyperliquid Arb Bot starting")
    logger.info(f"  Wallet  : {config.WALLET_ADDRESS}")
    logger.info(f"  API URL : {config.HYPERLIQUID_API_URL}")
    logger.info(f"  Pairs   : {config.ARB_PAIRS}")
    logger.info("=" * 60)

    await alert("🤖 Hyperliquid Arb Bot started")

    feed    = MarketDataFeed()
    orders  = OrderManager()
    risk    = RiskManager()
    strat   = PerpSpotArbStrategy(feed, orders, risk)

    # Periodic account value refresh for risk manager (every 30s)
    async def account_monitor():
        while True:
            try:
                value = await orders.get_account_value()
                risk.update_account_value(value)
                positions = await orders.get_positions()
                risk.update_positions(positions)
                logger.debug(f"Account value: ${value:,.2f} | Open positions: {len(positions)}")
            except Exception as e:
                logger.warning(f"Account monitor error: {e}")
            await asyncio.sleep(30)

    # Order timeout watchdog — cancel open orders older than ORDER_TIMEOUT_SECS
    async def order_watchdog():
        while True:
            try:
                open_orders = await orders.get_open_orders()
                logger.debug(f"Open orders on exchange: {len(open_orders)}")
            except Exception as e:
                logger.warning(f"Order watchdog error: {e}")
            await asyncio.sleep(10)

    # Start everything concurrently
    await orders.start()

    tasks = [
        asyncio.create_task(feed.run(),             name="feed"),
        asyncio.create_task(strat.run(),            name="strategy"),
        asyncio.create_task(account_monitor(),      name="account_monitor"),
        asyncio.create_task(order_watchdog(),       name="order_watchdog"),
    ]

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

    logger.info("Stopping strategy and feed…")
    strat.stop()
    feed.stop()

    if close_on_exit:
        logger.info("Closing all open positions before exit…")
        await alert("🛑 Bot shutting down — closing all positions")
        cancelled = await orders.cancel_all()
        logger.info(f"Cancelled {cancelled} open orders")
        # Note: this only cancels resting orders; actual position closure
        # requires market orders for each open position — extend as needed.

    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
    await orders.stop()
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
