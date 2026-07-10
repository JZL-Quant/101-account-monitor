import asyncio
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

import pandas as pd

from ..runtime_logging import setup_runtime_logger


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME_LOGGER = setup_runtime_logger("portfolio_nav_fetcher")


def read_minute_snapshot_file(minute_snapshot_file):
    if not os.path.exists(minute_snapshot_file):
        RUNTIME_LOGGER.warning("minute snapshot file %s does not exist", minute_snapshot_file)
        return None
    return pd.read_csv(minute_snapshot_file, parse_dates=["timestamp"])


class BaseExchangeAccount(ABC):
    supported_account_types = ()

    @classmethod
    def from_account_info(cls, name: str, account_info: dict):
        return cls(
            name,
            account_info["key"],
            account_info["secret"],
            account_info["initial_unit"],
            account_info["account_type"],
            account_info.get("ccy", "USDT"),
            account_info.get("exchange", "Exchange"),
            account_info.get("minute_snapshot_file"),
        )

    def __init__(
        self,
        name,
        api_key,
        secret,
        initial_unit,
        account_type,
        ccy="USDT",
        exchange="Exchange",
        minute_snapshot_file=None,
    ):
        self.name = name
        self.api_key = api_key
        self.secret = secret
        self.initial_unit = initial_unit
        self.account_type = account_type
        self.ccy = (ccy or "USDT").upper()
        self.exchange = (exchange or "Exchange").strip()
        self.exchange_id = self.exchange.lower()

        exchange_label = re.sub(r"\W+", "_", self.exchange[:1].upper() + self.exchange[1:]).strip("_")
        exchange_label = exchange_label or "Exchange"
        ccy_label = re.sub(r"\W+", "_", self.ccy).strip("_") or "USDT"
        default_snapshot_dir = os.getenv("MONITOR_MINUTE_SNAPSHOT_DIR", os.path.join(BASE_DIR, "minute_snapshots"))
        os.makedirs(default_snapshot_dir, exist_ok=True)
        self.minute_snapshot_file = minute_snapshot_file or os.path.join(
            default_snapshot_dir,
            f"{exchange_label}_{name}_{ccy_label}_minute_snapshot.csv",
        )

        if self.supported_account_types and account_type not in self.supported_account_types:
            raise ValueError(f"Unsupported account_type: {account_type}")

    def get_last_actual_equity_from_csv(self):
        df = read_minute_snapshot_file(self.minute_snapshot_file)
        if df is None:
            RUNTIME_LOGGER.warning("[%s] 无法读取快照文件，回退 actual_equity=0.0", self.name)
            return 0.0
        try:
            last_equity = float(df.iloc[-1]["actual_equity"])
            RUNTIME_LOGGER.info("[%s] 读取最后一条 actual_equity: %.8f", self.name, last_equity)
            return last_equity
        except Exception as exc:
            RUNTIME_LOGGER.error("[%s] 读取 actual_equity 失败: %s", self.name, exc)
            return 0.0

    @abstractmethod
    async def get_actual_equity(self):
        raise NotImplementedError

    async def request(self, *args, **kwargs):
        raise NotImplementedError("Exchange-specific request implementation is required")

    async def fetch_tickers(self):
        raise NotImplementedError

    async def fetch_account_assets(self, account_type: str):
        raise NotImplementedError

    async def fetch_rwusd_account(self):
        raise NotImplementedError

    def get_latest_total_unit(self):
        try:
            df = pd.read_csv(self.minute_snapshot_file)
            return df["total_unit"].iloc[-1]
        except (FileNotFoundError, IndexError):
            return self.initial_unit

    def record_minute_snapshot(self, actual_equity, total_unit):
        net_value = actual_equity / total_unit
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(os.path.dirname(self.minute_snapshot_file), exist_ok=True)

        df = pd.DataFrame(
            [[timestamp, actual_equity, total_unit, net_value, "", ""]],
            columns=[
                "timestamp",
                "actual_equity",
                "total_unit",
                "net_value",
                "dividend_amount",
                "subscription_amount",
            ],
        )

        df.to_csv(
            self.minute_snapshot_file,
            mode="a",
            header=not os.path.exists(self.minute_snapshot_file),
            index=False,
        )
        RUNTIME_LOGGER.debug(
            "[%s] 📊 记录快照: %s, 净值: %.8f, 份数: %.2f",
            self.name,
            timestamp,
            net_value,
            total_unit,
        )

    async def get_net_value(self):
        total_unit = self.get_latest_total_unit()
        try:
            actual_equity = await self.get_actual_equity()
            self.record_minute_snapshot(actual_equity, total_unit)
        except Exception:
            RUNTIME_LOGGER.exception("[%s] 获取净值失败", self.name)

    async def update_net_value(self):
        while True:
            await self.wait_until_next_minute()
            await self.get_net_value()

    async def wait_until_next_minute(self):
        now = datetime.now()
        next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        await asyncio.sleep((next_minute - now).total_seconds())

    def update_post_event_net_values(self, event_time, new_total_unit):
        try:
            df = pd.read_csv(self.minute_snapshot_file, parse_dates=["timestamp"])
            event_time = pd.to_datetime(event_time)
            mask = df["timestamp"] > event_time
            if not mask.any():
                RUNTIME_LOGGER.warning(
                    "[%s] ⚠️ 事件时间 %s 后没有可更新的数据",
                    self.name,
                    event_time,
                )
                return
            df.loc[mask, "total_unit"] = new_total_unit
            df.loc[mask, "net_value"] = df.loc[mask, "actual_equity"] / new_total_unit
            df.to_csv(self.minute_snapshot_file, index=False)
            RUNTIME_LOGGER.info(
                "[%s] ✅ 已更新事件 %s 后的净值和份数（共 %s 行）",
                self.name,
                event_time,
                int(mask.sum()),
            )
        except Exception:
            RUNTIME_LOGGER.exception("[%s] ❌ 更新分红/申购后净值失败", self.name)

    async def handle_dividend_pro(self, dividend_date, dividend_amount):
        try:
            actual_equity = await self.get_actual_equity()
        except Exception:
            RUNTIME_LOGGER.exception("[%s] 获取账户信息失败", self.name)
            return

        try:
            df = pd.read_csv(self.minute_snapshot_file, parse_dates=["timestamp"])
        except FileNotFoundError:
            RUNTIME_LOGGER.warning("[%s] ⚠️ 快照文件未找到，无法进行分红处理", self.name)
            return

        dividend_date = pd.to_datetime(dividend_date).date()
        day_data = df[df["timestamp"].dt.date == dividend_date]
        if day_data.empty:
            RUNTIME_LOGGER.warning("[%s] ⚠️ 找不到 %s 的记录数据", self.name, dividend_date)
            return

        day_data["net_value_change"] = day_data["net_value"].diff().abs()
        max_row = day_data.loc[day_data["net_value_change"].idxmax()]

        dividend_time = max_row["timestamp"]
        net_value_before = df[df["timestamp"] < dividend_time]["net_value"].iloc[-1]

        reduced_unit = dividend_amount / net_value_before
        new_total_unit = max_row["total_unit"] - reduced_unit
        new_net_value = actual_equity / new_total_unit

        df.loc[df["timestamp"] == dividend_time, "total_unit"] = new_total_unit
        df.loc[df["timestamp"] == dividend_time, "net_value"] = new_net_value
        df.loc[df["timestamp"] == dividend_time, "dividend_amount"] = dividend_amount
        df.to_csv(self.minute_snapshot_file, index=False)

        RUNTIME_LOGGER.info(
            "[%s] 💵 分红 %s 于 %s，更新份数: %.2f, 净值: %.8f",
            self.name,
            dividend_amount,
            dividend_time,
            new_total_unit,
            new_net_value,
        )

        self.update_post_event_net_values(dividend_time, new_total_unit)

    async def handle_subscription_pro(self, subscription_date, subscription_amount):
        try:
            actual_equity = await self.get_actual_equity()
        except Exception:
            RUNTIME_LOGGER.exception("[%s] 获取账户信息失败", self.name)
            return

        try:
            df = pd.read_csv(self.minute_snapshot_file, parse_dates=["timestamp"])
        except FileNotFoundError:
            RUNTIME_LOGGER.warning("[%s] ⚠️ 快照文件未找到，无法进行申购处理", self.name)
            return

        subscription_date = pd.to_datetime(subscription_date).date()
        day_data = df[df["timestamp"].dt.date == subscription_date]
        if day_data.empty:
            RUNTIME_LOGGER.warning("[%s] ⚠️ 找不到 %s 的记录数据", self.name, subscription_date)
            return

        day_data["net_value_change"] = day_data["net_value"].diff().abs()
        max_row = day_data.loc[day_data["net_value_change"].idxmax()]

        subscription_time = max_row["timestamp"]
        net_value_before = df[df["timestamp"] < subscription_time]["net_value"].iloc[-1]

        added_unit = subscription_amount / net_value_before
        new_total_unit = max_row["total_unit"] + added_unit
        new_net_value = actual_equity / new_total_unit

        df.loc[df["timestamp"] == subscription_time, "total_unit"] = new_total_unit
        df.loc[df["timestamp"] == subscription_time, "net_value"] = new_net_value
        df.loc[df["timestamp"] == subscription_time, "subscription_amount"] = subscription_amount
        df.to_csv(self.minute_snapshot_file, index=False)

        RUNTIME_LOGGER.info(
            "[%s] 💸 申购 %s 于 %s，更新份数: %.2f, 净值: %.8f",
            self.name,
            subscription_amount,
            subscription_time,
            new_total_unit,
            new_net_value,
        )

        self.update_post_event_net_values(subscription_time, new_total_unit)
