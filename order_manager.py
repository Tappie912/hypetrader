"""
Order Management
================
Place, modify, and cancel orders on Hyperliquid via the REST exchange API.
Handles EIP-712 signing, order tracking, and fills.

Supports:
  - Market and limit orders (perp + spot)
  - Reduce-only orders
  - Bulk cancel
  - Open order / position queries
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional
import aiohttp
import msgpack
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_hex

from logger import logger, alert
import config


class Side(str, Enum):
    BUY  = "B"
    SELL = "A"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT  = "limit"


class TIF(str, Enum):
    GTC = "Gtc"
    IOC = "Ioc"
    ALO = "Alo"   # Add Liquidity Only (post-only)


@dataclass
class Order:
    client_id: str
    asset: str
    market: str          # "perp" | "spot"
    side: Side
    size: float
    price: float         # 0.0 for market orders
    order_type: OrderType = OrderType.LIMIT
    tif: TIF             = TIF.GTC
    reduce_only: bool    = False
    hl_oid: Optional[int] = None   # Hyperliquid order ID (set after placement)
    filled: float        = 0.0
    status: str          = "pending"   # pending | open | filled | cancelled | error
    created_at: float    = field(default_factory=time.time)


@dataclass
class Position:
    asset: str
    market: str
    side: Side
    size: float
    entry_price: float
    unrealized_pnl: float = 0.0
    leverage: float       = 1.0


class OrderManager:
    """
    Manages order lifecycle against Hyperliquid's /exchange endpoint.
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._account = Account.from_key(config.PRIVATE_KEY)
        self._orders: Dict[str, Order] = {}   # client_id -> Order
        self._asset_index: Dict[str, int] = {}  # populated on first meta fetch

    # ── Session lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        await self._load_asset_indices()
        logger.info("OrderManager ready")

    async def stop(self) -> None:
        if self._session:
            await self._session.close()

    # ── Public order API ──────────────────────────────────────────────────────

    async def place_limit(
        self,
        asset: str,
        side: Side,
        size: float,
        price: float,
        market: str = "perp",
        tif: TIF = TIF.GTC,
        reduce_only: bool = False,
        client_id: Optional[str] = None,
    ) -> Optional[Order]:
        cid = client_id or self._gen_cid(asset, side)
        order = Order(
            client_id=cid, asset=asset, market=market,
            side=side, size=size, price=price,
            order_type=OrderType.LIMIT, tif=tif,
            reduce_only=reduce_only,
        )
        return await self._submit(order)

    async def place_market(
        self,
        asset: str,
        side: Side,
        size: float,
        market: str = "perp",
        reduce_only: bool = False,
        client_id: Optional[str] = None,
    ) -> Optional[Order]:
        cid = client_id or self._gen_cid(asset, side)
        order = Order(
            client_id=cid, asset=asset, market=market,
            side=side, size=size, price=0.0,
            order_type=OrderType.MARKET, tif=TIF.IOC,
            reduce_only=reduce_only,
        )
        return await self._submit(order)

    async def cancel(self, order: Order) -> bool:
        if not order.hl_oid:
            logger.warning(f"Cannot cancel {order.client_id}: no HL order ID")
            return False
        asset_idx = self._asset_index.get(order.asset)
        if asset_idx is None:
            logger.error(f"Unknown asset: {order.asset}")
            return False

        action = {
            "type": "cancel",
            "cancels": [{"a": asset_idx, "o": order.hl_oid}],
        }
        resp = await self._signed_post(action)
        if resp and resp.get("status") == "ok":
            order.status = "cancelled"
            logger.info(f"Cancelled order {order.client_id} (oid={order.hl_oid})")
            return True
        logger.warning(f"Cancel failed for {order.client_id}: {resp}")
        return False

    async def cancel_all(self, asset: Optional[str] = None) -> int:
        """Cancel all tracked open orders, optionally filtered by asset."""
        targets = [
            o for o in self._orders.values()
            if o.status == "open" and (asset is None or o.asset == asset)
        ]
        results = await asyncio.gather(*[self.cancel(o) for o in targets])
        cancelled = sum(results)
        logger.info(f"Cancelled {cancelled}/{len(targets)} orders")
        return cancelled

    async def modify(self, order: Order, new_price: float, new_size: Optional[float] = None) -> Optional[Order]:
        """Cancel and replace — Hyperliquid supports native modify via 'modify' action."""
        if not order.hl_oid:
            return None
        asset_idx = self._asset_index.get(order.asset)
        if asset_idx is None:
            return None

        sz = new_size or order.size
        action = {
            "type": "modify",
            "oid": order.hl_oid,
            "order": self._build_order_wire(order.asset, order.side, sz, new_price, order.tif, order.reduce_only),
        }
        resp = await self._signed_post(action)
        if resp and resp.get("status") == "ok":
            order.price = new_price
            if new_size:
                order.size = new_size
            logger.info(f"Modified order {order.client_id}: price={new_price}, size={sz}")
            return order
        logger.warning(f"Modify failed for {order.client_id}: {resp}")
        return None

    # ── Position & account queries ────────────────────────────────────────────

    async def get_positions(self) -> List[Position]:
        """Fetch all open perpetual positions."""
        resp = await self._info_post({"type": "clearinghouseState", "user": config.WALLET_ADDRESS})
        positions = []
        if not resp:
            return positions
        for p in resp.get("assetPositions", []):
            pos = p.get("position", {})
            size = float(pos.get("szi", 0))
            if size == 0:
                continue
            positions.append(Position(
                asset=pos["coin"],
                market="perp",
                side=Side.BUY if size > 0 else Side.SELL,
                size=abs(size),
                entry_price=float(pos.get("entryPx", 0)),
                unrealized_pnl=float(pos.get("unrealizedPnl", 0)),
                leverage=float(pos.get("leverage", {}).get("value", 1)),
            ))
        return positions

    async def get_open_orders(self) -> List[dict]:
        resp = await self._info_post({"type": "openOrders", "user": config.WALLET_ADDRESS})
        return resp or []

    async def get_account_value(self) -> float:
        resp = await self._info_post({"type": "clearinghouseState", "user": config.WALLET_ADDRESS})
        if not resp:
            return 0.0
        return float(resp.get("marginSummary", {}).get("accountValue", 0))

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _submit(self, order: Order) -> Optional[Order]:
        asset_idx = (
            config.SPOT_ASSET_INDICES.get(order.asset)
            if order.market == "spot"
            else self._asset_index.get(order.asset)
        )
        if asset_idx is None:
            logger.error(f"Asset not found in index: {order.asset}")
            order.status = "error"
            self._orders[order.client_id] = order
            return order

        wire_order = self._build_order_wire(
            order.asset, order.side, order.size,
            order.price, order.tif, order.reduce_only, order.market,
        )
        action = {
            "type": "order",
            "orders": [wire_order],
            "grouping": "na",
        }
        resp = await self._signed_post(action)

        if not resp:
            order.status = "error"
            self._orders[order.client_id] = order
            return order

        if not isinstance(resp, dict):
            order.status = "error"
            logger.error(f"Order request returned an unexpected response: {resp!r}")
            self._orders[order.client_id] = order
            return order

        response = resp.get("response")
        if not isinstance(response, dict):
            order.status = "error"
            logger.error(f"Order rejected by exchange: {resp!r}")
            self._orders[order.client_id] = order
            return order

        data = response.get("data")
        if not isinstance(data, dict):
            order.status = "error"
            logger.error(f"Order response missing data: {resp!r}")
            self._orders[order.client_id] = order
            return order

        statuses = data.get("statuses", [])
        if not isinstance(statuses, list):
            order.status = "error"
            logger.error(f"Order response has invalid statuses: {resp!r}")
            self._orders[order.client_id] = order
            return order

        if statuses:
            st = statuses[0]
            if not isinstance(st, dict):
                order.status = "error"
                logger.error(f"Order status has unexpected shape: {st!r}")
            elif "resting" in st:
                order.hl_oid = st["resting"]["oid"]
                order.status = "open"
                logger.info(f"Order placed: {order.client_id} | {order.side.name} {order.size} {order.asset} @ {order.price} | oid={order.hl_oid}")
                if config.ALERT_ON_FILL:
                    await alert(f"✅ Order open: {order.side.name} {order.size} {order.asset} @ {order.price}")
            elif "filled" in st:
                order.status = "filled"
                order.filled = order.size
                fill_px = st["filled"].get("avgPx", order.price)
                logger.info(f"Order filled immediately: {order.client_id} @ avg {fill_px}")
                if config.ALERT_ON_FILL:
                    await alert(f"🎯 Order filled: {order.side.name} {order.size} {order.asset} @ {fill_px}")
            elif "error" in st:
                order.status = "error"
                logger.error(f"Order error: {st['error']}")
        else:
            order.status = "error"
            logger.error(f"Unexpected response: {resp}")

        self._orders[order.client_id] = order
        return order

    def _build_order_wire(
        self, asset: str, side: Side, size: float, price: float,
        tif: TIF, reduce_only: bool, market: str = "perp",
    ) -> dict:
        asset_idx = (
            config.SPOT_ASSET_INDICES.get(asset, 0)
            if market == "spot"
            else self._asset_index.get(asset, 0)
        )
        is_buy = side == Side.BUY

        if price == 0.0:
            # Market order: use a very aggressive limit price
            order_type = {"limit": {"tif": "Ioc"}}
            limit_px = "0"   # will be treated as market
        else:
            order_type = {"limit": {"tif": tif.value}}
            limit_px = str(price)

        return {
            "a": asset_idx,
            "b": is_buy,
            "p": limit_px,
            "s": str(size),
            "r": reduce_only,
            "t": order_type,
        }

    async def _signed_post(self, action: dict) -> Optional[dict]:
        """Sign and submit an action to /exchange."""
        nonce = int(time.time() * 1000)
        payload = {"action": action, "nonce": nonce, "signature": self._sign(action, nonce)}
        return await self._post(f"{config.HYPERLIQUID_API_URL}/exchange", payload)

    async def _info_post(self, body: dict) -> Optional[dict]:
        return await self._post(f"{config.HYPERLIQUID_API_URL}/info", body)

    async def _post(self, url: str, body: dict) -> Optional[dict]:
        try:
            async with self._session.post(
                url, json=body,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.error(f"HTTP {resp.status} from {url}: {data}")
                    return None
                return data
        except asyncio.TimeoutError:
            logger.error(f"Timeout posting to {url}")
        except Exception as e:
            logger.error(f"POST error to {url}: {e}")
        return None

    def _sign(self, action: dict, nonce: int) -> dict:
        """
        EIP-712 signing for Hyperliquid exchange actions.
        See: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/signing
        """
        connection_id = bytes(32)   # mainnet = 32 zero bytes
        action_hash = self._hash_action(action, nonce, connection_id)
        phantom_agent = {
            "source": "a",
            "connectionId": action_hash,
        }
        domain = {
            "chainId": 1337,
            "name": "Exchange",
            "verifyingContract": "0x0000000000000000000000000000000000000000",
            "version": "1",
        }
        types = {
            "Agent": [
                {"name": "source",       "type": "string"},
                {"name": "connectionId", "type": "bytes32"},
            ]
        }
        structured = {
            "domain": domain,
            "types": types,
            "primaryType": "Agent",
            "message": phantom_agent,
        }
        signed = self._account.sign_message(encode_typed_data(full_message=structured))
        return {"r": to_hex(signed["r"]), "s": to_hex(signed["s"]), "v": signed["v"]}

    def _hash_action(self, action: dict, nonce: int, connection_id: bytes) -> bytes:
        action_bytes = msgpack.packb(action)
        nonce_bytes  = nonce.to_bytes(8, "big")
        # Current Hyperliquid L1 actions encode a missing active pool as 0x00.
        # The legacy connection ID is not included for this order payload.
        payload = action_bytes + nonce_bytes + b"\x00"
        return keccak(payload)

    async def _load_asset_indices(self) -> None:
        resp = await self._info_post({"type": "meta"})
        if not resp:
            logger.error("Failed to load asset metadata")
            return
        for i, asset in enumerate(resp.get("universe", [])):
            self._asset_index[asset["name"]] = i
        logger.info(f"Loaded {len(self._asset_index)} asset indices")

    @staticmethod
    def _gen_cid(asset: str, side: Side) -> str:
        return f"{asset}_{side.name}_{int(time.time()*1000)}"
