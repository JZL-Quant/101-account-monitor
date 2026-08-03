import pandas as pd
import asyncio
import os
import math
from datetime import datetime, timedelta, timezone
from core.account_registry import get_account_registry
from core.bigquery_returns import (
    build_binance_return_rows,
    completed_reference_date,
    merge_rows_to_bigquery,
)
from core.daily_report import build_daily_report
from core.metrics import AccountMetricsStore
from core.feishu import NOTIFIER
from core.feishu.daily_report_card import build_daily_detail_cards
from core.feishu.equity_change_card import build_large_equity_change_card
from core.feishu.performance_summary_card import build_return_performance_card
from core.large_equity_changes import find_new_large_equity_changes
from core.runtime_logging import cleanup_runtime_logs, setup_runtime_logger
from core.scheduler import MonitorScheduler
from config.settings import (
    ACCOUNTS_CONFIG_PATH,
    BIGQUERY_CREDENTIALS_PATH,
    BIGQUERY_DATASET,
    BIGQUERY_PROJECT_ID,
    BIGQUERY_RETURN_ENABLED,
    BIGQUERY_RETURN_TABLE,
    MIN_VALID_CALCULATION_VALUE,
    PROJECT_ROOT,
    RUNTIME_LOG_RETENTION_DAYS,
)

# 更改工作目录为文件所在目录
BASE_DIR = str(PROJECT_ROOT)
os.chdir(BASE_DIR)


RUNTIME_LOGGER = setup_runtime_logger("account_monitor")

def safe_metric_val(val):
    """把指标值转为可计算的 float；None、NaN、Inf 统一视为无效值。"""
    try:
        if val is None:
            return None
        float_val = float(val)
        if math.isnan(float_val) or math.isinf(float_val):
            return None
        return float_val
    except Exception:
        return None


def is_valid_denominator(value):
    """分母必须是有限数，且绝对值大于统一的极小值阈值。"""
    safe_value = safe_metric_val(value)
    return safe_value is not None and abs(safe_value) > MIN_VALID_CALCULATION_VALUE

# ---------- 动态创建 Prometheus 指标 ----------
account_registry = get_account_registry(str(ACCOUNTS_CONFIG_PATH))
accounts = account_registry.local_accounts()
account_metrics = AccountMetricsStore(accounts)

def register_monitor_account(account_name: str, account_info: dict):
    """将运行时新增账户同时加入调度集合和 Prometheus 注册表。"""
    account_metrics.add_account(account_name, account_info)
    accounts[account_name] = account_info

def unregister_monitor_account(account_name: str):
    """将归档账户移出报告、调度集合和 Prometheus 注册表。"""
    accounts.pop(account_name, None)
    account_metrics.remove_account(account_name)

def read_minute_snapshot_file(minute_snapshot_file):
    """读取账户分钟快照 CSV，并把 timestamp 解析为时间列。"""
    if not os.path.exists(minute_snapshot_file):
        RUNTIME_LOGGER.warning("minute snapshot file %s does not exist", minute_snapshot_file)
        return None
    df = pd.read_csv(minute_snapshot_file)
    if "timestamp" not in df.columns:
        raise ValueError(f"快照文件 {minute_snapshot_file} 缺少 timestamp 列")

    # 历史文件中同时存在 `2026/4/28 9:26` 和
    # `2026-07-16 08:43:21` 等格式。parse_dates 在 pandas 2.x 会按首行
    # 推断单一格式，遇到混合格式时可能把整列保留为 object/string。
    timestamps = pd.to_datetime(df["timestamp"], format="mixed", errors="coerce")
    invalid_count = int(timestamps.isna().sum())
    if invalid_count:
        invalid_rows = (timestamps[timestamps.isna()].index + 2).tolist()
        row_preview = ", ".join(map(str, invalid_rows[:5]))
        raise ValueError(
            f"快照文件 {minute_snapshot_file} 中有 {invalid_count} 行 timestamp 无法解析"
            f"（CSV 行号示例: {row_preview}）"
        )
    df["timestamp"] = timestamps
    return df

