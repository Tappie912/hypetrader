"""In-process lifecycle and telemetry for the trading bot."""

import asyncio
from typing import Optional

from feed import MarketDataFeed
from order_manager import OrderManager
from perp_spot_arb import PerpSpotArbStrategy
from risk_manager import RiskManager
from logger import logger


class BotRuntime:
    def __init__(self):
        self.orders = OrderManager()
        self.feed: Optional[MarketDataFeed] = None
        self.risk: Optional[RiskManager] = None
        self.strategy: Optional[PerpSpotArbStrategy] = None
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._trading_enabled = False
        self._orders_started = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def trading_enabled(self) -> bool:
        return self._trading_enabled

    async def start(self) -> None:
        if self._running:
            return
        if not self._orders_started:
            await self.orders.start()
            self._orders_started = True

        self.feed = MarketDataFeed()
        self.risk = RiskManager()
        self.strategy = PerpSpotArbStrategy(
            self.feed, self.orders, self.risk, trading_enabled=self._trading_enabled
        )
        self._running = True
        self._tasks = [
            asyncio.create_task(self.feed.run(), name="feed"),
            asyncio.create_task(self.strategy.run(), name="strategy"),
            asyncio.create_task(self._account_monitor(), name="account_monitor"),
            asyncio.create_task(self._order_watchdog(), name="order_watchdog"),
        ]
        logger.info("Bot runtime started")

    def set_trading_enabled(self, enabled: bool) -> None:
        self._trading_enabled = enabled
        if self.strategy:
            self.strategy.set_trading_enabled(enabled)
        logger.info("Trading %s", "enabled" if enabled else "disabled")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._trading_enabled = False
        if self.strategy:
            self.strategy.stop()
        if self.feed:
            self.feed.stop()
        await self._cancel_tasks()
        logger.info("Bot runtime stopped")

    async def close_positions(self) -> list[str]:
        if not self.strategy:
            return []
        return await self.strategy.close_all_positions()

    async def shutdown(self) -> None:
        await self.stop()
        if self._orders_started:
            await self.orders.stop()
            self._orders_started = False

    async def account_snapshot(self) -> dict:
        value = await self.orders.get_account_value(self._spot_prices())
        positions = await self.orders.get_positions()
        if self.risk:
            self.risk.update_account_value(value)
            self.risk.update_positions(positions)
        return {
            "value": value,
            "positions": [
                {
                    "asset": p.asset,
                    "market": p.market,
                    "side": p.side.name,
                    "size": p.size,
                    "entry_price": p.entry_price,
                    "unrealized_pnl": p.unrealized_pnl,
                    "leverage": p.leverage,
                }
                for p in positions
            ],
            "strategy_positions": self.strategy.positions_snapshot() if self.strategy else [],
            "risk": self.risk.state.__dict__ if self.risk else {},
        }

    async def _account_monitor(self) -> None:
        while self._running:
            try:
                value = await self.orders.get_account_value(self._spot_prices())
                positions = await self.orders.get_positions()
                if self.risk:
                    self.risk.update_account_value(value)
                    self.risk.update_positions(positions)
            except Exception as exc:
                logger.warning(f"Account monitor error: {exc}")
            await asyncio.sleep(30)

    def _spot_prices(self) -> dict[str, float]:
        if not self.feed:
            return {}
        return {
            asset: book.mid
            for (asset, market), book in self.feed._books.items()
            if market == "spot" and book.mid > 0
        }

    async def _order_watchdog(self) -> None:
        while self._running:
            try:
                await self.orders.get_open_orders()
            except Exception as exc:
                logger.warning(f"Order watchdog error: {exc}")
            await asyncio.sleep(10)

    async def _cancel_tasks(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
