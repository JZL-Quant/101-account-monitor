import math
from collections import defaultdict
from statistics import fmean, pstdev


MIN_GROUP_SIZE = 5
MIN_STD = 1e-7
OUTSTANDING_Z = 2.5
ATTENTION_Z = -1.5
PRIORITY_ATTENTION_Z = -2.0

RETURN_PERIODS = (
    ("24h", "24h 收益率表现", "annualized_return_24h"),
    ("7D", "7D 收益率表现", "annualized_return_7d"),
)


def finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def classify_z(z_value):
    if z_value >= OUTSTANDING_Z:
        return "outstanding"
    if z_value <= PRIORITY_ATTENTION_Z:
        return "priority_attention"
    if z_value <= ATTENTION_Z:
        return "attention"
    return None


def build_return_performance_sections(account_rows, min_group_size=MIN_GROUP_SIZE):
    """按交易所和本位币计算各周期总体标准差与 Z 值。"""
    sections = []
    for period_key, title, metric_key in RETURN_PERIODS:
        grouped = defaultdict(list)
        for row in account_rows:
            value = finite_number(row.get(metric_key))
            if value is None:
                continue
            exchange_id = str(row.get("exchange_id") or "unknown").lower()
            ccy = str(row.get("ccy") or "USDT").upper()
            grouped[(exchange_id, ccy)].append((row, value))

        flagged = []
        for (_, ccy), members in grouped.items():
            if len(members) < min_group_size:
                continue
            values = [value for _, value in members]
            mean = fmean(values)
            std = pstdev(values)
            if not math.isfinite(std) or std <= MIN_STD:
                continue
            for row, value in members:
                z_value = (value - mean) / std
                level = classify_z(z_value)
                if level is None:
                    continue
                exchange_label = str(
                    row.get("exchange_label") or row.get("exchange_id") or "Exchange"
                )
                flagged.append({
                    "level": level,
                    "account_name": str(row.get("account_name") or ""),
                    "return_value": value,
                    "group_mean": mean,
                    "group_std": std,
                    "z_value": z_value,
                    "group_label": f"{exchange_label}-{ccy}",
                })

        flagged.sort(key=lambda item: item["z_value"])
        sections.append({"period": period_key, "title": title, "rows": flagged})
    return sections
