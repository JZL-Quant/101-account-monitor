import asyncio
import logging
import os
import re
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path

from config.settings import RUNTIME_LOG_DIR, RUNTIME_LOG_FILE, logger_level


logging.addLevelName(logging.INFO, "MESSAGE")

LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "MESSAGE": logging.INFO,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
}

_shared_file_handler = None
_log_date_cache_day = None
_log_date_cache = ()
_log_date_cache_lock = threading.Lock()
_LOG_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})")


class DailyRuntimeFileHandler(logging.FileHandler):
    """启动时使用时间戳文件，跨天后切换到当天 000000 文件。"""

    def __init__(self, filename, encoding="utf-8"):
        self._current_date = date.today()
        self._log_dir = Path(filename).resolve().parent
        super().__init__(filename=filename, mode="a", encoding=encoding)

    def _switch_to_date(self, target_date):
        self.acquire()
        try:
            if self.stream is not None:
                self.flush()
                self.stream.close()
            self.baseFilename = str(
                self._log_dir / f"runtime_{target_date:%Y%m%d}_000000.log"
            )
            self.stream = self._open()
            self._current_date = target_date
        finally:
            self.release()

    def emit(self, record):
        today = date.today()
        if today != self._current_date:
            self._switch_to_date(today)
        super().emit(record)


def normalize_log_level(level, default=logging.INFO) -> int:
    if isinstance(level, int):
        return level
    if level is None:
        return default
    return LOG_LEVELS.get(str(level).strip().upper(), default)


def resolve_log_level(log_name: str, default="WARNING") -> int:
    default_level = normalize_log_level(default)
    return normalize_log_level(logger_level(log_name), default_level)


def setup_runtime_logger(log_name: str, stream_level=None, default_level="WARNING") -> logging.Logger:
    global _shared_file_handler

    logger = logging.getLogger(log_name)
    effective_level = resolve_log_level(log_name, default_level)

    if logger.handlers:
        logger.setLevel(effective_level)
        return logger

    logger.setLevel(effective_level)
    logger.propagate = False

    log_dir = str(RUNTIME_LOG_DIR)
    os.makedirs(log_dir, exist_ok=True)
    log_file = str(RUNTIME_LOG_FILE)

    if _shared_file_handler is None:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        )
        _shared_file_handler = DailyRuntimeFileHandler(
            filename=log_file,
            encoding="utf-8",
        )
        _shared_file_handler.setLevel(logging.NOTSET)
        _shared_file_handler.setFormatter(formatter)

    logger.addHandler(_shared_file_handler)
    return logger


async def maintain_daily_runtime_log():
    """在本地时间午夜主动创建并切换到当天的 000000 日志文件。"""
    while True:
        now = datetime.now()
        next_midnight = datetime.combine(now.date() + timedelta(days=1), time.min)
        await asyncio.sleep((next_midnight - now).total_seconds())
        if _shared_file_handler is not None:
            _shared_file_handler._switch_to_date(date.today())


def _runtime_log_files():
    if not RUNTIME_LOG_DIR.exists():
        return []
    return sorted(path for path in RUNTIME_LOG_DIR.glob("*.log*") if path.is_file())


def _line_date(line: str):
    match = _LOG_DATE_PREFIX.match(line)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def available_runtime_log_dates():
    """返回日志中可下载的日期；同一天最多扫描一次日志目录。"""
    global _log_date_cache_day, _log_date_cache

    today = date.today()
    with _log_date_cache_lock:
        if _log_date_cache_day == today:
            return list(_log_date_cache)

        dates = set()
        for path in _runtime_log_files():
            with path.open("r", encoding="utf-8", errors="replace") as log_file:
                for line in log_file:
                    line_date = _line_date(line)
                    if line_date is not None:
                        dates.add(line_date.isoformat())

        _log_date_cache_day = today
        _log_date_cache = tuple(sorted(dates, reverse=True))
        return list(_log_date_cache)


def iter_runtime_log_date(selected_date: str):
    """流式返回指定日期的日志，并保留异常堆栈等续行。"""
    for path in _runtime_log_files():
        include_line = False
        with path.open("r", encoding="utf-8", errors="replace") as log_file:
            for line in log_file:
                line_date = _line_date(line)
                if line_date is not None:
                    include_line = line_date.isoformat() == selected_date
                if include_line:
                    yield line.encode("utf-8")