async def calculate_simple_annualized_return(data, period_days):
    """按区间首尾净值计算简单年化收益率，返回小数形式。"""
    if len(data) < period_days:
        return None
    start_value = data.iloc[0]
    end_value = data.iloc[-1]
    if period_days <= 0 or not is_valid_denominator(start_value) or safe_metric_val(end_value) is None:
        return None
    total_return = (end_value / start_value) - 1
    simple_annualized_return = total_return / period_days * 365
    return safe_metric_val(simple_annualized_return)

async def get_daily_median_net_value(account_name, account_info, snapshot_df=None):
    """取每日北京时间 17-18 点的净值中位数，作为日报收益计算基准。"""
    try:
        df = snapshot_df if snapshot_df is not None else read_minute_snapshot_file(account_info["minute_snapshot_file"])

        if df is None or df.empty:
            RUNTIME_LOGGER.warning("%s 数据为空", account_name)
            return None

        # 检查是否包含所需的列
        if 'timestamp' not in df.columns or 'net_value' not in df.columns:
            RUNTIME_LOGGER.warning("%s 快照文件缺少 timestamp 或 net_value 列", account_name)
            return None

        df['date'] = df['timestamp'].dt.date
        df['hour'] = df['timestamp'].dt.hour

        # timestamp 按 UTC 存储，UTC 9-10 点对应北京时间 17-18 点。
        df_filtered = df[(df['hour'] >= 9) & (df['hour'] < 10)]

        # 如果过滤后数据为空，直接返回 None
        if df_filtered.empty:
            RUNTIME_LOGGER.warning("%s 没有找到符合时间区间的数据", account_name)
            return None

        daily_median = df_filtered.groupby('date')['net_value'].median()
        return daily_median

    except Exception as e:
        RUNTIME_LOGGER.exception("%s 读取快照文件失败", account_name)
        return None

async def get_daily_median_actual_equity(account_name, account_info, snapshot_df=None):
    """取最近一个有效交易日北京时间 17-18 点的实际权益中位数。"""
    try:
        df = snapshot_df if snapshot_df is not None else read_minute_snapshot_file(account_info["minute_snapshot_file"])

        if df is None or df.empty:
            RUNTIME_LOGGER.warning("%s 数据为空", account_name)
            return None

        # 检查是否包含所需的列
        if 'timestamp' not in df.columns or 'actual_equity' not in df.columns:
            RUNTIME_LOGGER.warning("%s 快照文件缺少 timestamp 或 actual_equity 列", account_name)
            return None

        df['date'] = df['timestamp'].dt.date
        df['hour'] = df['timestamp'].dt.hour

        # timestamp 按 UTC 存储，UTC 9-10 点对应北京时间 17-18 点。
        df_filtered = df[(df['hour'] >= 9) & (df['hour'] < 10)]

        # 如果过滤后数据为空，直接返回 None
        if df_filtered.empty:
            RUNTIME_LOGGER.warning("%s 没有找到符合时间区间的数据", account_name)
            return None

        daily_median = df_filtered.groupby('date')['actual_equity'].median()
        daily_median = daily_median.iloc[-1]
        return daily_median

    except Exception as e:
        RUNTIME_LOGGER.exception("%s 读取快照文件失败", account_name)
        return None

