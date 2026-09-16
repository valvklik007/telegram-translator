"""Backward-compatible entry point for the Telegram translator bot."""

import asyncio
import logging

from app.application import main



logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


if __name__ == "__main__":
    asyncio.run(main())
