"""集中管理项目路径、运行参数，并从本地文件读取敏感配置。"""

import os
import re
from datetime import datetime
from pathlib import Path

try:
    from . import local_secrets as _local_secrets
except ModuleNotFoundError:
    _local_secrets = None


def _secret(name: str, default=""):
    if _local_secrets is None:
        return default
    return getattr(_local_secrets, name, default)


FEISHU_APP_ID = _secret("FEISHU_APP_ID")
FEISHU_APP_SECRET = _secret("FEISHU_APP_SECRET")
GRAFANA_API_TOKEN = _secret("GRAFANA_API_TOKEN")
GRAFANA_USER = _secret("GRAFANA_USER")
GRAFANA_PASSWORD = _secret("GRAFANA_PASSWORD")

# Prefer the list form so notification groups can be added or removed in one
# place. Keep reading the two legacy variables for existing deployments.
_configured_feishu_webhooks = _secret("FEISHU_BOT_WEBHOOK_URLS", None)
if _configured_feishu_webhooks is None:
    _configured_feishu_webhooks = (
        _secret("FEISHU_BOT_WEBHOOK_URL"),
        _secret("FEISHU_BOT_WEBHOOK_URL_1"),
    )
elif isinstance(_configured_feishu_webhooks, str):
    _configured_feishu_webhooks = (_configured_feishu_webhooks,)

FEISHU_BOT_WEBHOOK_URLS = tuple(
    dict.fromkeys(
        url.strip()
        for url in _configured_feishu_webhooks
        if isinstance(url, str) and url.strip()
    )
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_CONFIG_PATH = PROJECT_ROOT / "accounts_config.yaml"
MINUTE_SNAPSHOT_DIR = PROJECT_ROOT / "minute_snapshots"
RUNTIME_LOG_DIR = PROJECT_ROOT / "runtime_logs"
RUNTIME_LOG_FILE = Path(
    os.getenv(
        "RUNTIME_LOG_FILE",
        str(RUNTIME_LOG_DIR / f"runtime_{datetime.now():%Y%m%d_%H%M%S}.log"),
    )
)
LEGACY_SNAPSHOT_DIRS = (
    PROJECT_ROOT.parent / "Binance_monitor",
    PROJECT_ROOT.parent / "Binance_monitor_B",
    PROJECT_ROOT.parent / "Gate_monitor",
)

MONITOR_NAV_HOST = os.getenv("MONITOR_NAV_HOST", "127.0.0.1")
MONITOR_NAV_PORT = int(os.getenv("MONITOR_NAV_PORT", "7007"))

DAILY_HOUR = int(os.getenv("BINANCE_DAILY_HOUR", "10"))
DAILY_MINUTE = int(os.getenv("BINANCE_DAILY_MINUTE", "31"))

# Optional BigQuery persistence for daily Binance return rows. By default the
# service-account key is loaded from the project root; deployments can override
# the path without changing code.
BIGQUERY_RETURN_ENABLED = os.getenv("BIGQUERY_RETURN_ENABLED", "1") == "1"
BIGQUERY_PROJECT_ID = os.getenv("BIGQUERY_PROJECT_ID", "applied-groove-464707-r8").strip()
BIGQUERY_DATASET = os.getenv("BIGQUERY_DATASET", "Daily_reports").strip()
BIGQUERY_RETURN_TABLE = os.getenv("BIGQUERY_RETURN_TABLE", "BN_Return_temp").strip()
BIGQUERY_CREDENTIALS_PATH = Path(
    os.getenv(
        "BIGQUERY_CREDENTIALS_PATH",
        str(PROJECT_ROOT / "applied-groove-464707-r8-bef90a05e7c9.json"),
    )
).expanduser()

# 分钟级大额资金变动告警阈值。
LARGE_EQUITY_CHANGE_THRESHOLDS = {
    "USDT": float(os.getenv("LARGE_EQUITY_CHANGE_USDT_THRESHOLD", "12000")),
    "BTC": float(os.getenv("LARGE_EQUITY_CHANGE_BTC_THRESHOLD", "2")),
}

DEFAULT_RUNTIME_LOG_LEVEL = os.getenv("RUNTIME_LOG_LEVEL", "WARNING")

# 收益率及净值计算的最小有效分母，避免零值或极小值导致溢出。
MIN_VALID_CALCULATION_VALUE = 1e-7

# 单个运行日志最大 100 MiB；达到上限后按当前时间切换新文件。
MAX_RUNTIME_LOG_BYTES = 1024 * 1024 * 1024
RUNTIME_LOG_RETENTION_DAYS = int(os.getenv("RUNTIME_LOG_RETENTION_DAYS", "7"))


def logger_level(log_name: str) -> str:
    safe_name = re.sub(r"\W+", "_", log_name).strip("_").upper()
    return os.getenv(f"{safe_name}_LOG_LEVEL", DEFAULT_RUNTIME_LOG_LEVEL)


GRAFANA_URL = os.getenv("GRAFANA_URL", "http://127.0.0.1:3000").strip()
GRAFANA_PROMETHEUS_UID_ACCOUNT_MONITOR = os.getenv(
    "GRAFANA_PROMETHEUS_UID_ACCOUNT_MONITOR", ""
).strip()
GRAFANA_PROMETHEUS_INSTANCE_ACCOUNT_MONITOR = os.getenv(
    "GRAFANA_PROMETHEUS_INSTANCE_ACCOUNT_MONITOR", "localhost:7007"
).strip()

ANNUALIZED_DASHBOARD_UID = _secret("ANNUALIZED_DASHBOARD_UID")
NAV_DASHBOARD_UID = _secret("NAV_DASHBOARD_UID")
REFERENCE_ANNUALIZED_DASHBOARD_UID = _secret("REFERENCE_ANNUALIZED_DASHBOARD_UID")
REFERENCE_NAV_DASHBOARD_UID = _secret("REFERENCE_NAV_DASHBOARD_UID")


def environment_value(name: str, default: str = "") -> str:
    """读取命令行动态数据源指定的非敏感环境变量。"""
    return os.getenv(name, default).strip()