async def update_actual_equity(account_name, account_info, snapshot_df=None):
    """用最新分钟快照里的实际权益更新 Prometheus 指标。"""
    try:
        df = snapshot_df if snapshot_df is not None else read_minute_snapshot_file(account_info["minute_snapshot_file"])
        if df is None or df.empty:
            account_metrics.set(account_name, "actual_equity", float('nan'))
            return

        latest_row = df.iloc[-1]
        actual_equity_value = latest_row['actual_equity']

        if safe_metric_val(actual_equity_value) is not None:
            account_metrics.set(account_name, 'actual_equity', actual_equity_value)
            RUNTIME_LOGGER.info("%s 实际净值更新成功: %s", account_name, actual_equity_value)
        else:
            account_metrics.set(account_name, 'actual_equity', float('nan'))
            RUNTIME_LOGGER.warning("%s 实际净值为空，已设置为 NaN", account_name)

    except Exception as e:
        RUNTIME_LOGGER.exception("%s 无法更新实际净值", account_name)

async def update_report_actual_equity(account_name, account_info, snapshot_df=None):
    """用日报口径的实际权益中位数刷新指标缓存。"""
    actual_equity = await get_daily_median_actual_equity(account_name, account_info, snapshot_df)
    if safe_metric_val(actual_equity) is None:
        account_metrics.set(account_name, "report_actual_equity", float("nan"))
        RUNTIME_LOGGER.warning("%s 日报实际权益中位数无效，已设置为 NaN", account_name)
        return
    account_metrics.set(account_name, "report_actual_equity", actual_equity)

async def calculate_annualized_return_1m(account_name, account_info, snapshot_df=None):
    """用最新净值和约 24 小时前净值计算单日简单年化收益率。"""
    try:
        df = snapshot_df if snapshot_df is not None else read_minute_snapshot_file(account_info["minute_snapshot_file"])
        if df is None or df.empty:
            account_metrics.set(account_name, "annualized_return_1m", float('nan'))
            return

        latest = df.iloc[-1]
        current_time = latest['timestamp']

        target_time = current_time - pd.Timedelta(days=1)

        # 取目标时间之前最近的一条快照，容忍采样时间不完全对齐。
        df_before = df[df['timestamp'] <= target_time]
        if df_before.empty:
            account_metrics.set(account_name, "annualized_return_1m", float('nan'))
            return

        previous = df_before.iloc[-1]

        if safe_metric_val(latest['net_value']) is not None and is_valid_denominator(previous['net_value']):
            period_return = (latest['net_value'] / previous['net_value']) - 1
            annualized = period_return * 365  # 一天年化
            annualized_percent = safe_metric_val(annualized * 100)
            if annualized_percent is not None:
                account_metrics.set(account_name, "annualized_return_1m", annualized_percent)
                RUNTIME_LOGGER.info("%s 24小时年化收益率更新成功: %.6f%%", account_name, annualized_percent)
                return
            account_metrics.set(account_name, "annualized_return_1m", float('nan'))
        else:
            account_metrics.set(account_name, "annualized_return_1m", float('nan'))
            RUNTIME_LOGGER.warning("%s 无法计算24小时年化收益率，已设置为 NaN", account_name)

    except Exception as e:
        RUNTIME_LOGGER.exception("%s 无法计算24小时年化收益率", account_name)

