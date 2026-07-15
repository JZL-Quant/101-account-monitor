from prometheus_client import CollectorRegistry, Gauge


import math


collector_registry = CollectorRegistry()


def create_account_metrics(accounts: dict, registry=collector_registry) -> dict:
    """为每个账户创建 Prometheus 指标。"""
    metrics = {}
    for account_name, account_info in accounts.items():
        exchange_label = account_info.get("exchange_label", account_info.get("exchange", "Binance"))
        metrics[account_name] = {
            "actual_equity": Gauge(
                f"{exchange_label}_{account_name}_actual_equity",
                f"{exchange_label}_{account_name}_Actual Equity",
                registry=registry,
            ),
            "report_actual_equity": Gauge(
                f"{exchange_label}_{account_name}_report_actual_equity",
                f"{exchange_label}_{account_name}_Report Actual Equity",
                registry=registry,
            ),
            "cumulative_return": Gauge(
                f"{exchange_label}_{account_name}_cumulative_return",
                f"{exchange_label}_{account_name}_Cumulative Return",
                registry=registry,
            ),
            "post_dividend_return": Gauge(
                f"{exchange_label}_{account_name}_post_dividend_annualized_return",
                f"{exchange_label}_{account_name}_Post-Dividend Annualized Return",
                registry=registry,
            ),
            "annualized_return_1m": Gauge(
                f"{exchange_label}_{account_name}_annualized_return_1m",
                f"{exchange_label}_{account_name}_Annualized Return based on Last 1 Minute Return",
                registry=registry,
            ),
            "annualized_return_1h": Gauge(
                f"{exchange_label}_{account_name}_annualized_return_1h",
                f"{exchange_label}_{account_name}_Annualized Return based on Last 1 hour median Return",
                registry=registry,
            ),
            "annualized_return_7d": Gauge(
                f"{exchange_label}_{account_name}_annualized_return_7d",
                f"{exchange_label}_{account_name}_7-day Annualized Return",
                registry=registry,
            ),
            "annualized_return_30d": Gauge(
                f"{exchange_label}_{account_name}_annualized_return_30d",
                f"{exchange_label}_{account_name}_30-day Annualized Return",
                registry=registry,
            ),
            "annualized_return_24h": Gauge(
                f"{exchange_label}_{account_name}_annualized_return_24h",
                f"{exchange_label}_{account_name}_24-hour Annualized Return",
                registry=registry,
            ),
        }
    return metrics


class AccountMetricsStore:
    """集中维护账户 Prometheus 指标的创建、写入和读取。"""

    def __init__(self, accounts: dict, registry=collector_registry):
        self._registry = registry
        self._metrics = create_account_metrics(accounts, registry)

    def add_account(self, account_name: str, account_info: dict):
        """运行时幂等注册账户指标。"""
        if account_name in self._metrics:
            return
        self._metrics.update(
            create_account_metrics({account_name: account_info}, self._registry)
        )

    def set(self, account_name: str, metric_key: str, value):
        """写入单个账户指标。"""
        try:
            if math.isinf(float(value)):
                value = float("nan")
        except (TypeError, ValueError):
            value = float("nan")
        self._metrics[account_name][metric_key].set(value)

    def read(self, account_name: str, metric_key: str):
        """读取单个账户指标当前值，失败时返回 NaN。"""
        try:
            return self._metrics[account_name][metric_key].collect()[0].samples[0].value
        except (IndexError, AttributeError, KeyError):
            return float("nan")
