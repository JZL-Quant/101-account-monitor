import math
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date

import aiohttp

from core.feishu.weekly_client_ranking_card import build_weekly_client_ranking_card


@dataclass
class WeeklyClientRanking:
    report_date: date
    groups: list
    skipped_accounts: list


def apply_previous_ranks(ranking, previous_ranks):
    """在当前榜单行上补充相对上一期的名次变化。"""
    previous_ranks = previous_ranks or {}
    for group in ranking.groups:
        for row in group["rows"]:
            previous_rank = previous_ranks.get(row["client"])
            row["previous_rank"] = previous_rank
            row["rank_change"] = (
                None if previous_rank is None else previous_rank - row["rank"]
            )
    return ranking


def load_previous_ranks(path, report_date):
    try:
        with open(path, "r", encoding="utf-8") as file:
            snapshots = json.load(file).get("snapshots", [])
    except (FileNotFoundError, OSError, TypeError, ValueError, AttributeError):
        return {}
    candidates = [
        item for item in snapshots
        if isinstance(item, dict) and str(item.get("report_date", "")) < report_date.isoformat()
    ]
    if not candidates:
        return {}
    latest = max(candidates, key=lambda item: str(item.get("report_date", "")))
    ranks = latest.get("ranks", {})
    return {
        str(client): int(rank) for client, rank in ranks.items()
        if isinstance(rank, int) and rank > 0
    } if isinstance(ranks, dict) else {}


def save_ranking_snapshot(path, ranking, keep=12):
    """按日期幂等保存榜单，并通过原子替换避免状态文件写坏。"""
    path = os.fspath(path)
    try:
        with open(path, "r", encoding="utf-8") as file:
            snapshots = json.load(file).get("snapshots", [])
    except (FileNotFoundError, OSError, TypeError, ValueError, AttributeError):
        snapshots = []
    report_date = ranking.report_date.isoformat()
    ranks = {
        row["client"]: row["rank"]
        for group in ranking.groups for row in group["rows"]
    }
    snapshots = [
        item for item in snapshots
        if isinstance(item, dict) and item.get("report_date") != report_date
    ]
    snapshots.append({"report_date": report_date, "ranks": ranks})
    snapshots = sorted(snapshots, key=lambda item: item["report_date"])[-keep:]
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix="weekly_ranking_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"snapshots": snapshots}, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _read_metric(metrics_store, account_name, metric_name):
    try:
        return _finite_number(metrics_store.read(account_name, metric_name))
    except Exception:
        return None


def build_weekly_client_ranking(
    account_map, metrics_store, report_date=None, equity_to_usdt=None
):
    """跨交易所聚合客户，并按折算后的 USDT 等值权益加权排名。"""
    report_date = report_date or date.today()
    equity_to_usdt = {"USDT": 1.0, **(equity_to_usdt or {})}
    clients = {}
    skipped_accounts = []

    for account_name, account_info in account_map.items():
        client = str(account_info.get("client") or "").strip()
        if not client:
            skipped_accounts.append({"account_name": account_name, "reason": "未配置客户"})
            continue

        ccy = str(account_info.get("ccy") or "USDT").upper()
        client_row = clients.setdefault(client, {
            "client": client,
            "account_count": 0,
            "valid_account_count": 0,
            "equity_by_ccy": {},
            "total_equity_usdt": 0.0,
            "weighted_return": 0.0,
        })
        client_row["account_count"] += 1

        equity = _read_metric(metrics_store, account_name, "report_actual_equity")
        annualized_return_7d = _read_metric(
            metrics_store, account_name, "annualized_return_7d"
        )
        rate = _finite_number(equity_to_usdt.get(ccy))
        if (
            equity is None or equity <= 0 or annualized_return_7d is None
            or rate is None or rate <= 0
        ):
            skipped_accounts.append({"account_name": account_name, "reason": "权益、汇率或7日收益无效"})
            continue
        equity_usdt = equity * rate
        client_row["valid_account_count"] += 1
        client_row["equity_by_ccy"][ccy] = (
            client_row["equity_by_ccy"].get(ccy, 0.0) + equity
        )
        client_row["total_equity_usdt"] += equity_usdt
        client_row["weighted_return"] += equity_usdt * annualized_return_7d

    rows = []
    for client_row in clients.values():
        if client_row["valid_account_count"] == 0 or client_row["total_equity_usdt"] <= 0:
            continue
        client_row["annualized_return_7d"] = (
            client_row.pop("weighted_return") / client_row["total_equity_usdt"]
        )
        rows.append(client_row)
    rows.sort(key=lambda row: (-row["annualized_return_7d"], row["client"]))
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    groups = [{"key": "all_clients", "title": "客户总榜", "rows": rows}] if rows else []
    return WeeklyClientRanking(report_date, groups, skipped_accounts)


async def build_weekly_client_ranking_report(
    *, get_account_map, metrics_store, refresh_metrics, history_path, refresh=True
):
    """刷新指标、获取折算汇率并生成带上期名次的客户周排名。"""
    if refresh:
        await refresh_metrics()
    account_map = get_account_map()
    equity_to_usdt = {"USDT": 1.0}
    if any(str(info.get("ccy") or "USDT").upper() == "BTC" for info in account_map.values()):
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"},
                timeout=10,
            ) as response:
                response.raise_for_status()
                equity_to_usdt["BTC"] = float((await response.json())["price"])
    ranking = build_weekly_client_ranking(
        account_map, metrics_store, equity_to_usdt=equity_to_usdt
    )
    return apply_previous_ranks(
        ranking,
        load_previous_ranks(history_path, ranking.report_date),
    )


async def send_weekly_client_ranking(
    *,
    get_account_map,
    metrics_store,
    notifier,
    refresh_metrics,
    history_path,
    logger=None,
    route="default",
    require_route=False,
    refresh=True,
    persist_history=None,
):
    """通过正式与 test 共用的唯一入口生成并发送客户周排名。"""
    ranking = await build_weekly_client_ranking_report(
        get_account_map=get_account_map,
        metrics_store=metrics_store,
        refresh_metrics=refresh_metrics,
        history_path=history_path,
        refresh=refresh,
    )
    if not ranking.groups:
        if logger:
            logger.warning("[weekly_client_ranking] no valid client rows; skipped")
        return None
    await notifier.send_card(
        build_weekly_client_ranking_card(ranking),
        route=route,
        require_route=require_route,
    )
    if persist_history is None:
        persist_history = route in (None, "", "default")
    if persist_history:
        save_ranking_snapshot(history_path, ranking)
    return ranking
