import ast
import unittest
from pathlib import Path


def load_account_sort_key(exchange_order):
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "sync_grafana_dashboards.py"
    ).read_text(encoding="utf-8")
    module = ast.parse(source)
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "account_sort_key"
    )
    namespace = {"EXCHANGE_SORT_ORDER": exchange_order}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<test>", "exec"), namespace)
    return namespace["account_sort_key"]


class GrafanaAccountOrderingTests(unittest.TestCase):
    def test_accounts_are_sorted_by_configured_exchange_then_internal_name(self):
        accounts = [
            {"exchange": "OKX", "name": "Zulu_2", "source": "monitor"},
            {"exchange": "Binance", "name": "beta_1", "source": "monitor"},
            {"exchange": "KuCoin", "name": "Alpha_2", "source": "monitor"},
            {"exchange": "Binance", "name": "Alpha_1", "source": "monitor"},
        ]

        account_sort_key = load_account_sort_key(["KuCoin", "OKX", "Binance"])
        ordered = sorted(accounts, key=account_sort_key)

        self.assertEqual(
            [(item["exchange"], item["name"]) for item in ordered],
            [
                ("KuCoin", "Alpha_2"),
                ("OKX", "Zulu_2"),
                ("Binance", "Alpha_1"),
                ("Binance", "beta_1"),
            ],
        )

    def test_unlisted_exchanges_follow_configured_ones_alphabetically(self):
        accounts = [
            {"exchange": "Zeta", "name": "A", "source": "monitor"},
            {"exchange": "Alpha", "name": "B", "source": "monitor"},
            {"exchange": "OKX", "name": "C", "source": "monitor"},
        ]

        account_sort_key = load_account_sort_key(["OKX"])
        ordered = sorted(accounts, key=account_sort_key)

        self.assertEqual(
            [item["exchange"] for item in ordered],
            ["OKX", "Alpha", "Zeta"],
        )


if __name__ == "__main__":
    unittest.main()
