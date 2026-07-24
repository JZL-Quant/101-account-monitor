from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PACKAGE_DIR / "config.yaml"


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass(frozen=True)
class LoginConfig:
    username: str
    password: str
    session_secret: str
    session_days: int = 30


@dataclass(frozen=True)
class KucoinConfig:
    api_key: str
    api_secret: str
    api_passphrase: str
    site_type: str = "global"


@dataclass(frozen=True)
class MonitorConfig:
    interval_seconds: int = 60
    stale_after_seconds: int = 180


@dataclass(frozen=True)
class FeishuConfig:
    enabled: bool = False
    webhook_url: str = ""
    timeout_seconds: float = 8.0


@dataclass(frozen=True)
class HedgeConfig:
    quote_currency: str = "USDT"
    matched_threshold_percent: float = 1.0
    warning_threshold_percent: float = 5.0
    dust_value_usdt: float = 1.0
    aliases: dict[str, str] = field(default_factory=lambda: {"XBT": "BTC"})
    excluded_assets: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class TradingConfig:
    enabled: bool = False
    cancel_open_orders_before_close: bool = True
    verification_delay_seconds: float = 1.5


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    login: LoginConfig
    kucoin: KucoinConfig
    monitor: MonitorConfig
    feishu: FeishuConfig
    hedge: HedgeConfig
    trading: TradingConfig
    config_path: Path
    database_path: Path


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name, {})
    if not isinstance(section, dict):
        raise ValueError(f"config section {name!r} must be a mapping")
    return section


def _required(section: dict[str, Any], key: str, section_name: str) -> str:
    value = str(section.get(key, "")).strip()
    if not value:
        raise ValueError(f"{section_name}.{key} is required")
    return value


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise FileNotFoundError(
            f"KuCoin config not found: {config_path}. "
            f"Copy {PACKAGE_DIR / 'config.example.yaml'} to config.yaml first."
        )
    with config_path.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}
    if not isinstance(raw, dict):
        raise ValueError("KuCoin config root must be a mapping")

    server = _section(raw, "server")
    login = _section(raw, "login")
    kucoin = _section(raw, "kucoin")
    monitor = _section(raw, "monitor")
    feishu = _section(raw, "feishu")
    hedge = _section(raw, "hedge")
    symbols = _section(raw, "symbols")
    trading = _section(raw, "trading")
    aliases = _section(symbols, "aliases")

    login_config = LoginConfig(
        username=_required(login, "username", "login"),
        password=_required(login, "password", "login"),
        session_secret=_required(login, "session_secret", "login"),
        session_days=max(1, int(login.get("session_days", 30))),
    )
    if len(login_config.session_secret) < 24:
        raise ValueError("login.session_secret must contain at least 24 characters")

    quote_currency = str(hedge.get("quote_currency", "USDT")).strip().upper()
    normalized_aliases = {
        str(key).strip().upper(): str(value).strip().upper()
        for key, value in (aliases or {"XBT": "BTC"}).items()
    }
    raw_excluded_assets = hedge.get("excluded_assets", [])
    if not isinstance(raw_excluded_assets, list):
        raise ValueError("hedge.excluded_assets must be a list")
    excluded_assets = frozenset(
        normalized_aliases.get(asset, asset)
        for value in raw_excluded_assets
        if (asset := str(value).strip().upper())
    )
    feishu_config = FeishuConfig(
        enabled=bool(feishu.get("enabled", False)),
        webhook_url=str(feishu.get("webhook_url", "")).strip(),
        timeout_seconds=max(1.0, float(feishu.get("timeout_seconds", 8))),
    )
    if feishu_config.enabled and not feishu_config.webhook_url:
        raise ValueError("feishu.webhook_url is required when feishu.enabled is true")

    database_path = PACKAGE_DIR / "runtime_data" / "kucoin_exposure.sqlite3"
    return AppConfig(
        server=ServerConfig(
            host=str(server.get("host", "127.0.0.1")).strip(),
            port=int(server.get("port", 8000)),
        ),
        login=login_config,
        kucoin=KucoinConfig(
            api_key=_required(kucoin, "api_key", "kucoin"),
            api_secret=_required(kucoin, "api_secret", "kucoin"),
            api_passphrase=_required(kucoin, "api_passphrase", "kucoin"),
            site_type=str(kucoin.get("site_type", "global")).strip() or "global",
        ),
        monitor=MonitorConfig(
            interval_seconds=max(10, int(monitor.get("interval_seconds", 60))),
            stale_after_seconds=max(30, int(monitor.get("stale_after_seconds", 180))),
        ),
        feishu=feishu_config,
        hedge=HedgeConfig(
            quote_currency=quote_currency,
            matched_threshold_percent=float(hedge.get("matched_threshold_percent", 1)),
            warning_threshold_percent=float(hedge.get("warning_threshold_percent", 5)),
            dust_value_usdt=float(hedge.get("dust_value_usdt", 1)),
            aliases=normalized_aliases,
            excluded_assets=excluded_assets,
        ),
        trading=TradingConfig(
            enabled=bool(trading.get("enabled", False)),
            cancel_open_orders_before_close=bool(
                trading.get("cancel_open_orders_before_close", True)
            ),
            verification_delay_seconds=max(
                0.2, float(trading.get("verification_delay_seconds", 1.5))
            ),
        ),
        config_path=config_path,
        database_path=database_path,
    )
