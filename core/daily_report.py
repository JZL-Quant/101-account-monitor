import math
from dataclasses import dataclass
from datetime import date

from config.settings import MIN_VALID_CALCULATION_VALUE
from core.return_attention import build_return_performance_sections


@dataclass
class DailyReport:
    report_date: date
    detail_groups: list
    performance_sections: list


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _read_metric(metrics_store, account_name, metric_name):
    try:
        return metrics_store.read(account_name, metric_name)
    except Exception:
        return None


def _combined_return_from_results(results, metric_key):
    total_equity = 0.0
    weighted_return = 0.0
    for result in results:
        equity = _finite_number(result.get("actual_equity"))
        annualized_return = _finite_number(result.get(metric_key))
        if equity is None or annualized_return is None or equity <= 0:
            continue
        total_equity += equity
        weighted_return += equity * annualized_return
    if total_equity <= MIN_VALID_CALCULATION_VALUE:
        return None
    return _finite_number(weighted_return / total_equity)


def _group_interest_rate(results, group_accounts):
    total_equity = 0.0
    weighted_rate = 0.0
    rates = []
    for result in results:
        account_info = group_accounts.get(result.get("account_name"), {})
        rate = _finite_number(account_info.get("interest_rate"))
        if rate is None:
            continue
        rate_percent = rate * 100
        rates.append(rate_percent)
        equity = _finite_number(result.get("actual_equity"))
        if equity is not None and equity > 0:
            total_equity += equity
            weighted_rate += equity * rate_percent
    if total_equity > MIN_VALID_CALCULATION_VALUE:
        return _finite_number(weighted_rate / total_equity)
    return sum(rates) / len(rates) if rates else None


def _build_combined_returns(results):
    return {
        metric_key: _combined_return_from_results(results, metric_key)
        for metric_key in ("ar24h", "ar7d", "ar30d")
    }


def build_daily_report(account_map, metrics_store, report_date=None):
    """从已刷新指标生成与展示渠道无关的日报数据。"""
    report_date = report_date or date.today()
    groups = {}
    performance_rows = []

    for account_name, account_info in account_map.items():
        ar24h = _read_metric(metrics_store, account_name, "annualized_return_24h")
        ar7d = _read_metric(metrics_store, account_name, "annualized_return_7d")
        ar30d = _read_metric(metrics_store, account_name, "annualized_return_30d")
        result = {
            "account_name": account_name,
            "display_name": account_name,
            "actual_equity": _read_metric(metrics_store, account_name, "report_actual_equity"),
            "ar24h": ar24h,
            "ar7d": ar7d,
            "ar30d": ar30d,
            "ar24h_val": _finite_number(ar24h) if _finite_number(ar24h) is not None else -999.0,
        }

        exchange_id = account_info.get("exchange_id", "binance")
        exchange_label = account_info.get(
            "exchange_label", account_info.get("exchange", "Exchange")
        )
        account_group = account_info.get("account_group", "Other")
        ccy = str(account_info.get("ccy", "USDT")).upper()
        group_key = f"{exchange_id}_{account_group.lower()}_{ccy.lower()}"
        group = groups.setdefault(group_key, {
            "key": group_key,
            "title": f"{exchange_label}_{account_group}_{ccy}",
            "accounts": {},
            "results": [],
            "ccy": ccy,
        })
        group["accounts"][account_name] = account_info
        group["results"].append(result)

        performance_rows.append({
            "account_name": account_name,
            "exchange_id": exchange_id,
            "exchange_label": exchange_label,
            "ccy": ccy,
            "annualized_return_24h": ar24h,
            "annualized_return_7d": ar7d,
        })

    detail_groups = []
    for group in groups.values():
        if not group["results"]:
            continue
        results = sorted(group["results"], key=lambda item: item["ar24h_val"], reverse=True)
        benchmark = _group_interest_rate(results, group["accounts"])
        group["benchmark"] = benchmark if benchmark is not None else 0.0
        detail_groups.append({
            "group": group,
            "results": results,
            "combined": _build_combined_returns(results),
        })

    return DailyReport(
        report_date=report_date,
        detail_groups=detail_groups,
        performance_sections=build_return_performance_sections(performance_rows),
    )