async def calculate_annualized_return_1h(account_name, account_info, snapshot_df=None):
    """用当前 1 小时和昨日同窗口净值中位数计算 24 小时简单年化收益率。"""
    try:
        df = snapshot_df if snapshot_df is not None else read_minute_snapshot_file(account_info["minute_snapshot_file"])
        if df is None or df.empty:
            account_metrics.set(account_name, "annualized_return_1h", float('nan'))
            return

        latest = df.iloc[-1]
        current_time = latest['timestamp']

        one_hour_ago = current_time - pd.Timedelta(hours=1)
        current_window = df[(df['timestamp'] > one_hour_ago) & (df['timestamp'] <= current_time)]
        if current_window.empty:
            RUNTIME_LOGGER.warning("%s 当前一小时内无净值数据", account_name)
            account_metrics.set(account_name, "annualized_return_1h", float('nan'))
            return
        current_median = current_window['net_value'].median()

        target_end = current_time - pd.Timedelta(days=1)
        target_start = target_end - pd.Timedelta(hours=1)
        previous_window = df[(df['timestamp'] > target_start) & (df['timestamp'] <= target_end)]
        if previous_window.empty:
            RUNTIME_LOGGER.warning("%s 前一天一小时内无净值数据", account_name)
            account_metrics.set(account_name, "annualized_return_1h", float('nan'))
            return
        previous_median = previous_window['net_value'].median()

        if safe_metric_val(current_median) is not None and is_valid_denominator(previous_median):
            period_return = (current_median / previous_median) - 1
            annualized = period_return * 365  # 线性年化
            annualized_percent = safe_metric_val(annualized * 100)
            if annualized_percent is not None:
                account_metrics.set(account_name, "annualized_return_1h", annualized_percent)
                RUNTIME_LOGGER.info("%s 24小时年化收益率更新成功（中位数版）: %.6f%%", account_name, annualized_percent)
                return
            account_metrics.set(account_name, "annualized_return_1h", float('nan'))
        else:
            account_metrics.set(account_name, "annualized_return_1h", float('nan'))
            RUNTIME_LOGGER.warning("%s 无法计算年化收益率（中位数版），已设置为 NaN", account_name)

    except Exception as e:
        RUNTIME_LOGGER.exception("%s 无法计算24小时年化收益率（中位数版）", account_name)

async def update_annualized_returns(account_name, account_info, snapshot_df=None):
    """基于每日净值中位数更新单日、7 日、30 日简单年化收益率。"""
    daily_median = await get_daily_median_net_value(account_name, account_info, snapshot_df)

    if daily_median is None or len(daily_median) == 0:
        RUNTIME_LOGGER.warning("%s 未找到有效的每日中位数净值数据", account_name)
        return
    # 当前时间（UTC）
    now_utc = datetime.now(timezone.utc)

    # 如果当前时间还未到北京时间18:00（即 UTC+8 → UTC 10:00）
    if now_utc.hour < 10:
        today = (now_utc.date() - timedelta(days=1))
    else:
        today = now_utc.date()

    # 数据不足完整窗口时不计算，对应指标会写入 NaN。
    last_7_days = daily_median.loc[daily_median.index >= today - timedelta(days=7)]
    last_30_days = daily_median.loc[daily_median.index >= today - timedelta(days=30)]
    last_24_hours = daily_median.loc[daily_median.index >= today - timedelta(days=1)]

    simple_annualized_return_7d = await calculate_simple_annualized_return(last_7_days, 7)
    simple_annualized_return_30d = await calculate_simple_annualized_return(last_30_days, 30)
    simple_annualized_return_24h = await calculate_simple_annualized_return(last_24_hours, 1)

    if simple_annualized_return_7d is not None:
        account_metrics.set(account_name, 'annualized_return_7d', simple_annualized_return_7d * 100)
        RUNTIME_LOGGER.info("%s 7日年化收益率更新成功: %.6f%%", account_name, simple_annualized_return_7d * 100)
    else:
        account_metrics.set(account_name, 'annualized_return_7d', float('nan'))
        RUNTIME_LOGGER.warning("%s 无法计算7日年化收益率，已设置为 NaN", account_name)

    if simple_annualized_return_30d is not None:
        account_metrics.set(account_name, 'annualized_return_30d', simple_annualized_return_30d * 100)
        RUNTIME_LOGGER.info("%s 30日年化收益率更新成功: %.6f%%", account_name, simple_annualized_return_30d * 100)
    else:
        account_metrics.set(account_name, 'annualized_return_30d', float('nan'))
        RUNTIME_LOGGER.warning("%s 无法计算30日年化收益率，已设置为 NaN", account_name)

    if simple_annualized_return_24h is not None:
        account_metrics.set(account_name, 'annualized_return_24h', simple_annualized_return_24h * 100)
        RUNTIME_LOGGER.info("%s 单日年化收益率更新成功: %.6f%%", account_name, simple_annualized_return_24h * 100)
    else:
        account_metrics.set(account_name, 'annualized_return_24h', float('nan'))
        RUNTIME_LOGGER.warning("%s 无法计算单日年化收益率，已设置为 NaN", account_name)

