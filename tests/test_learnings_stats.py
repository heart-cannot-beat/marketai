"""Issue #166 -- dimension-value statistics, parsing and asset codes (pure).

Known datasets -> expected verdicts and intervals; insufficient data;
confounding and imbalance detection; CPA direction; Bonferroni within a
factor; determinism; CSV / pasted-table parsing and row validation.
"""
from __future__ import annotations

import math
import unittest
from datetime import date
from decimal import Decimal
from statistics import NormalDist

from app.experiments.judge import _wilson
from app.learnings import codes, parsing, stats
from app.learnings.stats import FactorSpec, RoundInput, Unit

PEOPLE = FactorSpec("people", "人物", [("people-1", "有人出镜"), ("people-2", "无人出镜")])
ANGLE = FactorSpec("angle", "文案切入", [("angle-1", "痛点"), ("angle-2", "卖点")])
DIRECTIONS = [("dir-1", "快充"), ("dir-2", "防水"), ("dir-3", "远程")]


def _units(
    *, ctr_with: float = 0.02, ctr_without: float = 0.013, impressions: int = 3000,
    conversions_with: int = 3, conversions_without: int = 2, per_cell: int = 2,
) -> list[Unit]:
    """Fully crossed: 3 directions x 2 people x 2 angles x ``per_cell`` assets."""
    units = []
    index = 0
    for direction, _title in DIRECTIONS:
        for people, ctr in (("people-1", ctr_with), ("people-2", ctr_without)):
            for angle in ("angle-1", "angle-2"):
                for _ in range(per_cell):
                    index += 1
                    jitter = 1 + 0.05 * ((index % 3) - 1)
                    units.append(Unit(
                        unit_id=f"av{index:03d}", direction=direction,
                        cells={"people": people, "angle": angle},
                        impressions=impressions, clicks=int(impressions * ctr * jitter),
                        spend=Decimal("30"),
                        conversions=Decimal(conversions_with if people == "people-1" else conversions_without),
                        ads=1,
                    ))
    return units


def _round(units: list[Unit], factors=(PEOPLE, ANGLE), directions=DIRECTIONS) -> RoundInput:
    return RoundInput(round_id="plan_1", factors=list(factors), directions=list(directions), units=units)


def _value(result: dict, factor: str, value_id: str) -> dict:
    found = next(item for item in result["factors"] if item["key"] == factor)
    return next(item for item in found["values"] if item["value_id"] == value_id)


