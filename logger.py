"""
Logging & Alerting
==================
Structured rotating file logger + optional Slack/Telegram alerts.
"""

import logging
import logging.handlers
import os
import json
import asyncio
import aiohttp
from datetime import datetime, timezone
from typing import Optional

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config


def setup_logger(name: str = "hl_arb") -> logging.Logger:
    """Set up a rotating file + console logger."""
    os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Rotating file handler
    fh = logging.handlers.RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=config.LOG_ROTATE_MB * 1024 * 1024,
        backupCount=config.LOG_BACKUP_COUNT,
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


logger = setup_logger()


# ── Alert helpers ─────────────────────────────────────────────────────────────

async def _send_slack(message: str) -> None:
    if not config.SLACK_WEBHOOK_URL:
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                config.SLACK_WEBHOOK_URL,
                json={"text": message},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception as e:
        logger.warning(f"Slack alert failed: {e}")


async def _send_telegram(message: str) -> None:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                url,
                json={"chat_id": config.TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception as e:
        logger.warning(f"Telegram alert failed: {e}")


async def alert(message: str, level: str = "INFO") -> None:
    """Send an alert to all configured channels and log it."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    full = f"[{level}] {ts}\n{message}"
    logger.log(getattr(logging, level, logging.INFO), message)
    await asyncio.gather(
        _send_slack(full),
        _send_telegram(full),
        return_exceptions=True,
    )


def alert_sync(message: str, level: str = "INFO") -> None:
    """Fire-and-forget alert from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(alert(message, level))
        else:
            loop.run_until_complete(alert(message, level))
    except Exception:
        logger.warning(f"Could not send alert: {message}")