async def calculate_annualized_cumulative_return(account_name, account_info, snapshot_df=None):
    """用最新实际权益、累计分红和累计申购计算建仓以来的年化累计收益率。"""
    try:
        df = snapshot_df if snapshot_df is not None else read_minute_snapshot_file(account_info["minute_snapshot_file"])

        if df is None or df.empty:
            account_metrics.set(account_name, "cumulative_return", float('nan'))
            RUNTIME_LOGGER.warning("%s 快照文件为空", account_name)
            return None

        total_dividends = df['dividend_amount'].sum()
        total_subscriptions = df['subscription_amount'].sum()

        final_actual_equity = df['actual_equity'].iloc[-1]

        initial_date = df['timestamp'].iloc[0].date()

        total_days = (df['timestamp'].iloc[-1].date() - initial_date).days

        # initial_unit 作为建仓初始权益基准；剔除申购、分红、赎回和扣息等外部资金流后，
        # 计算建仓以来累计收益率。不要使用会随申购和赎回变化的 principal。
        initial_unit = account_info["initial_unit"]
        numerator = final_actual_equity + total_dividends - total_subscriptions
        if not is_valid_denominator(initial_unit) or safe_metric_val(numerator) is None:
            account_metrics.set(account_name, "cumulative_return", float('nan'))
            RUNTIME_LOGGER.warning("%s 累计收益率输入无效，已设置为 NaN", account_name)
            return
        cumulative_return_value = numerator / initial_unit - 1

        if total_days > 0:
            annualized_cumulative_return = cumulative_return_value * (365 / total_days)
            annualized_percent = safe_metric_val(annualized_cumulative_return * 100)
            account_metrics.set(
                account_name,
                "cumulative_return",
                annualized_percent if annualized_percent is not None else float('nan'),
            )
            RUNTIME_LOGGER.info("%s 年化累计收益率更新结果: %s%%", account_name, annualized_percent)
        else:
            account_metrics.set(account_name, "cumulative_return", float('nan'))

    except Exception as e:
        RUNTIME_LOGGER.exception("计算年化累计收益率失败")

async def calculate_post_dividend_annualized_return(account_name, account_info, snapshot_df=None):
    """从最近一次分红后的第二天开始，按每日净值中位数计算分红后年化收益率。"""
    try:
        daily_median = await get_daily_median_net_value(account_name, account_info, snapshot_df)

        if daily_median is None or len(daily_median) == 0:
            RUNTIME_LOGGER.warning("%s 未找到有效的每日中位数净值数据", account_name)
            return

        df = snapshot_df if snapshot_df is not None else read_minute_snapshot_file(account_info["minute_snapshot_file"])

        if df is None or df.empty:
            RUNTIME_LOGGER.warning("%s 快照文件为空", account_name)
            return None

        df_dividend = df[df['dividend_amount'].notna()]

        # 如果没有分红数据，返回 None
        if df_dividend.empty:
            RUNTIME_LOGGER.warning("%s 没有分红数据", account_name)
            return None

        last_dividend_date = df_dividend['timestamp'].max().date()
        RUNTIME_LOGGER.info("%s 最后分红日期: %s", account_name, last_dividend_date)

        # 获取分红后的数据（从分红后的第二天开始）
        start_date = last_dividend_date + timedelta(days=1)
        post_dividend_data = daily_median.loc[daily_median.index >= start_date]

        if len(post_dividend_data) < 2:
            RUNTIME_LOGGER.warning("%s 分红后数据不足两天，无法计算年化收益率", account_name)
            return None

        start_value = post_dividend_data.iloc[0]
        end_value = post_dividend_data.iloc[-1]
        days = (post_dividend_data.index[-1] - post_dividend_data.index[0]).days

        if days <= 0 or not is_valid_denominator(start_value) or safe_metric_val(end_value) is None:
            account_metrics.set(account_name, 'post_dividend_return', float('nan'))
            RUNTIME_LOGGER.warning("%s 分红后收益率输入无效，已设置为 NaN", account_name)
            return

        total_return = (end_value / start_value) - 1
        annualized_percent = safe_metric_val(total_return / days * 365 * 100)

        if annualized_percent is not None:
            account_metrics.set(account_name, 'post_dividend_return', annualized_percent)
            RUNTIME_LOGGER.info("%s 分红后年化收益率: %s%%", account_name, annualized_percent)
        else:
            account_metrics.set(account_name, 'post_dividend_return', float('nan'))
            RUNTIME_LOGGER.warning("%s 分红后年化收益率为 NaN", account_name)

    except Exception as e:
        RUNTIME_LOGGER.exception("计算分红后年化收益率失败 (%s)", account_name)