class TestMainEffects(unittest.TestCase):
    def test_a_clear_ctr_difference_is_a_winner_with_an_interval_and_n(self):
        result = stats.analyze_round(_round(_units()), input_hash="h1")
        with_people = _value(result, "people", "people-1")
        without = _value(result, "people", "people-2")
        self.assertEqual(with_people["verdict"], "winner")
        self.assertEqual(with_people["verdict_metric"], "ctr")
        self.assertEqual(without["verdict"], "loser")
        ctr = with_people["ctr"]
        self.assertGreater(float(ctr["lift_low"]), 0)
        self.assertLess(float(ctr["lift_low"]), float(ctr["lift"]))
        self.assertGreater(float(ctr["lift_high"]), float(ctr["lift"]))
        self.assertEqual(ctr["n"]["assets"], 12)
        self.assertEqual(ctr["n"]["impressions"], 36000)
        self.assertEqual(ctr["adjusted_alpha"], "0.0500")
        self.assertTrue(with_people["sentence"].startswith("有人出镜 比 无人出镜 CTR 高 "), with_people["sentence"])
        self.assertIn("95% 区间 +", with_people["sentence"])
        self.assertIn("n=36,000 次展示 / 12 张素材", with_people["sentence"])
        self.assertIn("CTR 低", without["sentence"])
        # CPA is not significant at these counts and says so after the CTR result.
        self.assertEqual(with_people["cpa"]["verdict"], "no_difference")
        self.assertIn("CPA 差异尚不显著", with_people["sentence"])

    def test_the_interval_contains_the_katz_interval_and_the_wilson_estimate_matches_judge(self):
        units = _units()
        result = stats.analyze_round(_round(units), input_hash="h1")
        ctr = _value(result, "people", "people-1")["ctr"]
        group = [u for u in units if u.cells["people"] == "people-1"]
        rest = [u for u in units if u.cells["people"] == "people-2"]
        c1, n1 = sum(u.clicks for u in group), sum(u.impressions for u in group)
        c2, n2 = sum(u.clicks for u in rest), sum(u.impressions for u in rest)
        point = (c1 / n1) / (c2 / n2)
        se = math.sqrt(1 / c1 - 1 / n1 + 1 / c2 - 1 / n2)
        z = NormalDist().inv_cdf(0.975)
        self.assertAlmostEqual(float(ctr["lift"]), point - 1, places=5)
        self.assertLessEqual(float(ctr["lift_low"]), point * math.exp(-z * se) - 1 + 1e-6)
        self.assertGreaterEqual(float(ctr["lift_high"]), point * math.exp(z * se) - 1 - 1e-6)
        estimate, low, high = _wilson(c1, n1, 0.05)
        self.assertAlmostEqual(float(ctr["estimate"]), float(estimate), places=6)
        self.assertAlmostEqual(float(ctr["interval_low"]), float(low), places=6)
        self.assertAlmostEqual(float(ctr["interval_high"]), float(high), places=6)

    def test_equal_rates_are_no_difference_not_a_forced_winner(self):
        result = stats.analyze_round(_round(_units()), input_hash="h1")
        for value_id in ("angle-1", "angle-2"):
            value = _value(result, "angle", value_id)
            self.assertEqual(value["verdict"], "no_difference", value["sentence"])
            self.assertIn("差异不显著", value["sentence"])

    def test_same_input_same_output_and_the_seed_comes_from_the_input_hash(self):
        units = _units()
        first = stats.analyze_round(_round(units), input_hash="h1")
        again = stats.analyze_round(_round(list(reversed(units))), input_hash="h1")
        self.assertEqual(first, again)
        other = stats.analyze_round(_round(units), input_hash="h2")
        self.assertEqual(_value(other, "people", "people-1")["verdict"], "winner")

    def test_a_lower_cpa_is_a_cpa_winner(self):
        units = _units(ctr_with=0.015, ctr_without=0.015, conversions_with=12, conversions_without=4, impressions=4000)
        result = stats.analyze_round(_round(units), input_hash="h1")
        value = _value(result, "people", "people-1")
        self.assertEqual(value["cpa"]["verdict"], "winner")
        self.assertEqual(value["verdict_metric"], "cpa")
        self.assertLess(float(value["cpa"]["lift"]), 0)
        self.assertIn("CPA 低", value["sentence"])
        self.assertIn("次购买", value["sentence"])
        self.assertEqual(_value(result, "people", "people-2")["verdict"], "loser")


class TestInsufficientData(unittest.TestCase):
    def test_below_the_minimum_impressions_and_clicks_nothing_is_concluded(self):
        units = _units(impressions=60, conversions_with=0, conversions_without=0)
        result = stats.analyze_round(_round(units), input_hash="h1")
        value = _value(result, "people", "people-1")
        self.assertEqual(value["verdict"], "insufficient_data")
        self.assertIn("value_low_clicks", value["ctr"]["reasons"])
        self.assertIn("样本不足，尚不能下结论", value["sentence"])
        self.assertIn("每组至少 1,000 次展示、30 次点击、2 张素材", value["sentence"])
        self.assertIsNone(value["ctr"]["lift"])

    def test_one_asset_per_side_is_not_enough_however_many_impressions(self):
        units = [
            Unit("a", "dir-1", {"people": "people-1"}, 500000, 10000),
            Unit("b", "dir-1", {"people": "people-2"}, 500000, 6000),
        ]
        result = stats.analyze_round(_round(units, factors=(PEOPLE,), directions=[("dir-1", "快充")]), input_hash="h")
        value = _value(result, "people", "people-1")
        self.assertEqual(value["verdict"], "insufficient_data")
        self.assertIn("value_low_assets", value["ctr"]["reasons"])
        self.assertIn("baseline_low_assets", value["ctr"]["reasons"])

    def test_no_data_and_no_baseline(self):
        units = [Unit(f"a{i}", "dir-1", {"people": "people-1"}, 5000, 100) for i in range(3)]
        result = stats.analyze_round(_round(units, factors=(PEOPLE,), directions=[("dir-1", "快充")]), input_hash="h")
        self.assertIn("no_baseline", _value(result, "people", "people-1")["ctr"]["reasons"])
        missing = _value(result, "people", "people-2")
        self.assertIn("no_data", missing["ctr"]["reasons"])
        self.assertEqual(missing["sentence"], "无人出镜：尚无投放数据")

    def test_unknown_conversions_leave_the_asset_out_of_cpa(self):
        units = [
            Unit(f"a{i}", "dir-1", {"people": "people-1" if i % 2 else "people-2"}, 5000, 80, None, None)
            for i in range(8)
        ]
        result = stats.analyze_round(_round(units, factors=(PEOPLE,), directions=[("dir-1", "快充")]), input_hash="h")
        value = _value(result, "people", "people-1")
        self.assertEqual(value["cpa"]["n"]["assets"], 0)
        self.assertIn("no_data", value["cpa"]["reasons"])
        self.assertEqual(result["totals"]["cpa_assets"], 0)


