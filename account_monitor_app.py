# -*- coding: utf-8 -*-
"""
Created on Thu Apr 24 17:13:08 2025

@author: LongLong
"""

import asyncio
import os
from contextlib import asynccontextmanager

import uvicorn

from account_monitor import start_monitor_scheduler
from config.settings import MONITOR_NAV_HOST, MONITOR_NAV_PORT, PROJECT_ROOT
from nav_service import state
from nav_service.app_factory import create_app
from nav_service.routes import update_loop


BASE_DIR = str(PROJECT_ROOT)
os.chdir(BASE_DIR)

state.initialize(BASE_DIR)


@asynccontextmanager
async def lifespan(_app):
    update_task = asyncio.create_task(update_loop())
    scheduler_task = start_monitor_scheduler().start_background()
    try:
        yield
    finally:
        tasks = {update_task, scheduler_task, *state.account_update_tasks.values()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await state.close_accounts()


app = create_app(BASE_DIR, lifespan=lifespan)


if __name__ == "__main__":
    uvicorn.run(
        "account_monitor_app:app",
        host=MONITOR_NAV_HOST,
        port=MONITOR_NAV_PORT,
        reload=False,
    )