async def check_large_equity_changes():
    """分钟级任务：检查并合并发送本分钟发现的大额资金变动。"""
    detected_changes = []
    for account_name, account_info in list(accounts.items()):
        try:
            snapshot_df = read_minute_snapshot_file(account_info["minute_snapshot_file"])
            detected_changes.extend(
                find_new_large_equity_changes(account_name, account_info, snapshot_df)
            )
        except Exception:
            RUNTIME_LOGGER.exception(
                "[large_equity_change] account %s check failed", account_name
            )
    if detected_changes:
        await NOTIFIER.send_card(
            build_large_equity_change_card(detected_changes),
            route="equity_change_alert",
        )


async def update_metrics():
    """分钟级任务：更新最新实际权益、24 小时点对点收益和 1 小时中位数收益。"""
    for account_name, account_info in list(accounts.items()):
        try:
            snapshot_df = read_minute_snapshot_file(account_info["minute_snapshot_file"])
            if snapshot_df is None:
                snapshot_df = pd.DataFrame()
            await update_actual_equity(account_name, account_info, snapshot_df)
            await calculate_annualized_return_1m(account_name, account_info, snapshot_df)
            await calculate_annualized_return_1h(account_name, account_info, snapshot_df)
        except Exception as exc:
            RUNTIME_LOGGER.exception("[update_metrics] account %s failed", account_name)


async def update_bigquery_returns(snapshot_frames=None):
    """Build and merge Binance return rows without generating report cards."""
    if not BIGQUERY_RETURN_ENABLED:
        return

    if snapshot_frames is None:
        snapshot_frames = {}
        for account_name, account_info in list(accounts.items()):
            if account_info.get("exchange_id", "binance").lower() != "binance":
                continue
            try:
                snapshot_df = read_minute_snapshot_file(account_info["minute_snapshot_file"])
                snapshot_frames[account_name] = (
                    snapshot_df if snapshot_df is not None else pd.DataFrame()
                )
            except Exception:
                RUNTIME_LOGGER.exception(
                    "[bigquery_returns] account=%s snapshot read failed",
                    account_name,
                )

    target_date = completed_reference_date()
    try:
        rows, skipped = build_binance_return_rows(accounts, snapshot_frames, target_date)
        for account_name, reason in skipped.items():
            RUNTIME_LOGGER.warning(
                "[bigquery_returns] skipped account=%s date=%s reason=%s",
                account_name,
                target_date,
                reason,
            )
        result = await asyncio.to_thread(
            merge_rows_to_bigquery,
            rows,
            project_id=BIGQUERY_PROJECT_ID,
            dataset=BIGQUERY_DATASET,
            table=BIGQUERY_RETURN_TABLE,
            credentials_path=BIGQUERY_CREDENTIALS_PATH,
        )
        RUNTIME_LOGGER.info(
            "[bigquery_returns] merged date=%s rows=%s skipped=%s job_id=%s",
            target_date,
            result["row_count"],
            len(skipped),
            result["job_id"],
        )
    except Exception:
        RUNTIME_LOGGER.exception(
            "[bigquery_returns] upload failed date=%s target=%s.%s.%s",
            target_date,
            BIGQUERY_PROJECT_ID,
            BIGQUERY_DATASET,
            BIGQUERY_RETURN_TABLE,
        )