class TestConfounding(unittest.TestCase):
    def test_a_value_tied_to_one_direction_is_confounded(self):
        # Scene s1 only ever appears in dir-1, s2 only in dir-2: the scene
        # effect cannot be told apart from the direction effect.
        scene = FactorSpec("scene", "场景", [("s1", "居家"), ("s2", "户外")])
        units = []
        for i in range(8):
            direction = "dir-1" if i < 4 else "dir-2"
            units.append(Unit(
                f"a{i}", direction, {"scene": "s1" if i < 4 else "s2", "people": "people-1" if i % 2 else "people-2"},
                5000, 150 if i < 4 else 60,
            ))
        result = stats.analyze_round(
            _round(units, factors=(scene, PEOPLE), directions=DIRECTIONS[:2]), input_hash="h",
        )
        value = _value(result, "scene", "s1")
        self.assertEqual(value["verdict"], "insufficient_data")
        self.assertIn("confounded", value["flags"])
        self.assertEqual(value["ctr"]["balance"]["factor_key"], "direction")
        self.assertEqual(value["ctr"]["balance"]["max_tvd"], "1.0000")
        self.assertIn("效果分不开", value["sentence"])
        # People is crossed with both directions -> balanced, a real verdict.
        self.assertNotIn("confounded", _value(result, "people", "people-1")["flags"])

    def test_moderate_imbalance_keeps_the_verdict_with_a_caveat(self):
        units = _units(per_cell=2)
        # Deliver dir-1 mostly to "with people": impressions shift the balance.
        shifted = []
        for unit in units:
            if unit.direction == "dir-1" and unit.cells["people"] == "people-1":
                shifted.append(Unit(unit.unit_id, unit.direction, unit.cells, 12000, int(12000 * 0.02), unit.spend, unit.conversions, 1))
            else:
                shifted.append(unit)
        result = stats.analyze_round(_round(shifted), input_hash="h")
        value = _value(result, "people", "people-1")
        tvd = float(value["ctr"]["balance"]["max_tvd"])
        self.assertGreaterEqual(tvd, 0.25)
        self.assertLess(tvd, 0.5)
        self.assertIn("imbalanced", value["flags"])
        self.assertEqual(value["verdict"], "winner")
        self.assertIn("其他因素分布不均", value["sentence"])

    def test_three_values_share_a_bonferroni_level(self):
        scene = FactorSpec("scene", "场景", [("s1", "居家"), ("s2", "户外"), ("s3", "办公")])
        units = [
            Unit(f"a{i}", "dir-1", {"scene": f"s{i % 3 + 1}"}, 4000, 60 + (i % 3) * 10) for i in range(12)
        ]
        result = stats.analyze_round(_round(units, factors=(scene,), directions=[("dir-1", "快充")]), input_hash="h")
        value = _value(result, "scene", "s1")
        self.assertEqual(value["ctr"]["adjusted_alpha"], "0.0167")
        self.assertEqual(value["compared_with"], "其他取值")

    def test_direction_comparisons_ignore_factors_bound_to_directions(self):
        result = stats.analyze_round(_round(_units()), input_hash="h1")
        first = result["directions"][0]
        self.assertEqual(first["title"], "快充")
        self.assertEqual(first["compared_with"], "其他方向")
        self.assertIn(first["verdict"], ("no_difference", "winner", "loser"))
        self.assertEqual(result["totals"]["assets"], 24)


