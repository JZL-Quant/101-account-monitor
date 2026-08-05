import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone

import pandas as pd
import yaml

from core.account_registry import EXCHANGE_ACCOUNT_BY_ID, get_account_registry


BASE_DIR = None
CONFIG_PATH = None
account_registry = None
account_infos = {}
accounts = {}
account_update_tasks = {}
_on_account_added = None
_on_account_archived = None
archived_account_infos = {}
LOGGER = logging.getLogger(__name__)


def initialize(base_dir, on_account_added=None, on_account_archived=None):
    global BASE_DIR, CONFIG_PATH, account_registry, account_infos, accounts
    global _on_account_added, _on_account_archived, archived_account_infos
    _on_account_added = on_account_added
    _on_account_archived = on_account_archived

    BASE_DIR = base_dir
    CONFIG_PATH = os.path.join(BASE_DIR, "accounts_config.yaml")
    ensure_principals_in_config()
    account_registry = get_account_registry(CONFIG_PATH)
    account_infos = account_registry.local_accounts()
    accounts = account_registry.exchange_accounts()
    archived_account_infos = load_archived_accounts()


def archive_dir():
    return os.path.join(BASE_DIR, "archived_accounts")


def archive_config_path():
    return os.path.join(archive_dir(), "accounts_config.yaml")


def load_archived_accounts():
    path = archive_config_path()
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    return data if isinstance(data, dict) else {}


def account_response(account_name, action, amount, action_date):
    account_info = account_infos.get(account_name, {})
    return {
        "message": f"{account_name} {action}成功，金额: {amount}，日期: {action_date}",
        "account_name": account_name,
        "ccy": account_info.get("ccy", "USDT"),
    }


def build_account_options():
    return [
        {
            "name": account_name,
            "label": (
                f"{account_name} "
                f"({account_info.get('exchange_label', account_info.get('exchange', 'Binance'))} "
                f"/ {account_info.get('ccy', 'USDT')})"
            ),
            "ccy": account_info.get("ccy", "USDT"),
            "exchange": account_info.get("exchange", "Binance"),
            "client": account_info.get("client", ""),
            "principal": account_info.get("principal", account_info.get("initial_unit", 0)),
        }
        for account_name, account_info in account_infos.items()
    ]


def find_equity_changes(account_name, event_date, threshold):
    """Return actual-equity changes on a date whose absolute size reaches threshold."""
    if account_name not in account_infos:
        raise ValueError(f"账户 {account_name} 不存在")
    threshold = float(threshold)
    if threshold <= 0:
        raise ValueError("最小资金变动金额必须大于 0")

    snapshot_file = account_infos[account_name]["minute_snapshot_file"]
    if not os.path.isfile(snapshot_file):
        raise ValueError(f"找不到账户 {account_name} 的分钟快照")

    frame = pd.read_csv(snapshot_file)
    required = {"timestamp", "actual_equity"}
    if not required.issubset(frame.columns):
        raise ValueError("分钟快照缺少 timestamp 或 actual_equity 列")

    timestamps = pd.to_datetime(frame["timestamp"], format="mixed", errors="coerce")
    equity = pd.to_numeric(frame["actual_equity"], errors="coerce")
    changes = equity.diff()
    amount_columns = {
        "subscription_amount": "subscription_amount",
        "dividend_amount": "dividend_amount",
        "interest_deduction": "interest_deduction",
        "withdrawal_amount": "withdraw_amount",
    }
    recorded = {}
    for result_name, column_name in amount_columns.items():
        recorded[result_name] = (
            pd.to_numeric(frame[column_name], errors="coerce").fillna(0.0)
            if column_name in frame.columns
            else pd.Series(0.0, index=frame.index)
        )
    target_date = pd.to_datetime(event_date).date()
    candidate_mask = (
        timestamps.notna()
        & (timestamps.dt.date == target_date)
        & changes.notna()
        & (changes.abs() >= threshold)
    )

    results = []
    ignore_tolerance = 0.01 if account_infos[account_name].get("ccy", "USDT") == "USDT" else 1e-8
    for index in frame.index[candidate_mask]:
        change = float(changes.at[index])
        subscription = float(recorded["subscription_amount"].at[index])
        dividend = float(recorded["dividend_amount"].at[index])
        interest = float(recorded["interest_deduction"].at[index])
        withdrawal = float(recorded["withdrawal_amount"].at[index])
        ignored_amount = (
            change - subscription
            if change > 0
            else abs(change) - dividend - interest - withdrawal
        )
        if abs(ignored_amount) <= ignore_tolerance:
            continue
        results.append({
            "timestamp": str(frame.at[index, "timestamp"]),
            "previous_timestamp": str(frame.at[index - 1, "timestamp"]),
            "previous_equity": float(equity.at[index - 1]),
            "actual_equity": float(equity.at[index]),
            "change": change,
            "direction": "in" if change > 0 else "out",
            "subscription_amount": subscription,
            "dividend_amount": dividend,
            "interest_deduction": interest,
            "withdrawal_amount": withdrawal,
            "ignored_amount": ignored_amount,
        })
    return results


