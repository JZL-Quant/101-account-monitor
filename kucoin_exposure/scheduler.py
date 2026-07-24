from __future__ import annotations

import asyncio
import logging

from .service import ExposureService


LOGGER = logging.getLogger("kucoin_exposure.scheduler")


async def run_refresh_loop(
    service: ExposureService, interval_seconds: int
) -> None:
    while True:
        try:
            await service.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Scheduled KuCoin refresh failed")
        await asyncio.sleep(interval_seconds)
