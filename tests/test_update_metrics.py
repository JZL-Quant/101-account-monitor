import ast
import unittest
from pathlib import Path


class UpdateMetricsTests(unittest.TestCase):
    def test_metric_refresh_does_not_apply_feishu_filter(self):
        source = (Path(__file__).resolve().parents[1] / "account_monitor.py").read_text(
            encoding="utf-8-sig"
        )
        module = ast.parse(source)
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


if __name__ == "__main__":
    unittest.main()