async def refresh_annualized_metrics():
    """刷新日报所需的账户指标，并返回本轮读取的分钟快照。"""
    snapshot_frames = {}
    for account_name, account_info in list(accounts.items()):
        try:
            snapshot_df = read_minute_snapshot_file(account_info["minute_snapshot_file"])
            if snapshot_df is None:
                snapshot_df = pd.DataFrame()
            snapshot_frames[account_name] = snapshot_df
            await update_annualized_returns(account_name, account_info, snapshot_df)
            await calculate_annualized_cumulative_return(account_name, account_info, snapshot_df)
            await calculate_post_dividend_annualized_return(account_name, account_info, snapshot_df)
            await update_report_actual_equity(account_name, account_info, snapshot_df)
        except Exception:
            RUNTIME_LOGGER.exception("[annualized_metrics] account %s refresh failed", account_name)

    return snapshot_frames


async def update_annualized_metrics():
    """日级任务入口：刷新指标并向正式路由发送日报。"""
    snapshot_frames = await refresh_annualized_metrics()

    await update_bigquery_returns(snapshot_frames)

    daily_report = build_daily_report(accounts, account_metrics)
    daily_detail_cards = build_daily_detail_cards(daily_report)
    performance_card = build_return_performance_card(
        daily_report.performance_sections,
        daily_report.report_date,
    )

    await NOTIFIER.send_card(daily_detail_cards, route="daily_report")
    # 收益表现总览最后发送，保证它位于本次日报消息的最下方。
    await NOTIFIER.send_card(performance_card, route="return_performance")


async def update_annualized_metrics_test():
    """启动任务：刷新同一套指标，并仅向严格 test 路由发送日报。"""
    await refresh_annualized_metrics()

    daily_report = build_daily_report(accounts, account_metrics)
    daily_detail_cards = build_daily_detail_cards(daily_report)
    performance_card = build_return_performance_card(
        daily_report.performance_sections,
        daily_report.report_date,
    )

    await NOTIFIER.send_card(daily_detail_cards, route="test")
    # 测试群同样保持收益表现总览位于最后。
    await NOTIFIER.send_card(performance_card, route="test")


def cleanup_expired_runtime_logs():
    deleted_paths, failed_paths = cleanup_runtime_logs(RUNTIME_LOG_RETENTION_DAYS)
    RUNTIME_LOGGER.info(
        "[runtime_log_cleanup] deleted %d log file(s) older than %d days",
        len(deleted_paths),
        RUNTIME_LOG_RETENTION_DAYS,
    )
    if failed_paths:
        RUNTIME_LOGGER.warning(
            "[runtime_log_cleanup] failed to delete %d log file(s): %s",
            len(failed_paths),
            ", ".join(path.name for path in failed_paths),
        )


def start_monitor_scheduler():
    scheduler = MonitorScheduler(RUNTIME_LOGGER)
    scheduler.add_task("startup", update_bigquery_returns)
    scheduler.add_task("startup", update_annualized_metrics_test)
    scheduler.add_task("minute", update_metrics)
    # scheduler.add_task("minute", check_large_equity_changes)
    scheduler.add_task("daily", update_annualized_metrics)
    scheduler.add_task("daily", cleanup_expired_runtime_logs)
    return scheduler

if __name__ == '__main__':
    RUNTIME_LOGGER.error(
        "This monitor entrypoint is integrated into "
        "the portfolio NAV service. Start the unified service instead."
    )
