import logging
import os
from logging.handlers import TimedRotatingFileHandler

from config.settings import RUNTIME_LOG_DIR, logger_level


logging.addLevelName(logging.INFO, "MESSAGE")

LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "MESSAGE": logging.INFO,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
}


def normalize_log_level(level, default=logging.INFO) -> int:
    if isinstance(level, int):
        return level
    if level is None:
        return default
    return LOG_LEVELS.get(str(level).strip().upper(), default)


def resolve_log_level(log_name: str, default="ERROR") -> int:
    default_level = normalize_log_level(default)
    return normalize_log_level(logger_level(log_name), default_level)


def setup_runtime_logger(log_name: str, stream_level=None, default_level="ERROR") -> logging.Logger:
    logger = logging.getLogger(log_name)
    effective_level = resolve_log_level(log_name, default_level)

    if logger.handlers:
        logger.setLevel(effective_level)
        for handler in logger.handlers:
            handler.setLevel(effective_level)
        return logger

    logger.setLevel(effective_level)
    logger.propagate = False

    log_dir = str(RUNTIME_LOG_DIR)
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"{log_name}.log")

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = TimedRotatingFileHandler(
        filename=log_file,
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setLevel(effective_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_level = normalize_log_level(stream_level, effective_level)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(console_level)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger
