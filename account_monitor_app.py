# -*- coding: utf-8 -*-
"""
Created on Thu Apr 24 17:13:08 2025

@author: LongLong
"""

import asyncio
import os
from contextlib import asynccontextmanager

import uvicorn

from account_monitor import register_monitor_account, start_monitor_scheduler
from config.settings import (
    DEFAULT_RUNTIME_LOG_LEVEL,
    MONITOR_NAV_HOST,
    MONITOR_NAV_PORT,
    PROJECT_ROOT,
)
from core.runtime_logging import maintain_daily_runtime_log, setup_runtime_logger
from nav_service import state
from nav_service.app_factory import create_app
from nav_service.routes import update_loop


BASE_DIR = str(PROJECT_ROOT)
os.chdir(BASE_DIR)

state.initialize(BASE_DIR, on_account_added=register_monitor_account)


@asynccontextmanager
async def lifespan(_app):
    update_task = asyncio.create_task(update_loop())
    scheduler_task = start_monitor_scheduler().start_background()
    log_rollover_task = asyncio.create_task(maintain_daily_runtime_log())
    try:
        yield
    finally:
        tasks = {
            update_task,
            scheduler_task,
            log_rollover_task,
            *state.account_update_tasks.values(),
        }
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await state.close_accounts()


app = create_app(BASE_DIR, lifespan=lifespan)


if __name__ == "__main__":
    uvicorn_level = {
        "MESSAGE": "info",
        "INFO": "info",
        "WARN": "warning",
    }.get(DEFAULT_RUNTIME_LOG_LEVEL.upper(), DEFAULT_RUNTIME_LOG_LEVEL.lower())
    setup_runtime_logger("uvicorn")
    setup_runtime_logger("uvicorn.error")
    uvicorn.run(
        "account_monitor_app:app",
        host=MONITOR_NAV_HOST,
        port=MONITOR_NAV_PORT,
        reload=False,
        access_log=False,
        log_config=None,
        log_level=uvicorn_level,
    )
