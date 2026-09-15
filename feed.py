"""
WebSocket Market Data Feed
==========================
Subscribes to Hyperliquid's WebSocket API and maintains a local
best-bid/ask (BBA) cache for both perp and spot markets.

Subscriptions used:
  - l2Book   : full order book (we extract BBA)
  - trades   : recent trades (for signal use)
  - allMids  : mid-price snapshot for all assets
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple
import websockets

from logger import logger, alert
import config


@dataclass
class BookSnapshot:
    asset: str
    market: str                     # "perp" | "spot"
    bid_price: float = 0.0
    bid_size: float  = 0.0
    ask_price: float = 0.0
    ask_size: float  = 0.0
    mid: float       = 0.0
    ts: float        = field(default_factory=time.time)

    @property
    def spread_bps(self) -> float:
        if self.bid_price <= 0:
            return 0.0
        return (self.ask_price - self.bid_price) / self.mid * 10_000

    def is_stale(self, max_age_secs: float = 5.0) -> bool:
        return (time.time() - self.ts) > max_age_secs


class MarketDataFeed:
    """
    Manages a single WebSocket connection to Hyperliquid.
    Maintains book snapshots and calls registered callbacks on updates.
    """

    def __init__(self, ws_url: str = config.HYPERLIQUID_WS_URL):
        self._url = ws_url
        self._books: Dict[Tuple[str, str], BookSnapshot] = {}  # (asset, market) -> snapshot
        self._callbacks: list[Callable[[BookSnapshot], None]] = []
        self._ws = None
        self._running = False
        self._subscriptions: list[dict] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def on_book_update(self, callback: Callable[[BookSnapshot], None]) -> None:
        """Register a callback invoked on every book update."""
        self._callbacks.append(callback)

    def get_book(self, asset: str, market: str = "perp") -> Optional[BookSnapshot]:
        return self._books.get((asset, market))

    def get_spread_bps(self, asset: str) -> Optional[float]:
        """Return basis-point spread between perp ask and spot bid (or None)."""
        perp = self._books.get((asset, "perp"))
        spot = self._books.get((asset, "spot"))
        if not perp or not spot:
            return None
        if perp.is_stale() or spot.is_stale():
            return None
        # Perp premium: positive means perp trades above spot
        mid_avg = (perp.mid + spot.mid) / 2
        if mid_avg <= 0:
            return None
        return (perp.mid - spot.mid) / mid_avg * 10_000

    async def subscribe_perp(self, asset: str) -> None:
        sub = {"method": "subscribe", "subscription": {"type": "l2Book", "coin": asset}}
        self._subscriptions.append((sub, asset, "perp"))
        if self._ws:
            await self._ws.send(json.dumps(sub))
            logger.debug(f"Subscribed perp l2Book: {asset}")

    async def subscribe_spot(self, asset: str) -> None:
        coin = config.SPOT_COINS.get(asset, asset)
        sub = {"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}
        self._subscriptions.append((sub, asset, "spot"))
        if self._ws:
            await self._ws.send(json.dumps(sub))
            logger.debug(f"Subscribed spot l2Book: {asset}")

    async def run(self) -> None:
        """Connect and maintain the WebSocket feed with auto-reconnect."""
        self._running = True
        backoff = 1
        while self._running:
            try:
                logger.info(f"Connecting to WebSocket: {self._url}")
                async with websockets.connect(
                    self._url,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=10 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    backoff = 1
                    logger.info("WebSocket connected")

                    # Resubscribe (handles reconnects)
                    for sub, *_ in self._subscriptions:
                        await ws.send(json.dumps(sub))

                    async for raw in ws:
                        await self._handle_message(raw)

            except websockets.ConnectionClosed as e:
                logger.warning(f"WebSocket closed: {e}. Reconnecting in {backoff}s…")
            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting in {backoff}s…")
                await alert(f"WebSocket error: {e}", "ERROR")
            finally:
                self._ws = None

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def stop(self) -> None:
        self._running = False

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        channel = msg.get("channel")
        data    = msg.get("data", {})

        if channel == "l2Book":
            await self._process_l2(data)

    async def _process_l2(self, data: dict) -> None:
        coin   = data.get("coin", "")
        levels = data.get("levels", [[], []])

        spot_assets = {coin: asset for asset, coin in config.SPOT_COINS.items()}
        is_spot = coin in spot_assets or "/" in coin
        asset = spot_assets.get(coin, coin.split("/", 1)[0] if is_spot else coin)
        market  = "spot" if is_spot else "perp"

        bids = levels[0]  # list of {"px": str, "sz": str, "n": int}
        asks = levels[1]

        if not bids or not asks:
            return

        best_bid = bids[0]
        best_ask = asks[0]

        bp = float(best_bid["px"])
        bs = float(best_bid["sz"])
        ap = float(best_ask["px"])
        as_ = float(best_ask["sz"])
        mid = (bp + ap) / 2

        snap = BookSnapshot(
            asset=asset, market=market,
            bid_price=bp, bid_size=bs,
            ask_price=ap, ask_size=as_,
            mid=mid, ts=time.time(),
        )
        self._books[(asset, market)] = snap

        for cb in self._callbacks:
            try:
                cb(snap)
            except Exception as e:
                logger.warning(f"Book callback error: {e}")
