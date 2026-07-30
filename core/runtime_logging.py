import asyncio
import logging
import os
import re
import threading
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from config.settings import (
    MAX_RUNTIME_LOG_BYTES,
    RUNTIME_LOG_DIR,
    RUNTIME_LOG_FILE,
    logger_level,
)


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
_RUNTIME_LOG_FILENAME = re.compile(
    r"^runtime_(\d{8})(?:_(\d{6}))?\.log(?:\..*)?$"
)


class DailyRuntimeFileHandler(logging.FileHandler):
    """启动时使用时间戳文件，跨天后切换到当天 000000 文件。"""

    def __init__(self, filename, max_bytes, encoding="utf-8"):
        self._current_date = date.today()
        self._log_dir = Path(filename).resolve().parent
        self._max_bytes = max_bytes
        super().__init__(filename=filename, mode="a", encoding=encoding)

    def _available_timestamp_path(self, now):
        base_name = f"runtime_{now:%Y%m%d_%H%M%S}"
        candidate = self._log_dir / f"{base_name}.log"
        sequence = 1
        while candidate.exists():
            candidate = self._log_dir / f"{base_name}_{sequence:02d}.log"
            sequence += 1
        return candidate

    def _switch_file(self, target_path, target_date):
        if self.stream is not None:
            self.flush()
            self.stream.close()
        self.baseFilename = str(target_path)
        self.stream = self._open()
        self._current_date = target_date

    def _switch_to_date(self, target_date):
        self.acquire()
        try:
            self._switch_file(
                self._log_dir / f"runtime_{target_date:%Y%m%d}_000000.log",
                target_date,
            )
        finally:
            self.release()

    def _switch_for_size(self, now):
        self._switch_file(self._available_timestamp_path(now), now.date())

    def _would_exceed_limit(self, record):
        if self._max_bytes <= 0 or not os.path.exists(self.baseFilename):
            return False
        current_size = os.path.getsize(self.baseFilename)
        if current_size == 0:
            return False
        message_size = len((self.format(record) + self.terminator).encode(self.encoding or "utf-8"))
        return current_size + message_size > self._max_bytes

    def emit(self, record):
        today = date.today()
        if today != self._current_date:
            self._switch_to_date(today)
        if self._would_exceed_limit(record):
            self._switch_for_size(datetime.now())
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
            max_bytes=MAX_RUNTIME_LOG_BYTES,
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


def _runtime_log_timestamp(path: Path) -> datetime:
    """Get a log's creation time from its name, falling back to modification time."""
    match = _RUNTIME_LOG_FILENAME.match(path.name)
    if match is not None:
        time_text = match.group(2) or "000000"
        return datetime.strptime(f"{match.group(1)}{time_text}", "%Y%m%d%H%M%S")
    return datetime.fromtimestamp(path.stat().st_mtime)


def cleanup_runtime_logs(retention_days: int = 7, now: datetime = None):
    """Delete inactive runtime log files older than the retention period.

    Returns ``(deleted_paths, failed_paths)``. The active file handler target is
    always preserved, even if its filename or modification time is unexpectedly
    old.
    """
    global _log_date_cache_day, _log_date_cache

    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")

    cutoff = (now or datetime.now()) - timedelta(days=retention_days)
    active_path = None
    if _shared_file_handler is not None:
        active_path = Path(_shared_file_handler.baseFilename).resolve()

    deleted_paths = []
    failed_paths = []
    for path in _runtime_log_files():
        try:
            if active_path is not None and path.resolve() == active_path:
                continue
            if _runtime_log_timestamp(path) >= cutoff:
                continue
            path.unlink()
            deleted_paths.append(path)
        except (OSError, ValueError):
            failed_paths.append(path)

    if deleted_paths:
        with _log_date_cache_lock:
            _log_date_cache_day = None
            _log_date_cache = ()

    return deleted_paths, failed_paths


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


def iter_runtime_log_file_date(path, selected_date: str):
    """流式返回单个文件内指定日期的日志，并保留异常堆栈等续行。"""
    include_line = False
    with Path(path).open("r", encoding="utf-8", errors="replace") as log_file:
        for line in log_file:
            line_date = _line_date(line)
            if line_date is not None:
                include_line = line_date.isoformat() == selected_date
            if include_line:
                yield line.encode("utf-8")


def runtime_log_files_for_date(selected_date: str):
    """返回至少包含一条所选日期日志的文件。"""
    matched_files = []
    for path in _runtime_log_files():
        if any(True for _ in iter_runtime_log_file_date(path, selected_date)):
            matched_files.append(path)
    return matched_files


def build_runtime_log_archive(paths, selected_date: str):
    """将多个日志文件中所选日期的内容写入临时 ZIP。"""
    with NamedTemporaryFile(prefix="runtime_logs_", suffix=".zip", delete=False) as temp_file:
        archive_path = temp_file.name

    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                with archive.open(Path(path).name, "w") as archived_log:
                    for chunk in iter_runtime_log_file_date(path, selected_date):
                        archived_log.write(chunk)
        return archive_path
    except Exception:
        os.unlink(archive_path)
        raise
