import asyncio
from datetime import datetime, timedelta

from config.settings import (
    DAILY_HOUR,
    DAILY_MINUTE,
    WEEKLY_RANKING_HOUR,
    WEEKLY_RANKING_MINUTE,
    WEEKLY_RANKING_WEEKDAY,
)


class MonitorScheduler:
    def __init__(self, logger=None):
        self._tasks = {"startup": [], "minute": [], "daily": [], "weekly": []}
        self._logger = logger
        self._last_daily_run_date = None
        self._daily_hour = DAILY_HOUR
        self._daily_minute = DAILY_MINUTE
        self._last_weekly_run_key = None
        self._weekly_weekday = WEEKLY_RANKING_WEEKDAY
        self._weekly_hour = WEEKLY_RANKING_HOUR
        self._weekly_minute = WEEKLY_RANKING_MINUTE

    def add_task(self, task_type: str, task_func, name: str = None):
        if task_type not in self._tasks:
            raise ValueError(f"unsupported scheduled task type: {task_type}")
        task_name = name or getattr(task_func, "__name__", repr(task_func))
        self._tasks[task_type].append((task_name, task_func))
        return task_func

    def start_background(self):
        return asyncio.create_task(self._run())

    async def _safe_run(self, task_name: str, coro):
        try:
            return await coro
        except Exception as exc:
            self._log("exception", "%s failed", task_name)
            return None

    async def _run_task_type(self, task_type: str):
        async def run_one(task_name, task_func):
            try:
                result = task_func()
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                self._log("exception", "%s:%s failed", task_type, task_name)

        tasks = self._tasks.get(task_type, [])
        if tasks:
            await asyncio.gather(*(run_one(task_name, task_func) for task_name, task_func in tasks))

    def _daily_due(self, now: datetime) -> bool:
        return (
            (now.hour, now.minute) >= (self._daily_hour, self._daily_minute)
            and self._last_daily_run_date != now.date()
        )

    def _weekly_due(self, now: datetime) -> bool:
        week_key = (now.isocalendar().year, now.isocalendar().week)
        return (
            now.weekday() == self._weekly_weekday
            and (now.hour, now.minute) >= (self._weekly_hour, self._weekly_minute)
            and self._last_weekly_run_key != week_key
        )

    async def _sleep_until_next_minute(self):
        now = datetime.now()
        next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        await asyncio.sleep(max(1.0, (next_minute - datetime.now()).total_seconds()))

    async def _run(self):
        await self._safe_run("startup minute tasks", self._run_task_type("minute"))
        await self._safe_run("one-time startup tasks", self._run_task_type("startup"))
        # Startup does not run the production daily task. Any startup preview
        # report is registered separately and uses the strict Feishu test route.
        # If today's scheduled time has passed, do not catch up the production task.
        now = datetime.now()
        if (now.hour, now.minute) >= (self._daily_hour, self._daily_minute):
            self._last_daily_run_date = now.date()
        if self._weekly_due(now):
            self._last_weekly_run_key = (now.isocalendar().year, now.isocalendar().week)

        self._log("info", "scheduler started")
        while True:
            await self._sleep_until_next_minute()
            now = datetime.now()
            if self._daily_due(now):
                await self._safe_run("daily tasks", self._run_task_type("daily"))
                self._last_daily_run_date = now.date()
            if self._weekly_due(now):
                await self._safe_run("weekly tasks", self._run_task_type("weekly"))
                self._last_weekly_run_key = (now.isocalendar().year, now.isocalendar().week)
            await self._safe_run("minute tasks", self._run_task_type("minute"))

    def _log(self, level: str, message: str, *args):
        if self._logger:
            getattr(self._logger, level)(message, *args)
