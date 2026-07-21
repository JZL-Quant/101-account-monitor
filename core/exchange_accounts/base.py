import asyncio
import csv
import inspect
import math
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

import pandas as pd

from ..runtime_logging import setup_runtime_logger
from config.settings import MINUTE_SNAPSHOT_DIR, MIN_VALID_CALCULATION_VALUE


RUNTIME_LOGGER = setup_runtime_logger("portfolio_nav_fetcher")

SNAPSHOT_COLUMNS = [
    "timestamp", "actual_equity", "total_unit", "net_value",
    "dividend_amount", "interest_deduction", "withdraw_amount",
    "subscription_amount",
]


def is_valid_calculation_value(value, *, denominator=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(number):
        return False
    return not denominator or abs(number) > MIN_VALID_CALCULATION_VALUE


def read_minute_snapshot_file(minute_snapshot_file):
    if not os.path.exists(minute_snapshot_file):
        RUNTIME_LOGGER.warning("minute snapshot file %s does not exist", minute_snapshot_file)
        return None
    df = pd.read_csv(minute_snapshot_file)
    timestamps = parse_snapshot_timestamps(df, minute_snapshot_file)
    df["timestamp"] = timestamps
    return df


def parse_snapshot_timestamps(df, snapshot_file):
    """Parse timestamps for calculations without changing the CSV text column."""
    timestamps = pd.to_datetime(df["timestamp"], format="mixed", errors="coerce")
    invalid_count = int(timestamps.isna().sum())
    if invalid_count:
        raise ValueError(
            f"快照文件 {snapshot_file} 中有 {invalid_count} 行 timestamp 无法解析"
        )
    return timestamps


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
        self._latest_total_unit = None
        self._snapshot_schema_checked = False
        # Snapshot appends and historical fund-event rewrites both update the
        # CSV and the in-memory unit cache. Keep them in one critical section.
        self._snapshot_lock = asyncio.Lock()

        exchange_label = re.sub(r"\W+", "_", self.exchange[:1].upper() + self.exchange[1:]).strip("_")
        exchange_label = exchange_label or "Exchange"
        ccy_label = re.sub(r"\W+", "_", self.ccy).strip("_") or "USDT"
        os.makedirs(MINUTE_SNAPSHOT_DIR, exist_ok=True)
        self.minute_snapshot_file = minute_snapshot_file or os.path.join(
            MINUTE_SNAPSHOT_DIR,
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

    async def validate_credentials(self):
        """Verify credentials and permissions required by this account type."""
        raise NotImplementedError

    async def request(self, *args, **kwargs):
        raise NotImplementedError("Exchange-specific request implementation is required")

    async def fetch_tickers(self):
        raise NotImplementedError

    async def fetch_account_assets(self, account_type: str):
        raise NotImplementedError

    async def fetch_rwusd_account(self):
        raise NotImplementedError

    async def close(self):
        """Close an exchange SDK client when the concrete account exposes one."""
        client = getattr(self, "client", None) or getattr(self, "api_client", None)
        close = getattr(client, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    def get_latest_total_unit(self):
        if self._latest_total_unit is not None:
            return self._latest_total_unit
        try:
            with open(self.minute_snapshot_file, "r", encoding="utf-8", newline="") as file:
                rows = csv.reader(file)
                header = next(rows)
                total_unit_index = header.index("total_unit")
                latest = None
                for row in rows:
                    if row and len(row) > total_unit_index and row[total_unit_index]:
                        latest = float(row[total_unit_index])
            self._latest_total_unit = latest if latest is not None else self.initial_unit
        except (FileNotFoundError, StopIteration, ValueError):
            self._latest_total_unit = self.initial_unit
        return self._latest_total_unit

    def _ensure_snapshot_schema(self):
        if self._snapshot_schema_checked or not os.path.exists(self.minute_snapshot_file):
            self._snapshot_schema_checked = True
            return
        existing_header = list(pd.read_csv(self.minute_snapshot_file, nrows=0).columns)
        missing_columns = [column for column in SNAPSHOT_COLUMNS if column not in existing_header]
        if missing_columns:
            existing = pd.read_csv(self.minute_snapshot_file)
            for column in missing_columns:
                existing[column] = ""
            existing[SNAPSHOT_COLUMNS].to_csv(self.minute_snapshot_file, index=False)
        self._snapshot_schema_checked = True

    def record_minute_snapshot(self, actual_equity, total_unit):
        if not is_valid_calculation_value(actual_equity):
            raise ValueError(f"{self.name} actual_equity 不是有限数")
        if not is_valid_calculation_value(total_unit, denominator=True):
            raise ValueError(f"{self.name} total_unit 小于等于有效阈值")
        net_value = actual_equity / total_unit
        if not is_valid_calculation_value(net_value):
            raise ValueError(f"{self.name} net_value 不是有限数")
        # 快照属于调度触发的分钟，而不是 API 请求完成的具体秒数。
        # 统一落在分钟边界，避免请求耗时让时间戳出现 :21、:37 等偏移。
        timestamp = datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(os.path.dirname(self.minute_snapshot_file), exist_ok=True)

        file_exists = os.path.exists(self.minute_snapshot_file) and os.path.getsize(self.minute_snapshot_file) > 0
        self._ensure_snapshot_schema()

        df = pd.DataFrame(
            [[timestamp, actual_equity, total_unit, net_value, "", "", "", ""]],
            columns=SNAPSHOT_COLUMNS,
        )

        df.to_csv(
            self.minute_snapshot_file,
            mode="a",
            header=not file_exists,
            index=False,
        )
        self._latest_total_unit = total_unit
        RUNTIME_LOGGER.debug(
            "[%s] 📊 记录快照: %s, 净值: %.8f, 份数: %.2f",
            self.name,
            timestamp,
            net_value,
            total_unit,
        )

    async def get_net_value(self):
        try:
            actual_equity = await self.get_actual_equity()
            # A fund event may change the unit while the network request is in
            # flight, so read it only after the request has completed.
            async with self._snapshot_lock:
                total_unit = self.get_latest_total_unit()
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
            if not is_valid_calculation_value(new_total_unit, denominator=True):
                raise ValueError("事件后的 total_unit 小于等于有效阈值")
            df = pd.read_csv(self.minute_snapshot_file)
            timestamps = parse_snapshot_timestamps(df, self.minute_snapshot_file)
            event_time = pd.to_datetime(event_time)
            mask = timestamps > event_time
            if not mask.any():
                RUNTIME_LOGGER.warning(
                    "[%s] ⚠️ 事件时间 %s 后没有可更新的数据",
                    self.name,
                    event_time,
                )
                return
            new_net_values = pd.to_numeric(df.loc[mask, "actual_equity"], errors="coerce") / new_total_unit
            if not new_net_values.map(is_valid_calculation_value).all():
                raise ValueError("事件后的净值包含 NaN 或 Inf")
            df.loc[mask, "total_unit"] = new_total_unit
            df.loc[mask, "net_value"] = new_net_values
            df.to_csv(self.minute_snapshot_file, index=False)
            self._latest_total_unit = new_total_unit
            RUNTIME_LOGGER.info(
                "[%s] ✅ 已更新事件 %s 后的净值和份数（共 %s 行）",
                self.name,
                event_time,
                int(mask.sum()),
            )
        except Exception:
            RUNTIME_LOGGER.exception("[%s] ❌ 更新分红/申购后净值失败", self.name)

    async def handle_dividend_pro(self, dividend_date, dividend_amount):
        return await self.handle_outflow(dividend_date, dividend_amount, "dividend_amount", "分红")

    async def handle_interest_deduction_pro(self, deduction_date, deduction_amount):
        return await self.handle_outflow(deduction_date, deduction_amount, "interest_deduction", "扣息")

    async def handle_withdrawal_pro(self, withdrawal_date, withdrawal_amount):
        return await self.handle_outflow(withdrawal_date, withdrawal_amount, "withdraw_amount", "赎回")

    async def handle_fund_changes(self, changes):
        async with self._snapshot_lock:
            return await self._handle_fund_changes_locked(changes)

    async def _handle_fund_changes_locked(self, changes):
        """Apply incremental fund classifications at explicit snapshot timestamps."""
        if not changes:
            raise ValueError("没有需要保存的资金变动")

        try:
            df = pd.read_csv(self.minute_snapshot_file)
        except FileNotFoundError:
            raise ValueError(f"找不到 {self.name} 的快照文件，无法处理资金变动")
        for column in SNAPSHOT_COLUMNS:
            if column not in df.columns:
                df[column] = ""
        df = df[SNAPSHOT_COLUMNS]
        timestamps = parse_snapshot_timestamps(df, self.minute_snapshot_file)
        equity = pd.to_numeric(df["actual_equity"], errors="coerce")

        indexed_changes = []
        seen_indexes = set()
        for change in changes:
            event_time = pd.to_datetime(change["event_timestamp"])
            matching_indexes = df.index[timestamps == event_time]
            if len(matching_indexes) != 1:
                raise ValueError(f"无法唯一定位资金变动时刻 {change['event_timestamp']}")
            event_index = int(matching_indexes[0])
            if event_index == 0:
                raise ValueError(f"{event_time} 之前没有快照，无法处理资金变动")
            if event_index in seen_indexes:
                raise ValueError(f"资金变动时刻 {event_time} 重复提交")
            seen_indexes.add(event_index)
            indexed_changes.append((event_index, event_time, change))

        results = []
        for event_index, event_time, change in sorted(indexed_changes):
            amounts = {
                "subscription_amount": float(change.get("subscription_amount", 0) or 0),
                "dividend_amount": float(change.get("dividend_amount", 0) or 0),
                "interest_deduction": float(change.get("interest_deduction", 0) or 0),
                "withdrawal_amount": float(change.get("withdrawal_amount", 0) or 0),
            }
            if not all(is_valid_calculation_value(value) and value >= 0 for value in amounts.values()):
                raise ValueError(f"{event_time} 包含无效的资金变动金额")
            if not any(amounts.values()):
                raise ValueError(f"{event_time} 没有填写资金变动金额")

            observed_change = float(equity.iloc[event_index] - equity.iloc[event_index - 1])
            outflow = (
                amounts["dividend_amount"]
                + amounts["interest_deduction"]
                + amounts["withdrawal_amount"]
            )
            if observed_change > 0 and outflow > 0:
                raise ValueError(f"{event_time} 是资金流入，只能填写申购金额")
            if observed_change < 0 and amounts["subscription_amount"] > 0:
                raise ValueError(f"{event_time} 是资金流出，不能填写申购金额")

            net_value_before = pd.to_numeric(
                pd.Series([df.at[event_index - 1, "net_value"]]), errors="coerce"
            ).iloc[0]
            if not is_valid_calculation_value(net_value_before, denominator=True):
                raise ValueError(f"{event_time} 之前的净值无效，无法处理资金变动")

            unit_delta = (amounts["subscription_amount"] - outflow) / float(net_value_before)
            future_units = pd.to_numeric(df.loc[event_index:, "total_unit"], errors="coerce") + unit_delta
            if not future_units.map(lambda value: is_valid_calculation_value(value, denominator=True)).all():
                raise ValueError(f"{event_time} 处理后的总份额无效")
            df.loc[event_index:, "total_unit"] = future_units

            column_amounts = {
                "subscription_amount": amounts["subscription_amount"],
                "dividend_amount": amounts["dividend_amount"],
                "interest_deduction": amounts["interest_deduction"],
                "withdraw_amount": amounts["withdrawal_amount"],
            }
            cumulative = {}
            for column, increment in column_amounts.items():
                existing = pd.to_numeric(pd.Series([df.at[event_index, column]]), errors="coerce").iloc[0]
                existing = 0.0 if pd.isna(existing) else float(existing)
                cumulative[column] = existing + increment
                df.at[event_index, column] = cumulative[column]

            future_equity = pd.to_numeric(df.loc[event_index:, "actual_equity"], errors="coerce")
            future_nav = future_equity / pd.to_numeric(df.loc[event_index:, "total_unit"], errors="coerce")
            if not future_nav.map(is_valid_calculation_value).all():
                raise ValueError(f"{event_time} 处理后的净值包含 NaN 或 Inf")
            df.loc[event_index:, "net_value"] = future_nav
            results.append({
                "event_timestamp": str(df.at[event_index, "timestamp"]),
                "total_unit": float(df.at[event_index, "total_unit"]),
                "net_value": float(df.at[event_index, "net_value"]),
                **cumulative,
            })

        df.to_csv(self.minute_snapshot_file, index=False)
        self._latest_total_unit = float(pd.to_numeric(df.iloc[-1]["total_unit"], errors="raise"))
        return results

    async def handle_outflow(self, event_date, amount, csv_column, action_label):
        async with self._snapshot_lock:
            return await self._handle_outflow_locked(event_date, amount, csv_column, action_label)

    async def _handle_outflow_locked(self, event_date, amount, csv_column, action_label):
        if amount <= 0:
            raise ValueError(f"{action_label}金额必须大于 0")

        try:
            df = pd.read_csv(self.minute_snapshot_file)
        except FileNotFoundError:
            raise ValueError(f"找不到 {self.name} 的快照文件，无法处理{action_label}")
        for column in SNAPSHOT_COLUMNS:
            if column not in df.columns:
                df[column] = ""
        df = df[SNAPSHOT_COLUMNS]

        timestamps = parse_snapshot_timestamps(df, self.minute_snapshot_file)

        event_date = pd.to_datetime(event_date).date()
        day_data = df[timestamps.dt.date == event_date].copy()
        if day_data.empty:
            raise ValueError(f"找不到 {event_date} 的快照数据")

        day_data["net_value_change"] = day_data["net_value"].diff().abs()
        max_row = day_data.loc[day_data["net_value_change"].idxmax()]

        event_index = max_row.name
        event_time = timestamps.loc[event_index]
        prior_rows = df[timestamps < event_time]
        if prior_rows.empty:
            raise ValueError(f"{event_time} 之前没有净值数据，无法处理{action_label}")
        net_value_before = prior_rows["net_value"].iloc[-1]
        if not is_valid_calculation_value(net_value_before, denominator=True):
            raise ValueError(f"{event_time} 之前的净值无效，无法处理{action_label}")

        reduced_unit = amount / net_value_before
        new_total_unit = max_row["total_unit"] - reduced_unit
        if not is_valid_calculation_value(new_total_unit, denominator=True):
            raise ValueError(
                f"{action_label}金额过大，处理后的总份额必须大于 "
                f"{MIN_VALID_CALCULATION_VALUE:g}"
            )
        event_actual_equity = max_row["actual_equity"]
        if not is_valid_calculation_value(event_actual_equity):
            raise ValueError(f"{action_label}事件行的 actual_equity 无效")
        new_net_value = float(event_actual_equity) / new_total_unit
        if not is_valid_calculation_value(new_net_value):
            raise ValueError(f"{action_label}后的净值无效")

        df.loc[event_index, "total_unit"] = new_total_unit
        df.loc[event_index, "net_value"] = new_net_value
        df.loc[event_index, csv_column] = amount
        df.to_csv(self.minute_snapshot_file, index=False)

        RUNTIME_LOGGER.info(
            "[%s] %s %s 于 %s，更新份数: %.2f, 净值: %.8f",
            self.name,
            action_label,
            amount,
            event_time,
            new_total_unit,
            new_net_value,
        )

        self.update_post_event_net_values(event_time, new_total_unit)
        return event_time

    async def handle_subscription_pro(self, subscription_date, subscription_amount):
        async with self._snapshot_lock:
            return await self._handle_subscription_locked(subscription_date, subscription_amount)

    async def _handle_subscription_locked(self, subscription_date, subscription_amount):
        if subscription_amount <= 0:
            raise ValueError("申购金额必须大于 0")

        try:
            df = pd.read_csv(self.minute_snapshot_file)
        except FileNotFoundError:
            raise ValueError(f"找不到 {self.name} 的快照文件，无法处理申购")

        timestamps = parse_snapshot_timestamps(df, self.minute_snapshot_file)
        subscription_date = pd.to_datetime(subscription_date).date()
        day_data = df[timestamps.dt.date == subscription_date].copy()
        if day_data.empty:
            raise ValueError(f"找不到 {subscription_date} 的快照数据")

        day_data["net_value_change"] = day_data["net_value"].diff().abs()
        max_row = day_data.loc[day_data["net_value_change"].idxmax()]

        subscription_index = max_row.name
        subscription_time = timestamps.loc[subscription_index]
        prior_rows = df[timestamps < subscription_time]
        if prior_rows.empty:
            raise ValueError(f"{subscription_time} 之前没有净值数据，无法处理申购")
        net_value_before = prior_rows["net_value"].iloc[-1]
        if not is_valid_calculation_value(net_value_before, denominator=True):
            raise ValueError(f"{subscription_time} 之前的净值无效，无法处理申购")

        added_unit = subscription_amount / net_value_before
        new_total_unit = max_row["total_unit"] + added_unit
        if not is_valid_calculation_value(new_total_unit, denominator=True):
            raise ValueError("申购后的 total_unit 无效")
        event_actual_equity = max_row["actual_equity"]
        if not is_valid_calculation_value(event_actual_equity):
            raise ValueError("申购事件行的 actual_equity 无效")
        new_net_value = float(event_actual_equity) / new_total_unit
        if not is_valid_calculation_value(new_net_value):
            raise ValueError("申购后的净值无效")

        df.loc[subscription_index, "total_unit"] = new_total_unit
        df.loc[subscription_index, "net_value"] = new_net_value
        df.loc[subscription_index, "subscription_amount"] = subscription_amount
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
        return subscription_time
