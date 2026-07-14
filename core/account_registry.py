import os
import re

import yaml

from .exchange_accounts import BinanceExchangeAccount, GateExchangeAccount
from config.settings import MINUTE_SNAPSHOT_DIR


EXCHANGE_ACCOUNT_BY_ID = {
    "binance": BinanceExchangeAccount,
    "gate": GateExchangeAccount,
}


class AccountRegistry:
    """加载账户配置，并提供本地账户数据和交易所运行时账户实例。"""

    def __init__(self, config_path: str):
        """基于一个 accounts_config.yaml 文件创建账户注册表。"""
        self.config_path = os.path.abspath(config_path)
        self._accounts = None

    def local_accounts(self) -> dict:
        """返回由配置生成的账户 dict，用于指标、报告和本地 CSV 读取。"""
        if self._accounts is None:
            self._accounts = self._load_accounts()
        return {name: dict(account_info) for name, account_info in self._accounts.items()}

    def exchange_accounts(self) -> dict:
        """构建交易所运行时账户对象，用于拉取权益并写入快照。"""
        accounts = {}
        for account_name, account_info in self.local_accounts().items():
            account_cls = EXCHANGE_ACCOUNT_BY_ID.get(account_info["exchange_id"])
            if account_cls is None:
                raise NotImplementedError(
                    f"NAV fetcher for exchange {account_info['exchange']!r} is not implemented"
                )
            accounts[account_name] = account_cls.from_account_info(account_name, account_info)
        return accounts

    def reload(self):
        """重新读取 accounts_config.yaml，并返回最新的本地账户 dict。"""
        self._accounts = None
        return self.local_accounts()

    def _load_accounts(self) -> dict:
        with open(self.config_path, "r", encoding="utf-8") as file:
            raw_config = yaml.safe_load(file) or {}

        global_blacklist = raw_config.get("Blacklist", [])
        return {
            str(account_name): self._build_account_info(str(account_name), account_info, global_blacklist)
            for account_name, account_info in raw_config.items()
            if isinstance(account_info, dict)
        }

    def _build_account_info(self, account_name: str, account_info: dict, global_blacklist=None) -> dict:
        exchange = account_info.get("exchange", "Binance")
        exchange_id = self._exchange_id(exchange)
        exchange_label = self._exchange_label(exchange)
        account_group = self._account_group(account_name)
        ccy = self._ccy(account_info.get("ccy", "USDT"))
        minute_snapshot_file = self._minute_snapshot_file(exchange_label, account_name, ccy)
        return {
            "minute_snapshot_file": minute_snapshot_file,
            "initial_unit": account_info["initial_unit"],
            "account_type": account_info["account_type"],
            "interest_rate": account_info.get("interest_rate", 0),
            "client": account_info.get("client", ""),
            "ccy": ccy,
            "exchange": exchange,
            "exchange_id": exchange_id,
            "exchange_label": exchange_label,
            "account_group": account_group,
            "key": account_info["key"],
            "secret": account_info["secret"],
            "blacklist": account_info.get("blacklist", global_blacklist or []),
        }

    def _minute_snapshot_file(self, exchange_label: str, account_name: str, ccy: str) -> str:
        ccy_label = self._safe_label(ccy.upper())
        file_name = f"{exchange_label}_{account_name}_{ccy_label}_minute_snapshot.csv"
        os.makedirs(MINUTE_SNAPSHOT_DIR, exist_ok=True)
        return os.path.join(MINUTE_SNAPSHOT_DIR, file_name)

    @staticmethod
    def _exchange_id(exchange: str) -> str:
        return (exchange or "Binance").strip().lower()

    @staticmethod
    def _exchange_label(exchange: str) -> str:
        exchange = (exchange or "Binance").strip()
        if not exchange:
            return "Binance"
        return AccountRegistry._safe_label(exchange[:1].upper() + exchange[1:]) or "Exchange"

    @staticmethod
    def _ccy(ccy: str) -> str:
        return (ccy or "USDT").strip().upper()

    @staticmethod
    def _safe_label(value: str) -> str:
        return re.sub(r"\W+", "_", str(value or "")).strip("_")

    @staticmethod
    def _account_group(account_name: str) -> str:
        account_name = str(account_name)
        return account_name.split("_", 1)[0] if "_" in account_name else "Other"

def get_account_registry(config_path: str) -> AccountRegistry:
    """根据 accounts_config.yaml 路径创建 AccountRegistry。"""
    return AccountRegistry(config_path)
