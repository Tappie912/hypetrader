"""
Perp-Spot Arbitrage Strategy
=============================
Detects and trades the premium/discount between a perpetual contract
and its corresponding spot market on Hyperliquid.

Logic:
  ENTRY  — when |perp_mid - spot_mid| > MIN_SPREAD_BPS:
           Long the cheaper side, short the expensive side.

  EXIT   — when spread compresses to <= CLOSE_SPREAD_BPS,
           or a stop-loss / risk limit is hit.

This is a delta-neutral strategy: long one side, short the other,
so directional market risk is minimised.

Note: Funding rate is an additional P&L source for this strategy —
      when perp trades above spot, shorts on perp earn positive funding.
"""

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

from feed import MarketDataFeed, BookSnapshot
from order_manager import OrderManager, Order, Position, Side, TIF
from risk_manager import RiskManager
from logger import logger, alert
import config


class ArbState(str, Enum):
    FLAT       = "flat"       # no position
    ENTERING   = "entering"   # orders sent, waiting for fills
    OPEN       = "open"       # both legs filled, position live
    CLOSING    = "closing"    # close orders sent
    UNWINDING  = "unwinding"  # failed entry exposure being reduced
    UNHEDGED   = "unhedged"   # startup exposure is incomplete or same-sided


@dataclass
class ArbLeg:
    order: Optional[Order] = None
    close_order: Optional[Order] = None
    close_filled: float = 0.0
    filled: bool = False


@dataclass
class ArbPosition:
    asset: str
    state: ArbState = ArbState.FLAT
    perp_leg: ArbLeg = field(default_factory=ArbLeg)
    spot_leg: ArbLeg = field(default_factory=ArbLeg)
    entry_spread_bps: float = 0.0
    entry_time: float = field(default_factory=time.time)
    realized_pnl: float = 0.0
    last_status_check: float = 0.0

    @property
    def age_secs(self) -> float:
        return time.time() - self.entry_time


