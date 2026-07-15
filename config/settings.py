"""集中管理项目路径、运行参数，并从本地文件读取敏感配置。"""

import os
import re
from pathlib import Path

try:
    from .local_secrets import (
        FEISHU_APP_ID,
        FEISHU_APP_SECRET,
        FEISHU_BOT_WEBHOOK_URL,
        FEISHU_BOT_WEBHOOK_URL_1,
        GRAFANA_API_TOKEN,
        GRAFANA_PASSWORD,
        GRAFANA_USER,
    )
except ModuleNotFoundError:
    FEISHU_BOT_WEBHOOK_URL = ""
    FEISHU_BOT_WEBHOOK_URL_1 = ""
    FEISHU_APP_ID = ""
    FEISHU_APP_SECRET = ""
    GRAFANA_API_TOKEN = ""
    GRAFANA_USER = ""
    GRAFANA_PASSWORD = ""


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_CONFIG_PATH = PROJECT_ROOT / "accounts_config.yaml"
MINUTE_SNAPSHOT_DIR = PROJECT_ROOT / "minute_snapshots"
RUNTIME_LOG_DIR = PROJECT_ROOT / "runtime_logs"
LEGACY_SNAPSHOT_DIRS = (
    PROJECT_ROOT.parent / "Binance_monitor",
    PROJECT_ROOT.parent / "Binance_monitor_B",
    PROJECT_ROOT.parent / "Gate_monitor",
)

MONITOR_NAV_HOST = os.getenv("MONITOR_NAV_HOST", "127.0.0.1")
MONITOR_NAV_PORT = int(os.getenv("MONITOR_NAV_PORT", "7007"))

DAILY_HOUR = int(os.getenv("BINANCE_DAILY_HOUR", "10"))
DAILY_MINUTE = int(os.getenv("BINANCE_DAILY_MINUTE", "31"))
RUN_DAILY_ON_STARTUP = os.getenv("BINANCE_RUN_DAILY_ON_STARTUP", "1") == "1"

DEFAULT_RUNTIME_LOG_LEVEL = os.getenv("RUNTIME_LOG_LEVEL", "WARNING")

# 收益率及净值计算的最小有效分母，避免零值或极小值导致溢出。
MIN_VALID_CALCULATION_VALUE = 1e-7


def logger_level(log_name: str) -> str:
    safe_name = re.sub(r"\W+", "_", log_name).strip("_").upper()
    return os.getenv(f"{safe_name}_LOG_LEVEL", DEFAULT_RUNTIME_LOG_LEVEL)


FEISHU_BOT_WEBHOOK_URLS = tuple(
    value for value in (FEISHU_BOT_WEBHOOK_URL, FEISHU_BOT_WEBHOOK_URL_1) if value
)

GRAFANA_URL = os.getenv("GRAFANA_URL", "http://127.0.0.1:3000").strip()
GRAFANA_PROMETHEUS_UID_ACCOUNT_MONITOR = os.getenv(
    "GRAFANA_PROMETHEUS_UID_ACCOUNT_MONITOR", ""
).strip()
GRAFANA_PROMETHEUS_INSTANCE_ACCOUNT_MONITOR = os.getenv(
    "GRAFANA_PROMETHEUS_INSTANCE_ACCOUNT_MONITOR", "localhost:7007"
).strip()

ANNUALIZED_DASHBOARD_UID = "383918d5-4412-4311-8831-074592cfa7b0"
NAV_DASHBOARD_UID = "964cbc88-efb7-4c31-92e4-d42476f1ebfb"
REFERENCE_ANNUALIZED_DASHBOARD_UID = "c7c8c3b8-0c2d-409d-a1af-1e004385788e"
REFERENCE_NAV_DASHBOARD_UID = "4c9c0ed6-3f42-4bcd-9e5d-ce185b2d9d9e"


def environment_value(name: str, default: str = "") -> str:
    """读取命令行动态数据源指定的非敏感环境变量。"""
    return os.getenv(name, default).strip()
