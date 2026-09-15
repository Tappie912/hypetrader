"""
Risk Manager
============
Enforces hard limits before any order is placed:
  - Per-position notional cap
  - Total exposure cap
  - Single order size cap
  - Daily loss limit
  - Max drawdown circuit breaker

Call RiskManager.check() before every order. If it returns False, do NOT trade.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from utils.logger import logger, alert
from core.order_manager import Position, Side
import config


@dataclass
class RiskState:
    daily_realized_pnl: float = 0.0
    peak_account_value: float = 0.0
    current_account_value: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    session_start: float = field(default_factory=time.time)


class RiskManager:
    """
    Pre-trade risk checks and real-time position monitoring.
    """

    def __init__(self):
        self.state = RiskState()
        self._position_notionals: Dict[str, float] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def update_account_value(self, value: float) -> None:
        if value > self.state.peak_account_value:
            self.state.peak_account_value = value
        self.state.current_account_value = value
        self._check_drawdown(value)

    def record_pnl(self, pnl: float) -> None:
        self.state.daily_realized_pnl += pnl
        self._check_daily_loss()

    def update_positions(self, positions: List[Position]) -> None:
        self._position_notionals = {
            f"{p.asset}_{p.market}": p.size * p.entry_price
            for p in positions
        }

    def check_order(
        self,
        asset: str,
        market: str,
        side: Side,
        size: float,
        price: float,
    ) -> tuple[bool, str]:
        """
        Returns (allowed, reason). Call before every order.
        """
        if self.state.halted:
            return False, f"Trading halted: {self.state.halt_reason}"

        notional = size * price

        # Single order cap
        if notional > config.MAX_ORDER_USD:
            return False, f"Order notional ${notional:.0f} exceeds limit ${config.MAX_ORDER_USD}"

        # Per-position cap
        key = f"{asset}_{market}"
        existing = self._position_notionals.get(key, 0.0)
        if existing + notional > config.MAX_POSITION_USD:
            return False, (
                f"Position notional ${existing + notional:.0f} would exceed "
                f"limit ${config.MAX_POSITION_USD} for {asset} {market}"
            )

        # Total exposure cap
        total = sum(self._position_notionals.values()) + notional
        if total > config.MAX_TOTAL_EXPOSURE_USD:
            return False, f"Total exposure ${total:.0f} would exceed limit ${config.MAX_TOTAL_EXPOSURE_USD}"

        return True, "ok"

    def halt(self, reason: str) -> None:
        if not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = reason
            logger.critical(f"TRADING HALTED: {reason}")
            # Fire alert synchronously-ish
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(alert(f"🚨 TRADING HALTED: {reason}", "ERROR"))
            except Exception:
                pass

    def resume(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""
        logger.info("Trading resumed")

    def summary(self) -> str:
        lines = [
            "── Risk Summary ──────────────────────",
            f"  Account value   : ${self.state.current_account_value:,.2f}",
            f"  Peak value      : ${self.state.peak_account_value:,.2f}",
            f"  Daily PnL       : ${self.state.daily_realized_pnl:,.2f}",
            f"  Total exposure  : ${sum(self._position_notionals.values()):,.2f}",
            f"  Halted          : {self.state.halted}",
        ]
        if self.state.halted:
            lines.append(f"  Halt reason     : {self.state.halt_reason}")
        return "\n".join(lines)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _check_daily_loss(self) -> None:
        if self.state.daily_realized_pnl < -abs(config.MAX_DAILY_LOSS_USD):
            self.halt(
                f"Daily loss limit hit: ${self.state.daily_realized_pnl:.2f} "
                f"(limit: ${config.MAX_DAILY_LOSS_USD})"
            )

    def _check_drawdown(self, current: float) -> None:
        if self.state.peak_account_value <= 0:
            return
        drawdown = (self.state.peak_account_value - current) / self.state.peak_account_value
        if drawdown >= config.MAX_DRAWDOWN_PCT:
            self.halt(
                f"Max drawdown breached: {drawdown*100:.1f}% "
                f"(limit: {config.MAX_DRAWDOWN_PCT*100:.1f}%)"
            )