def build_account_table_groups():
    """构建账户展示表格，明确排除 key、secret 等敏感字段。"""
    groups = {}
    for account_name, account_info in sorted(account_infos.items()):
        exchange = account_info.get("exchange_label") or account_info.get("exchange", "Binance")
        exchange = str(exchange)
        groups.setdefault(exchange, []).append(
            {
                "name": account_name,
                "client": account_info.get("client", ""),
                "ccy": account_info.get("ccy", "USDT"),
                "account_type": account_info.get("account_type", "account"),
                "initial_unit": account_info.get("initial_unit", ""),
                "principal": account_info.get("principal", account_info.get("initial_unit", "")),
                "interest_rate": account_info.get("interest_rate", 0),
                "archived": False,
            }
        )
    for account_name, account_info in sorted(archived_account_infos.items()):
        exchange = str(account_info.get("exchange_label") or account_info.get("exchange", "Binance"))
        groups.setdefault(exchange, []).append(
            {
                "name": account_name,
                "client": account_info.get("client", ""),
                "ccy": account_info.get("ccy", "USDT"),
                "account_type": account_info.get("account_type", "account"),
                "initial_unit": account_info.get("initial_unit", ""),
                "principal": account_info.get("principal", account_info.get("initial_unit", "")),
                "interest_rate": account_info.get("interest_rate", 0),
                "archived": True,
                "archived_at": account_info.get("archived_at", ""),
            }
        )
    return [
        {"exchange": exchange, "accounts": rows}
        for exchange, rows in sorted(groups.items())
    ]


def normalize_account_type(value):
    account_type = (value or "").strip()
    if account_type not in ("account", "account_pro"):
        raise ValueError("账户类型仅支持普通账户或 Pro 账户")
    return account_type


