import pandas as pd

from config.settings import LARGE_EQUITY_CHANGE_THRESHOLDS


# 首次检查只建立基线，避免服务重启后重新发送历史告警。
LAST_CHECKED_SNAPSHOT = {}


def find_new_large_equity_changes(account_name, account_info, snapshot_df):
    """查找上次分钟检查之后尚未登记的大额实际权益变动。"""
    if snapshot_df is None or snapshot_df.empty:
        return []
    required = {"timestamp", "actual_equity"}
    if not required.issubset(snapshot_df.columns):
        raise ValueError("分钟快照缺少 timestamp 或 actual_equity 列")

    timestamps = pd.to_datetime(snapshot_df["timestamp"], format="mixed", errors="coerce")
    equity = pd.to_numeric(snapshot_df["actual_equity"], errors="coerce")
    valid_timestamps = timestamps.dropna()
    if valid_timestamps.empty:
        return []

    latest_timestamp = valid_timestamps.max()
    previous_checked = LAST_CHECKED_SNAPSHOT.get(account_name)
    LAST_CHECKED_SNAPSHOT[account_name] = latest_timestamp
    if previous_checked is None:
        return []

    ccy = str(account_info.get("ccy", "USDT")).upper()
    threshold = LARGE_EQUITY_CHANGE_THRESHOLDS.get(
        ccy, LARGE_EQUITY_CHANGE_THRESHOLDS["USDT"]
    )
    changes = equity.diff()
    candidate_mask = (
        timestamps.notna()
        & (timestamps > previous_checked)
        & changes.notna()
        & (changes.abs() >= threshold)
    )

    amount_columns = {
        "subscription_amount": "subscription_amount",
        "dividend_amount": "dividend_amount",
        "interest_deduction": "interest_deduction",
        "withdrawal_amount": "withdraw_amount",
    }
    recorded = {
        result_name: (
            pd.to_numeric(snapshot_df[column_name], errors="coerce").fillna(0.0)
            if column_name in snapshot_df.columns
            else pd.Series(0.0, index=snapshot_df.index)
        )
        for result_name, column_name in amount_columns.items()
    }
    ignore_tolerance = 0.01 if ccy == "USDT" else 1e-8
    results = []
    for index in snapshot_df.index[candidate_mask]:
        change = float(changes.at[index])
        subscription = float(recorded["subscription_amount"].at[index])
        dividend = float(recorded["dividend_amount"].at[index])
        interest = float(recorded["interest_deduction"].at[index])
        withdrawal = float(recorded["withdrawal_amount"].at[index])
        unrecorded_amount = (
            change - subscription
            if change > 0
            else abs(change) - dividend - interest - withdrawal
        )
        if abs(unrecorded_amount) <= ignore_tolerance:
            continue
        results.append({
            "account_name": account_name,
            "ccy": ccy,
            "threshold": threshold,
            "timestamp": str(snapshot_df.at[index, "timestamp"]),
            "previous_equity": float(equity.at[index - 1]),
            "actual_equity": float(equity.at[index]),
            "change": change,
            "unrecorded_amount": unrecorded_amount,
        })
    return results
