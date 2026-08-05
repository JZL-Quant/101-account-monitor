import ast
import unittest
from pathlib import Path


class UpdateMetricsTests(unittest.TestCase):
    @staticmethod
    def _monitor_module():
        source = (Path(__file__).resolve().parents[1] / "account_monitor.py").read_text(
            encoding="utf-8-sig"
        )
        return ast.parse(source)

    @classmethod
    def _load_function(cls, name, namespace=None):
        module = cls._monitor_module()
        function = next(
            node
            for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        )
        namespace = dict(namespace or {})
        exec(compile(ast.Module(body=[function], type_ignores=[]), "<test>", "exec"), namespace)
        return namespace[name]

    def test_metric_refresh_does_not_apply_feishu_filter(self):
        module = self._monitor_module()
        update_metrics = next(
            node
            for node in module.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "update_metrics"
        )

        called_functions = {
            node.func.id
            for node in ast.walk(update_metrics)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        self.assertNotIn("_default_feishu_accounts", called_functions)
        self.assertIn("accounts", {node.id for node in ast.walk(update_metrics) if isinstance(node, ast.Name)})

    def test_exchange_filter_accepts_a_requested_exchange(self):
        exchange_accounts = self._load_function("_exchange_accounts")
        account_map = {
            "KC": {"exchange_id": "KuCoin"},
            "OK": {"exchange_id": "OKX"},
            "BN": {"exchange_id": "binance"},
        }

        self.assertEqual(set(exchange_accounts(account_map, "kucoin")), {"KC"})
        self.assertEqual(set(exchange_accounts(account_map, "OKX")), {"OK"})

    def test_kucoin_and_okx_are_excluded_from_default_feishu(self):
        default_accounts = self._load_function(
            "_default_feishu_accounts",
            {"TEST_ONLY_FEISHU_EXCHANGES": ("kucoin", "okx")},
        )
        account_map = {
            "KC": {"exchange_id": "kucoin"},
            "OK": {"exchange_id": "okx"},
            "BN": {"exchange_id": "binance"},
            "Skipped": {"exchange_id": "gate", "skip_default_feishu": True},
        }

        self.assertEqual(set(default_accounts(account_map)), {"BN"})


if __name__ == "__main__":
    unittest.main()