class TestParsing(unittest.TestCase):
    def test_template_headers_parse_and_normalize(self):
        text = (
            "素材编号,方向,画面组合,开始日期,结束日期,展示次数,链接点击量,花费,购买次数,币种\n"
            "mk-7f3k2q,快充,居家 · 真人,2026/09/01,2026-09-07,\"1,200\",36,$12.50,2,usd\n"
        )
        table = parsing.parse_table(text)
        self.assertIn("方向", table.ignored_columns)
        row = parsing.normalize_row(table.rows[0], default_currency=None, today=date(2026, 10, 1))
        self.assertIsNone(row.error)
        self.assertEqual(row.asset_code, "MK-7F3K2Q")
        self.assertEqual((row.date_start, row.date_end), (date(2026, 9, 1), date(2026, 9, 7)))
        self.assertEqual((row.impressions, row.clicks), (1200, 36))
        self.assertEqual(row.spend, Decimal("12.50"))
        self.assertEqual(row.currency, "USD")

    def test_ads_manager_export_headers_and_all_clicks_is_ignored(self):
        text = (
            "Ad name\tReporting starts\tReporting ends\tImpressions\tClicks (all)\tLink clicks\t"
            "Amount spent (USD)\tPurchases\n"
            "夏季_MK-7F3K2Q_v2\t2026-09-01\t2026-09-07\t5000\t400\t90\t30\t3\n"
        )
        table = parsing.parse_table(text)
        self.assertEqual(table.header_currency, "USD")
        self.assertEqual(table.columns["clicks"], "Link clicks")
        self.assertIn("Clicks (all)", table.ignored_columns)
        row = parsing.normalize_row(table.rows[0], default_currency=table.header_currency, today=date(2026, 10, 1))
        self.assertIsNone(row.error)
        self.assertEqual(row.clicks, 90)
        self.assertEqual(codes.codes_in_text(row.ad_name), ["MK-7F3K2Q"])

    def test_missing_columns_are_refused_with_a_reason(self):
        with self.assertRaises(parsing.ParseError) as raised:
            parsing.parse_table("开始日期,结束日期,展示次数,链接点击量\n2026-09-01,2026-09-02,1,1\n")
        self.assertEqual(raised.exception.code, "missing_identifier_column")
        with self.assertRaises(parsing.ParseError) as raised:
            parsing.parse_table("素材编号,开始日期,结束日期,展示次数\nMK-7F3K2Q,2026-09-01,2026-09-02,1\n")
        self.assertEqual(raised.exception.code, "missing_clicks_column")

    def test_row_validation_reasons(self):
        today = date(2026, 10, 1)
        base = {"line": 2, "asset_code": "MK-7F3K2Q", "date_start": "2026-09-01", "date_end": "2026-09-07",
                "impressions": "100", "clicks": "5"}
        cases = {
            "invalid_date": {"date_start": "9月1日"},
            "date_order": {"date_end": "2026-08-01"},
            "future_date": {"date_end": "2026-12-01"},
            "clicks_exceed_impressions": {"clicks": "500"},
            "negative_number": {"spend": "-3"},
            "not_integer": {"impressions": "10.5"},
            "invalid_number": {"clicks": "很多"},
            "currency_missing": {"spend": "5"},
            "missing_identifier": {"asset_code": ""},
        }
        for code, override in cases.items():
            with self.subTest(code=code):
                row = parsing.normalize_row({**base, **override}, default_currency=None, today=today)
                self.assertEqual(row.error, code)
                self.assertIn(code, parsing.REASONS)

    def test_codes_are_stable_printable_and_tolerant(self):
        code = codes.asset_code("ws_1", "av_1")
        self.assertRegex(code, r"^MK-[0-9A-HJKMNP-TV-Z]{6}$")
        self.assertEqual(code, codes.asset_code("ws_1", "av_1"))
        self.assertNotEqual(code, codes.asset_code("ws_2", "av_1"))
        self.assertEqual(codes.normalize_code(code.lower()), code)
        self.assertEqual(codes.normalize_code(code.replace("-", "")), code)
        self.assertEqual(codes.normalize_code(code[3:]), code)
        self.assertIsNone(codes.normalize_code("hello"))
        self.assertEqual(codes.codes_in_text(f"ad {code} and {code}"), [code])

    def test_percent_formatting(self):
        self.assertEqual(stats.percent(0.38), "+38%")
        self.assertEqual(stats.percent(-0.123), "-12%")
        self.assertEqual(stats.percent(0.004), "+0.4%")
        self.assertEqual(stats.percent(0.0), "0%")
        self.assertEqual(stats.percent(-0.25, signed=False), "25%")


if __name__ == "__main__":
    unittest.main()
