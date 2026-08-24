import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nav_service import state


class AccountConfigIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.index_path = Path(self.temp_dir.name) / "account_config_index.json"
        self.path_patch = patch.object(
            state,
            "ACCOUNT_CONFIG_INDEX_PATH",
            str(self.index_path),
        )
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def read_index(self):
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    @staticmethod
    def empty_index():
        return {
            "binance": {"length": 0, "accounts": []},
            "gate": {"length": 0, "accounts": []},
            "kucoin": {"length": 0, "accounts": []},
            "okx": {"length": 0, "accounts": []},
        }

    def test_empty_account_map_creates_empty_index(self):
        changed = state.sync_account_config_index({})

        self.assertTrue(changed)
        self.assertEqual(self.read_index(), self.empty_index())

    def test_index_contains_all_current_accounts_without_credentials(self):
        changed = state.sync_account_config_index({
            "Gate_A": {
                "exchange_id": "gate",
                "account_type": "account",
                "key": "gate-key",
                "secret": "gate-secret",
            },
            "Binance_A": {
                "exchange_id": "binance",
                "account_type": "classic",
                "key": "binance-key",
                "secret": "binance-secret",
            },
        })

        self.assertTrue(changed)
        expected = self.empty_index()
        expected["binance"]["length"] = 1
        expected["binance"]["accounts"] = [{
            "account_name": "Binance_A",
            "account_type": "classic",
        }]
        expected["gate"]["length"] = 1
        expected["gate"]["accounts"] = [{
            "account_name": "Gate_A",
            "account_type": "account",
        }]
        self.assertEqual(self.read_index(), expected)

    def test_unchanged_index_is_not_rewritten(self):
        account_map = {
            "Binance_A": {
                "exchange_id": "binance",
                "account_type": "account",
            }
        }
        self.assertTrue(state.sync_account_config_index(account_map))
        initial_mtime = self.index_path.stat().st_mtime_ns

        self.assertFalse(state.sync_account_config_index(account_map))
        self.assertEqual(self.index_path.stat().st_mtime_ns, initial_mtime)

    def test_legacy_index_is_rebuilt(self):
        self.index_path.write_text(
            json.dumps({"accounts": {"Old_A": {"exchange": "binance"}}}),
            encoding="utf-8",
        )

        self.assertTrue(state.sync_account_config_index({}))
        self.assertEqual(self.read_index(), self.empty_index())

    def test_append_account_config_rebuilds_complete_index(self):
        config_path = Path(self.temp_dir.name) / "accounts_config.yaml"
        config_path.write_text("", encoding="utf-8")

        account_info = {
            "key": "key",
            "secret": "secret",
            "initial_unit": 100,
            "principal": 100,
            "account_type": "account",
            "exchange": "binance",
            "exchange_id": "binance",
            "interest_rate": 0,
            "client": "ClientA",
            "ccy": "USDT",
        }

        class FakeAccountClass:
            creatable_account_types = ("account",)

            @staticmethod
            def normalize_credentials(_values):
                return {}

            @staticmethod
            def normalize_managed_credentials(_values):
                return {}

            @staticmethod
            def from_account_info(_name, _info):
                return SimpleNamespace()

        fake_registry = SimpleNamespace(reload=lambda: {"Binance_A": account_info})
        with (
            patch.object(state, "CONFIG_PATH", str(config_path)),
            patch.object(state, "account_registry", fake_registry),
            patch.object(state, "account_infos", {}),
            patch.object(state, "archived_account_infos", {}),
            patch.object(state, "accounts", {}),
            patch.object(
                state,
                "EXCHANGE_ACCOUNT_BY_ID",
                {"binance": FakeAccountClass},
            ),
            patch.object(state, "_on_account_added", None),
            patch.object(state, "start_account_update_task"),
        ):
            result = state.append_account_config(
                "Binance_A",
                "100",
                "USDT",
                "binance",
                "account",
                "ClientA",
                "0",
                "key",
                "secret",
            )

        self.assertEqual(result, account_info)
        self.assertEqual(self.read_index(), {
            "binance": {
                "length": 1,
                "accounts": [{
                    "account_name": "Binance_A",
                    "account_type": "account",
                }],
            },
        })


if __name__ == "__main__":
    unittest.main()
