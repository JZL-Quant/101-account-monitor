import unittest

from core.return_attention import build_return_performance_sections, classify_z


def row(name, exchange, value_24h, value_7d=None):
    return {
        "account_name": name,
        "exchange_id": exchange.lower(),
        "exchange_label": exchange,
        "ccy": "USDT",
        "annualized_return_24h": value_24h,
        "annualized_return_7d": value_7d,
    }


class ReturnAttentionTests(unittest.TestCase):
    def test_threshold_boundaries(self):
        self.assertEqual(classify_z(2.5), "outstanding")
        self.assertEqual(classify_z(-1.5), "attention")
        self.assertEqual(classify_z(-2.0), "priority_attention")
        self.assertIsNone(classify_z(-1.4999))

    def test_groups_by_exchange_and_ignores_invalid_values_per_period(self):
        rows = [row(f"A{i}", "Binance", 0.0, None) for i in range(7)]
        rows.append(row("A7", "Binance", 10.0, 99.0))
        rows.extend(row(f"B{i}", "Gate", 0.0, 1.0) for i in range(7))
        rows.append(row("B7", "Gate", -10.0, float("nan")))

        sections = build_return_performance_sections(rows)

        self.assertEqual([section["period"] for section in sections], ["24h", "7D"])
        rows_24h = sections[0]["rows"]
        self.assertEqual({item["account_name"] for item in rows_24h}, {"A7", "B7"})
        self.assertEqual(
            {item["level"] for item in rows_24h},
            {"outstanding", "priority_attention"},
        )
        self.assertEqual(sections[1]["rows"], [])

    def test_small_or_zero_variance_groups_are_skipped(self):
        rows = [row(f"A{i}", "Binance", 1.0) for i in range(5)]
        rows.extend(row(f"B{i}", "Gate", value) for i, value in enumerate([1, 2, 3, 4]))

        sections = build_return_performance_sections(rows)

        self.assertTrue(all(section["rows"] == [] for section in sections))


if __name__ == "__main__":
    unittest.main()