def parse_initial_unit(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError("委托数量必须是数字")
    if amount <= 0:
        raise ValueError("委托数量必须大于 0")
    return amount


def normalize_ccy(value):
    ccy = (value or "").strip().upper()
    if ccy not in ("USDT", "BTC"):
        raise ValueError("币种仅支持 USDT 或 BTC")
    return ccy


def build_exchange_options():
    return [
        {
            "id": exchange_id,
            "label": account_cls.exchange_label,
            "credential_fields": [
                field.to_dict() for field in account_cls.credential_fields
            ],
        }
        for exchange_id, account_cls in sorted(EXCHANGE_ACCOUNT_BY_ID.items())
    ]


def normalize_exchange(value):
    exchange_id = (value or "").strip().lower()
    if exchange_id not in EXCHANGE_ACCOUNT_BY_ID:
        raise ValueError(f"交易所 {value} 暂不支持")
    return exchange_id


def normalize_client(value):
    client = (value or "").strip()
    if not client:
        raise ValueError("client 不能为空")
    if not re.fullmatch(r"[A-Za-z0-9_\\-]+", client):
        raise ValueError("client 只能包含英文字母、数字、下划线或短横线")
    return client


def product_prefix(product_name):
    return product_name.split("_", 1)[0] if "_" in product_name else product_name


def validate_new_account_name(product_name):
    product_name = (product_name or "").strip()
    if product_name in account_infos:
        raise ValueError(f"账户 {product_name} 已存在")
    if product_name in archived_account_infos:
        raise ValueError(f"账户 {product_name} 已存在并已归档，不允许再次创建")
    return product_name


def infer_interest_rate(product_name):
    prefix = product_prefix(product_name)
    rate_counts = {}
    for account_name, account_info in account_infos.items():
        if product_prefix(account_name) != prefix:
            continue
        rate = account_info.get("interest_rate", 0)
        rate_counts[rate] = rate_counts.get(rate, 0) + 1
    if not rate_counts:
        return 0
    return max(rate_counts.items(), key=lambda item: item[1])[0]


def parse_interest_rate(value, product_name):
    if value is None or str(value).strip() == "":
        return infer_interest_rate(product_name)
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise ValueError("interest_rate 必须是数字")
    if rate < 0:
        raise ValueError("interest_rate 不能小于 0")
    return rate


async def validate_exchange_credentials(
    exchange,
    account_type,
    api_key,
    secret_key,
    extra_credentials=None,
):
    """Validate credentials before they are persisted to accounts_config.yaml."""
    exchange_id = normalize_exchange(exchange)
    account_type = normalize_account_type(account_type)
    api_key = (api_key or "").strip()
    secret_key = (secret_key or "").strip()
    if not api_key or not secret_key:
        raise ValueError("API Key 和 Secret Key 不能为空")
    account_cls = EXCHANGE_ACCOUNT_BY_ID[exchange_id]
    credential_values = dict(extra_credentials or {})
    credentials = account_cls.normalize_credentials(credential_values)
    account_info = {
        "key": api_key,
        "secret": secret_key,
        "initial_unit": 1.0,
        "account_type": account_type,
        "ccy": "USDT",
        "exchange": exchange_id,
        "minute_snapshot_file": os.devnull,
        "blacklist": [],
        **credentials,
    }
    account = account_cls.from_account_info("__credential_check__", account_info)
    try:
        resolved = await account.validate_credentials()
        if isinstance(resolved, dict):
            credentials.update(account_cls.normalize_managed_credentials(resolved))
        return credentials
    finally:
        try:
            await account.close()
        except Exception:
            LOGGER.exception("Failed to close temporary credential validation client")


def append_account_config(
    product_name,
    initial_unit,
    ccy,
    exchange,
    account_type,
    client,
    interest_rate,
    api_key,
    secret_key,
    extra_credentials=None,
):
    global account_infos, accounts

    product_name = product_name.strip()
    api_key = api_key.strip()
    secret_key = secret_key.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", product_name):
        raise ValueError("产品名称只能包含英文字母、数字和下划线")
    validate_new_account_name(product_name)
    if not api_key or not secret_key:
        raise ValueError("API Key 和 Secret Key 不能为空")

    initial_unit = parse_initial_unit(initial_unit)
    ccy = normalize_ccy(ccy)
    exchange = normalize_exchange(exchange)
    account_cls = EXCHANGE_ACCOUNT_BY_ID[exchange]
    credential_values = dict(extra_credentials or {})
    credentials = account_cls.normalize_credentials(credential_values)
    credentials.update(account_cls.normalize_managed_credentials(credential_values))
    account_type = normalize_account_type(account_type)
    client = normalize_client(client)
    interest_rate = parse_interest_rate(interest_rate, product_name)

    config_entry = {
        product_name: {
            "key": api_key,
            "secret": secret_key,
            "initial_unit": initial_unit,
            "principal": initial_unit,
            "account_type": account_type,
            "exchange": exchange,
            "interest_rate": interest_rate,
            "client": client,
            "ccy": ccy,
        }
    }
    config_entry[product_name].update(credentials)
    yaml_fragment = yaml.safe_dump(config_entry, allow_unicode=True, sort_keys=False)

    original_size = os.path.getsize(CONFIG_PATH)
    try:
        with open(CONFIG_PATH, "a", encoding="utf-8", newline="\n") as file:
            file.write("\n" + yaml_fragment)

        account_infos = account_registry.reload()
        account_info = account_infos[product_name]
        account_cls = EXCHANGE_ACCOUNT_BY_ID[account_info["exchange_id"]]
        accounts[product_name] = account_cls.from_account_info(product_name, account_info)
        if _on_account_added is not None:
            _on_account_added(product_name, account_info)
    except Exception:
        with open(CONFIG_PATH, "r+b") as file:
            file.truncate(original_size)
        account_registry.reload()
        raise

    start_account_update_task(product_name)
    return account_infos[product_name]


async def archive_account(account_name):
    """停止账户、保存归档记录、移动 CSV，并从活动配置中移除。"""
    global account_infos, archived_account_infos
    if account_name in archived_account_infos:
        raise ValueError(f"账户 {account_name} 已归档")
    account_info = account_infos.get(account_name)
    if account_info is None:
        raise ValueError(f"账户 {account_name} 不存在")

    task = account_update_tasks.pop(account_name, None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    account = accounts.pop(account_name, None)
    if account is not None:
        await account.close()

    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        active_content = file.read()
    account_pattern = re.compile(
        rf"(?ms)^(?P<header>{re.escape(account_name)}:[ \t]*\r?\n)"
        rf"(?P<body>(?:^[ \t]+[^\r\n]*(?:\r?\n|$))*)"
    )
    match = account_pattern.search(active_content)
    if not match:
        raise ValueError(f"配置文件中找不到账户 {account_name}")
    raw_entry = yaml.safe_load(match.group(0)) or {}
    archived_entry = dict(raw_entry.get(account_name) or {})

    os.makedirs(archive_dir(), exist_ok=True)
    snapshot_file = account_info["minute_snapshot_file"]
    archived_snapshot = ""
    if os.path.isfile(snapshot_file):
        archived_snapshot = os.path.join(archive_dir(), os.path.basename(snapshot_file))
        if os.path.exists(archived_snapshot):
            raise ValueError(f"归档目录中已存在快照 {os.path.basename(archived_snapshot)}")
        shutil.move(snapshot_file, archived_snapshot)

    archived_entry.update({
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_file": archived_snapshot,
        "exchange_label": account_info.get("exchange_label", account_info.get("exchange", "Binance")),
    })
    new_archive = dict(archived_account_infos)
    new_archive[account_name] = archived_entry
    archive_tmp = archive_config_path() + ".tmp"
    with open(archive_tmp, "w", encoding="utf-8", newline="\n") as file:
        yaml.safe_dump(new_archive, file, allow_unicode=True, sort_keys=False)
    os.replace(archive_tmp, archive_config_path())

    updated_content = active_content[:match.start()] + active_content[match.end():]
    active_tmp = CONFIG_PATH + ".tmp"
    with open(active_tmp, "w", encoding="utf-8", newline="\n") as file:
        file.write(updated_content)
    os.replace(active_tmp, CONFIG_PATH)

    archived_account_infos = new_archive
    account_infos = account_registry.reload()
    if _on_account_archived is not None:
        _on_account_archived(account_name)
    return archived_account_infos[account_name]


def ensure_principals_in_config():
    """为缺少 principal 的老账户补上与 initial_unit 相同的本金。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        content = file.read()

    account_pattern = re.compile(
        r"(?ms)^(?P<header>[^\s#][^:\r\n]*:[ \t]*\r?\n)"
        r"(?P<body>(?:^[ \t]+[^\r\n]*(?:\r?\n|$))*)"
    )

    def add_principal(match):
        body = match.group("body")
        if re.search(r"(?m)^[ \t]+principal\s*:", body):
            return match.group(0)
        initial = re.search(
            r"(?m)^(?P<indent>[ \t]+)initial_unit:\s*(?P<value>[^#\r\n]+?)\s*(?:#.*)?(?:\r?\n|$)",
            body,
        )
        if not initial:
            return match.group(0)
        insertion = f'{initial.group(0)}{initial.group("indent")}principal: {initial.group("value").strip()}\n'
        new_body = body[:initial.start()] + insertion + body[initial.end():]
        return match.group("header") + new_body

    updated = account_pattern.sub(add_principal, content)
    if updated != content:
        temp_path = CONFIG_PATH + ".tmp"
        with open(temp_path, "w", encoding="utf-8", newline="\n") as file:
            file.write(updated)
        os.replace(temp_path, CONFIG_PATH)


def adjust_principal(account_name, amount):
    """按资金变动调整 config 中的本金；申购为正，赎回为负。"""
    if account_name not in account_infos:
        raise ValueError(f"账户 {account_name} 不存在")
    amount = float(amount)
    current = float(account_infos[account_name].get("principal", account_infos[account_name]["initial_unit"]))
    new_value = current + amount
    if new_value < 0:
        raise ValueError("赎回后的本金不能小于 0")

    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        content = file.read()
    account_pattern = re.compile(
        rf"(?ms)^(?P<header>{re.escape(account_name)}:[ \t]*\r?\n)"
        rf"(?P<body>(?:^[ \t]+[^\r\n]*(?:\r?\n|$))*)"
    )
    match = account_pattern.search(content)
    if not match:
        raise ValueError(f"配置文件中找不到账户 {account_name}")
    body = match.group("body")
    principal_pattern = re.compile(r"(?m)^(?P<indent>[ \t]+)principal:\s*[^#\r\n]+(?P<suffix>\s*(?:#.*)?)$")
    if principal_pattern.search(body):
        replacement_body = principal_pattern.sub(
            lambda item: f'{item.group("indent")}principal: {new_value:g}{item.group("suffix")}',
            body,
            count=1,
        )
    else:
        initial_unit_match = re.search(r"(?m)^(?P<indent>[ \t]+)initial_unit:[^\r\n]*(?:\r?\n|$)", body)
        if not initial_unit_match:
            raise ValueError(f"账户 {account_name} 缺少本金初值配置")
        insertion = f'{initial_unit_match.group(0)}{initial_unit_match.group("indent")}principal: {new_value:g}\n'
        replacement_body = body[:initial_unit_match.start()] + insertion + body[initial_unit_match.end():]
    updated = content[:match.start("body")] + replacement_body + content[match.end("body"):]
    temp_path = CONFIG_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8", newline="\n") as file:
        file.write(updated)
    os.replace(temp_path, CONFIG_PATH)

    account_infos[account_name]["principal"] = new_value
    account_registry.reload()
    return new_value


def deduct_principal(account_name, withdrawal_amount):
    return adjust_principal(account_name, -float(withdrawal_amount))


def _persist_account_config_fields(account_name, updates):
    """Update adapter-resolved scalar fields without rewriting the YAML file."""
    if not updates:
        return
    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        content = file.read()
    account_pattern = re.compile(
        rf"(?ms)^(?P<header>{re.escape(account_name)}:[ \t]*\r?\n)"
        rf"(?P<body>(?:^[ \t]+[^\r\n]*(?:\r?\n|$))*)"
    )
    match = account_pattern.search(content)
    if not match:
        raise ValueError(f"配置文件中找不到账户 {account_name}")

    body = match.group("body")
    indent_match = re.search(r"(?m)^(?P<indent>[ \t]+)\S", body)
    indent = indent_match.group("indent") if indent_match else "  "
    for field, value in updates.items():
        serialized = json.dumps(str(value), ensure_ascii=False)
        field_pattern = re.compile(
            rf"(?m)^(?P<indent>[ \t]+){re.escape(field)}\s*:[^\r\n]*$"
        )
        if field_pattern.search(body):
            body = field_pattern.sub(
                lambda item: f'{item.group("indent")}{field}: {serialized}',
                body,
                count=1,
            )
        else:
            if body and not body.endswith(("\n", "\r")):
                body += "\n"
            body += f"{indent}{field}: {serialized}\n"

    updated = content[:match.start("body")] + body + content[match.end("body"):]
    temp_path = CONFIG_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8", newline="\n") as file:
        file.write(updated)
    os.replace(temp_path, CONFIG_PATH)

    account_infos[account_name].update(updates)
    account_registry.reload()


async def resolve_missing_credentials():
    """Detect and persist adapter-managed credential metadata once per account."""
    for account_name, account in accounts.items():
        account_cls = type(account)
        managed_fields = account_cls.managed_credential_fields
        missing = [
            field for field in managed_fields if not account_infos[account_name].get(field)
        ]
        if not missing:
            continue
        try:
            resolved = await account.validate_credentials()
            if not isinstance(resolved, dict):
                continue
            updates = {field: resolved[field] for field in missing if resolved.get(field)}
            _persist_account_config_fields(account_name, updates)
            LOGGER.info("Persisted detected credentials for account %s: %s", account_name, sorted(updates))
        except Exception:
            LOGGER.exception("Failed to detect credentials for account %s", account_name)


def start_account_update_task(account_name):
    if account_name not in accounts:
        return
    task = account_update_tasks.get(account_name)
    if task is None or task.done():
        account_update_tasks[account_name] = asyncio.create_task(accounts[account_name].update_net_value())


async def close_accounts():
    """Release HTTP sessions and connection pools owned by exchange clients."""
    results = await asyncio.gather(
        *(account.close() for account in accounts.values()),
        return_exceptions=True,
    )
    for account_name, result in zip(accounts, results):
        if isinstance(result, Exception):
            # Shutdown should continue even if one SDK has already closed itself.
            print(f"failed to close exchange client for {account_name}: {result}")
