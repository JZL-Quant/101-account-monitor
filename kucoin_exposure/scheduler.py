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
            # 定时任务不排队，避免手动刷新或交易后的校验仍在执行时
            # 紧接着再占用一轮 KuCoin REST 配额。
            await service.refresh(skip_if_running=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Scheduled KuCoin refresh failed")
        await asyncio.sleep(interval_seconds)