class PerpSpotArbStrategy:
    """
    Runs the perp-spot arbitrage loop.

    Usage:
        feed    = MarketDataFeed()
        orders  = OrderManager()
        risk    = RiskManager()
        strat   = PerpSpotArbStrategy(feed, orders, risk)
        await strat.run()
    """

    def __init__(
        self,
        feed: MarketDataFeed,
        orders: OrderManager,
        risk: RiskManager,
        trading_enabled: bool = False,
    ):
        self._feed   = feed
        self._orders = orders
        self._risk   = risk
        self._positions: Dict[str, ArbPosition] = {
            asset: ArbPosition(asset=asset)
            for asset, _ in config.ARB_PAIRS
        }
        self._running = False
        self._trading_enabled = trading_enabled
        self._last_pnl_alert = time.time()
        self._last_book_diagnostic: Dict[str, float] = {}

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info("PerpSpotArbStrategy starting")
        self._running = True

        # Subscribe to required feeds
        for perp_asset, spot_asset in config.ARB_PAIRS:
            await self._feed.subscribe_perp(perp_asset)
            await self._feed.subscribe_spot(spot_asset)

        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Strategy tick error: {e}", exc_info=True)
                await alert(f"Strategy error: {e}", "ERROR")
            await asyncio.sleep(0.1)   # 100ms loop — adjust to your needs

    def stop(self) -> None:
        self._running = False
        logger.info("PerpSpotArbStrategy stopping")

    def set_trading_enabled(self, enabled: bool) -> None:
        self._trading_enabled = enabled

    def adopt_positions(self, positions: List[Position]) -> None:
        """Seed strategy state from live account positions after a restart."""
        by_market_asset = {
            (position.asset, position.market): position
            for position in positions
        }
        for perp_asset, spot_asset in config.ARB_PAIRS:
            perp_position = by_market_asset.get((perp_asset, "perp"))
            spot_position = by_market_asset.get((spot_asset, "spot"))
            if not perp_position and not spot_position:
                continue

            pos = self._positions[perp_asset]
            pos.perp_leg = ArbLeg(
                order=self._adopted_order(perp_asset, "perp", perp_position)
                if perp_position else None,
            )
            pos.spot_leg = ArbLeg(
                order=self._adopted_order(perp_asset, "spot", spot_position)
                if spot_position else None,
            )
            pos.entry_time = time.time()
            pos.last_status_check = 0.0
            if (
                perp_position
                and spot_position
                and perp_position.side != spot_position.side
            ):
                pos.state = ArbState.OPEN
                entry_mid = (perp_position.entry_price + spot_position.entry_price) / 2
                if entry_mid > 0:
                    pos.entry_spread_bps = (
                        (perp_position.entry_price - spot_position.entry_price)
                        / entry_mid
                        * 10_000
                    )
            else:
                pos.state = ArbState.UNHEDGED
                logger.error(
                    f"Adopted incomplete or same-sided {perp_asset} exposure; "
                    "automatic entries are blocked for this pair"
                )
            logger.info(
                f"Adopted {perp_asset} positions: "
                f"perp={perp_position.size if perp_position else 0:g} "
                f"spot={spot_position.size if spot_position else 0:g} "
                f"state={pos.state.value}"
            )

    @staticmethod
    def _adopted_order(
        perp_asset: str,
        market: str,
        position: Position,
    ) -> Order:
        return Order(
            client_id=f"adopted-{perp_asset}-{market}",
            asset=perp_asset,
            market=market,
            side=position.side,
            size=position.size,
            price=position.entry_price,
            filled=position.size,
            status="filled",
        )

    def positions_snapshot(self) -> List[dict]:
        """Return both legs tracked by the strategy for dashboard telemetry."""
        snapshots = []
        for pos in self._positions.values():
            if pos.state == ArbState.FLAT:
                continue
            spot_asset = next(
                (spot for perp, spot in config.ARB_PAIRS if perp == pos.asset),
                pos.asset,
            )

            def leg_snapshot(leg: ArbLeg, market: str, asset: str) -> dict:
                order = leg.order
                close_order = leg.close_order
                return {
                    "asset": asset,
                    "market": market,
                    "side": order.side.name if order else None,
                    "size": (order.filled or order.size) if order else 0.0,
                    "price": order.price if order else 0.0,
                    "status": order.status if order else "missing",
                    "close_status": close_order.status if close_order else "not started",
                }

            snapshots.append({
                "asset": pos.asset,
                "state": pos.state.value,
                "entry_spread_bps": pos.entry_spread_bps,
                "legs": [
                    leg_snapshot(pos.perp_leg, "perp", pos.asset),
                    leg_snapshot(pos.spot_leg, "spot", spot_asset),
                ],
            })
        return snapshots

    async def close_all_positions(self) -> List[str]:
        """Force-close every tracked non-flat arb at current book prices."""
        closed = []
        for pos in self._positions.values():
            if pos.state == ArbState.FLAT:
                continue
            perp_order = pos.perp_leg.order
            spot_order = pos.spot_leg.order
            if not perp_order or not spot_order:
                logger.error(f"Cannot force-close incomplete {pos.asset} position")
                continue

            perp_book = self._feed.get_book(pos.asset, "perp")
            spot_asset = next((spot for perp, spot in config.ARB_PAIRS if perp == pos.asset), pos.asset)
            spot_book = self._feed.get_book(spot_asset, "spot")
            if not perp_book or not spot_book or perp_book.is_stale() or spot_book.is_stale():
                logger.error(f"Cannot force-close {pos.asset}: live books unavailable")
                continue

            perp_side = Side.BUY if perp_order.side == Side.SELL else Side.SELL
            spot_side = Side.BUY if spot_order.side == Side.SELL else Side.SELL
            perp_close, spot_close = await asyncio.gather(
                self._orders.place_market(
                    pos.asset, perp_side, perp_order.filled or perp_order.size,
                    market="perp", reduce_only=config.REDUCE_ONLY_ON_CLOSE,
                    price=self._marketable_price(perp_book, perp_side),
                ),
                self._orders.place_market(
                    pos.asset, spot_side, spot_order.filled or spot_order.size,
                    market="spot", price=self._marketable_price(spot_book, spot_side),
                ),
            )
            pos.perp_leg.close_order = perp_close
            pos.spot_leg.close_order = spot_close
            pos.state = ArbState.CLOSING
            closed.append(pos.asset)
        return closed

    # ── Per-tick logic ────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        # Periodic PnL alert
        if time.time() - self._last_pnl_alert > config.ALERT_PNL_INTERVAL_SECS:
            await self._send_pnl_summary()
            self._last_pnl_alert = time.time()

        for perp_asset, spot_asset in config.ARB_PAIRS:
            pos = self._positions[perp_asset]

            perp_book = self._feed.get_book(perp_asset, "perp")
            spot_book  = self._feed.get_book(spot_asset,  "spot")

            if not perp_book or not spot_book:
                diagnostic_key = f"missing:{perp_asset}"
                if time.time() - self._last_book_diagnostic.get(diagnostic_key, 0) >= 10:
                    missing = []
                    if not perp_book:
                        missing.append(f"perp:{perp_asset}")
                    if not spot_book:
                        missing.append(f"spot:{spot_asset}")
                    logger.debug(f"Book unavailable for {perp_asset}: {', '.join(missing)}")
                    self._last_book_diagnostic[diagnostic_key] = time.time()
                continue
            if perp_book.is_stale() or spot_book.is_stale():
                diagnostic_key = f"stale:{perp_asset}"
                if time.time() - self._last_book_diagnostic.get(diagnostic_key, 0) >= 10:
                    perp_age = time.time() - perp_book.ts
                    spot_age = time.time() - spot_book.ts
                    logger.debug(
                        f"Stale book data for {perp_asset}, skipping | "
                        f"perp={perp_book.asset}/{perp_book.market} mid={perp_book.mid:.8g} "
                        f"age={perp_age:.2f}s | "
                        f"spot={spot_book.asset}/{spot_book.market} mid={spot_book.mid:.8g} "
                        f"age={spot_age:.2f}s"
                    )
                    self._last_book_diagnostic[diagnostic_key] = time.time()
                continue

            if pos.state == ArbState.FLAT:
                spread_bps = self._feed.get_spread_bps(perp_asset, spot_asset)
                if spread_bps is not None:
                    await self._check_entry(pos, perp_book, spot_book, spread_bps)

            elif pos.state == ArbState.ENTERING:
                await self._check_entry_fills(pos)

            elif pos.state == ArbState.OPEN:
                spread_bps = self._exit_spread_bps(pos, perp_book, spot_book)
                if spread_bps is not None:
                    await self._check_exit(pos, spread_bps, perp_book, spot_book)

            elif pos.state == ArbState.CLOSING:
                await self._check_close_fills(pos)

            elif pos.state == ArbState.UNWINDING:
                await self._check_unwind_fills(pos)

    # ── Entry ─────────────────────────────────────────────────────────────────

    async def _check_entry(
        self,
        pos: ArbPosition,
        perp: BookSnapshot,
        spot: BookSnapshot,
        spread_bps: float,
    ) -> None:
        if not self._trading_enabled:
            return
        if abs(spread_bps) < config.MIN_SPREAD_BPS:
            return

        # Determine direction
        if spread_bps > 0:
            # Perp premium: short perp, long spot
            perp_side = Side.SELL
            spot_side = Side.BUY
        else:
            # Perp discount: long perp, short spot
            if not config.ALLOW_SPOT_SELL:
                logger.debug(f"Skipping {pos.asset}: spot shorting is disabled")
                return
            perp_side = Side.BUY
            spot_side = Side.SELL

        perp_price = self._marketable_price(perp, perp_side)
        spot_price = self._marketable_price(spot, spot_side)
        size = self._calc_size(pos.asset, max(perp_price, spot_price))
        perp_liquidity = perp.bid_size if perp_side == Side.SELL else perp.ask_size
        spot_liquidity = spot.ask_size if spot_side == Side.BUY else spot.bid_size
        size = min(size, perp_liquidity, spot_liquidity)
        size_scale = 10 ** config.SIZE_DECIMALS.get(pos.asset, 4)
        size = math.floor(size * size_scale) / size_scale
        if size <= 0:
            return

        # Risk check
        ok, reason = self._risk.check_order(pos.asset, "perp", perp_side, size, perp_price)
        if not ok:
            logger.warning(f"Risk check failed for {pos.asset} perp: {reason}")
            return
        ok, reason = self._risk.check_order(pos.asset, "spot", spot_side, size, spot_price)
        if not ok:
            logger.warning(f"Risk check failed for {pos.asset} spot: {reason}")
            return

        logger.info(
            f"Arb signal: {pos.asset} spread={spread_bps:.1f}bps | "
            f"perp {perp_side.name} @ {perp_price} | spot {spot_side.name} @ {spot_price}"
        )
        await alert(
            f"📊 Arb entry: {pos.asset} | spread={spread_bps:.1f}bps\n"
            f"  Perp {perp_side.name} {size} @ {perp_price}\n"
            f"  Spot {spot_side.name} {size} @ {spot_price}"
        )

        # Place both legs simultaneously
        perp_order, spot_order = await asyncio.gather(
            self._orders.place_limit(
                pos.asset, perp_side, size, perp_price,
                market="perp", tif=TIF.IOC,
            ),
            self._orders.place_limit(
                pos.asset, spot_side, size, spot_price,
                market="spot", tif=TIF.IOC,
            ),
        )

        if not perp_order or not spot_order or perp_order.status == "error" or spot_order.status == "error":
            logger.error(f"Arb entry rejected for {pos.asset}; unwinding any filled leg")
            for order in (perp_order, spot_order):
                if order and order.status == "open":
                    await self._orders.cancel(order)
            await self._unwind_entry_fills(pos, perp_order, spot_order)
            return

        pos.perp_leg = ArbLeg(order=perp_order)
        pos.spot_leg = ArbLeg(order=spot_order)
        pos.entry_spread_bps = spread_bps
        pos.entry_time = time.time()
        pos.last_status_check = 0.0
        pos.state = ArbState.ENTERING

    async def _unwind_entry_fills(
        self,
        pos: ArbPosition,
        perp_order: Optional[Order],
        spot_order: Optional[Order],
    ) -> None:
        """Close confirmed fills when the paired entry leg fails."""
        pos.perp_leg = ArbLeg(order=perp_order)
        pos.spot_leg = ArbLeg(order=spot_order)
        pos.entry_time = time.time()
        pos.last_status_check = 0.0
        pos.state = ArbState.UNWINDING
        await self._check_unwind_fills(pos)
        if pos.state == ArbState.UNWINDING:
            logger.critical(
                f"Failed entry still has exposure for {pos.asset}; retrying unwind"
            )

    async def _check_entry_fills(self, pos: ArbPosition) -> None:
        """Wait for both legs to fill. Cancel and reset if one leg times out."""
        if time.time() - pos.last_status_check >= 0.5:
            await asyncio.gather(
                self._orders.refresh_order(pos.perp_leg.order),
                self._orders.refresh_order(pos.spot_leg.order),
            )
            pos.last_status_check = time.time()

        if any(
            leg.order is None or leg.order.status in ("error", "cancelled")
            for leg in (pos.perp_leg, pos.spot_leg)
        ):
            logger.error(f"Entry leg rejected for {pos.asset}; unwinding confirmed fills")
            await self._unwind_entry_fills(
                pos, pos.perp_leg.order, pos.spot_leg.order,
            )
            return

        perp_filled = pos.perp_leg.order and pos.perp_leg.order.status == "filled"
        spot_filled = pos.spot_leg.order and pos.spot_leg.order.status == "filled"

        pos.perp_leg.filled = perp_filled
        pos.spot_leg.filled = spot_filled

        if perp_filled and spot_filled:
            perp_size = pos.perp_leg.order.filled
            spot_size = pos.spot_leg.order.filled
            tolerance = 10 ** -config.SIZE_DECIMALS.get(pos.asset, 4)
            if abs(perp_size - spot_size) > tolerance:
                logger.error(
                    f"Unequal fills for {pos.asset}: "
                    f"perp={perp_size} spot={spot_size}; unwinding both legs"
                )
                await self._unwind_entry_fills(
                    pos,
                    pos.perp_leg.order,
                    pos.spot_leg.order,
                )
                return
            pos.state = ArbState.OPEN
            logger.info(
                f"Both legs filled for {pos.asset} — position open "
                f"(perp={perp_size}, spot={spot_size})"
            )
            return

        # Timeout: cancel unfilled leg to avoid one-legged risk
        if pos.age_secs > config.ORDER_TIMEOUT_SECS:
            logger.warning(f"Entry timeout for {pos.asset} — cancelling unfilled legs")
            await alert(f"⚠️ Entry timeout for {pos.asset} — cancelling", "WARNING")
            for leg in (pos.perp_leg, pos.spot_leg):
                if leg.order and leg.order.status == "open":
                    await self._orders.cancel(leg.order)
            await self._unwind_entry_fills(
                pos, pos.perp_leg.order, pos.spot_leg.order,
            )

    async def _check_unwind_fills(self, pos: ArbPosition) -> None:
        if time.time() - pos.last_status_check < 0.5:
            return
        pos.last_status_check = time.time()

        spot_asset = next(
            (spot for perp, spot in config.ARB_PAIRS if perp == pos.asset),
            pos.asset,
        )
        tolerance = 10 ** -config.SIZE_DECIMALS.get(pos.asset, 4)
        for leg, market, asset in (
            (pos.perp_leg, "perp", pos.asset),
            (pos.spot_leg, "spot", spot_asset),
        ):
            entry_order = leg.order
            if not entry_order or entry_order.filled <= 0:
                continue

            close_order = leg.close_order
            if close_order:
                await self._orders.refresh_order(close_order)
                if close_order.status in ("pending", "open"):
                    continue
                leg.close_filled += close_order.filled
                leg.close_order = None

            remaining = max(0.0, entry_order.filled - leg.close_filled)
            if remaining <= tolerance:
                continue

            book = self._feed.get_book(asset, market)
            if not book or book.is_stale():
                continue

            close_side = Side.BUY if entry_order.side == Side.SELL else Side.SELL
            leg.close_order = await self._orders.place_market(
                pos.asset,
                close_side,
                remaining,
                market=market,
                reduce_only=market == "perp" and config.REDUCE_ONLY_ON_CLOSE,
                price=self._marketable_price(
                    book, close_side, config.UNWIND_SLIPPAGE_BPS,
                ),
            )

        all_reduced = all(
            not leg.order
            or leg.order.filled - leg.close_filled <= tolerance
            for leg in (pos.perp_leg, pos.spot_leg)
        )
        if all_reduced:
            logger.info(f"Failed entry unwound for {pos.asset}")
            pos.state = ArbState.FLAT
            pos.perp_leg = ArbLeg()
            pos.spot_leg = ArbLeg()

    # ── Exit ──────────────────────────────────────────────────────────────────

    async def _check_exit(
        self,
        pos: ArbPosition,
        spread_bps: float,
        perp_book: BookSnapshot,
        spot_book: BookSnapshot,
    ) -> None:
        perp_entry = pos.perp_leg.order
        if not perp_entry:
            return
        if perp_entry.side == Side.SELL:
            should_close = spread_bps <= config.CLOSE_SPREAD_BPS
        else:
            should_close = spread_bps >= -config.CLOSE_SPREAD_BPS

        if not should_close:
            return

        logger.info(f"Closing arb on {pos.asset}: spread={spread_bps:.1f}bps (entry={pos.entry_spread_bps:.1f}bps)")
        await alert(f"📉 Closing arb: {pos.asset} | spread={spread_bps:.1f}bps compressed")

        # Keep the entry orders as the source of truth for the exposure being
        # closed. Close orders are tracked separately so a retry cannot reverse
        # an earlier close attempt.
        perp_order = pos.perp_leg.order
        spot_order = pos.spot_leg.order

        if not perp_order or not spot_order:
            pos.state = ArbState.FLAT
            return

        # Close each leg in opposite direction, reduce-only. A previously
        # filled leg must not be submitted again after the other leg times out.
        close_perp_side = Side.BUY  if perp_order.side == Side.SELL else Side.SELL
        close_spot_side = Side.BUY  if spot_order.side == Side.SELL else Side.SELL
        close_perp_price = self._marketable_price(perp_book, close_perp_side)
        close_spot_price = self._marketable_price(spot_book, close_spot_side)

        async def close_if_needed(
            leg: ArbLeg,
            side: Side,
            size: float,
            market: str,
            price: float,
        ) -> Optional[Order]:
            if leg.close_order and leg.close_order.status == "filled":
                return leg.close_order
            return await self._orders.place_market(
                pos.asset,
                side,
                size,
                market=market,
                reduce_only=market == "perp" and config.REDUCE_ONLY_ON_CLOSE,
                price=price,
            )

        close_perp, close_spot = await asyncio.gather(
            close_if_needed(pos.perp_leg, close_perp_side, perp_order.filled or perp_order.size, "perp", close_perp_price),
            close_if_needed(pos.spot_leg, close_spot_side, spot_order.filled or spot_order.size, "spot", close_spot_price),
        )

        if not close_perp or close_perp.status in ("error", "cancelled"):
            logger.error(f"Perp close failed for {pos.asset}: {close_perp.status if close_perp else 'no order'}")
        if not close_spot or close_spot.status in ("error", "cancelled"):
            logger.error(f"Spot close failed for {pos.asset}: {close_spot.status if close_spot else 'no order'}")

        pos.perp_leg.close_order = close_perp
        pos.spot_leg.close_order = close_spot
        pos.entry_time = time.time()
        pos.last_status_check = 0.0
        pos.state = ArbState.CLOSING

    async def _check_close_fills(self, pos: ArbPosition) -> None:
        perp_order = pos.perp_leg.close_order
        spot_order = pos.spot_leg.close_order
        if not perp_order or not spot_order:
            logger.error(
                f"Close order missing for {pos.asset}; keeping position open for retry"
            )
            pos.state = ArbState.OPEN
            pos.last_status_check = 0.0
            return

        if time.time() - pos.last_status_check >= 0.5:
            await asyncio.gather(
                self._orders.refresh_order(perp_order),
                self._orders.refresh_order(spot_order),
            )
            pos.last_status_check = time.time()

        perp_done = perp_order.status == "filled"
        spot_done = spot_order.status == "filled"

        if perp_done and spot_done:
            logger.info(f"Position closed for {pos.asset}")
            await alert(f"✅ Position closed: {pos.asset}")
            pos.state = ArbState.FLAT
            pos.perp_leg = ArbLeg()
            pos.spot_leg = ArbLeg()

        elif pos.age_secs > config.ORDER_TIMEOUT_SECS:
            logger.warning(f"Close timeout for {pos.asset} — retrying with market orders")
            pos.state = ArbState.OPEN

    @staticmethod
    def _exit_spread_bps(
        pos: ArbPosition,
        perp: BookSnapshot,
        spot: BookSnapshot,
    ) -> Optional[float]:
        perp_entry = pos.perp_leg.order
        if not perp_entry:
            return None
        if perp_entry.side == Side.SELL:
            perp_price, spot_price = perp.ask_price, spot.bid_price
        else:
            perp_price, spot_price = perp.bid_price, spot.ask_price
        mid = (perp_price + spot_price) / 2
        if perp_price <= 0 or spot_price <= 0 or mid <= 0:
            return None
        return (perp_price - spot_price) / mid * 10_000

    @staticmethod
    def _marketable_price(
        book: BookSnapshot,
        side: Side,
        slippage_bps: Optional[float] = None,
    ) -> float:
        """Create a valid IOC limit price that crosses the current book."""
        reference = book.ask_price if side == Side.BUY else book.bid_price
        tolerance = (
            config.SLIPPAGE_TOLERANCE_BPS
            if slippage_bps is None
            else slippage_bps
        )
        slippage = tolerance / 10_000
        price = reference * (1 + slippage if side == Side.BUY else 1 - slippage)
        if price <= 0:
            return 0.0
        digits = max(0, 5 - int(math.floor(math.log10(price))) - 1)
        return round(price, digits)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _calc_size(self, asset: str, price: float) -> float:
        if price <= 0:
            return 0.0
        max_by_order = config.MAX_ORDER_USD / price
        max_by_pos   = config.MAX_POSITION_USD / price
        size = min(max_by_order, max_by_pos)
        decimals = config.SIZE_DECIMALS.get(asset, 4)
        scale = 10 ** decimals
        return math.floor(size * scale) / scale

    async def _send_pnl_summary(self) -> None:
        lines = ["📊 *PnL Summary*"]
        for asset, pos in self._positions.items():
            lines.append(f"  {asset}: state={pos.state.value} | entry_spread={pos.entry_spread_bps:.1f}bps")
        lines.append(self._risk.summary())
        await alert("\n".join(lines))
