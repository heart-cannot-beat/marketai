"""Parsers from raw insight and configuration pages to typed observations (#108, #101 S2).

The parsers are pure: these tests hand them Meta-shaped page bodies, built by
hand or by the sandbox's own row builders, and check the value objects --
and, for every way a page set cannot be evidence, the typed refusal
(Scale contract §8): another ad's rows, duplicate rows, a currency or time
zone that disagrees with the contract, negative values, and more clicks than
impressions. A day of a complete read without a row is observed zero
delivery, and a privacy-suppressed segment keeps no value.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal

from app.adapters.meta import fake_adapter, sandbox_timeline
from app.meta.scale_read_contracts import AccountBinding
from app.meta.scale_read_parsers import (
    ScaleReadParseError,
    parse_breakdown,
    parse_daily,
    parse_delivery_configuration,
    parse_window,
)

ACCOUNT = AccountBinding("act_acme_charge_sandbox", "USD", "America/Los_Angeles")
AD = "ad_fake_acme_charge_1"
OTHER_AD = "ad_fake_brightvolt_like_2"
SINCE, UNTIL = date(2026, 11, 3), date(2026, 11, 9)
FIELDS = ["account_currency", "account_id", "ad_id", "spend", "impressions", "inline_link_clicks", "actions",
          "action_values", "reach", "frequency"]


def row(day: date, ad_id: str = AD, **overrides) -> dict:
    values = {
        "account_id": "acme_charge_sandbox",  # Meta reports it without "act_"
        "account_currency": "USD",
        "ad_id": ad_id,
        "date_start": day.isoformat(),
        "date_stop": day.isoformat(),
        "spend": "12.34",
        "impressions": "1000",
        "clicks": "40",
        "inline_link_clicks": "25",
        "actions": [{"action_type": "purchase", "value": "3"}, {"action_type": "link_click", "value": "25"}],
        "action_values": [{"action_type": "purchase", "value": "89.70"}],
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


def page(*rows: dict) -> dict:
    return {"data": list(rows), "paging": {"cursors": {}}}


class DailyParser(unittest.TestCase):
    def _parse(self, *pages, since=SINCE, until=UNTIL):
        return parse_daily(list(pages), expected=ACCOUNT, ad_id=AD, since=since, until=until)

    def test_every_day_is_present_and_a_day_without_a_row_is_observed_zero(self):
        observed = self._parse(page(row(date(2026, 11, 3)), row(date(2026, 11, 5))), page(row(date(2026, 11, 9))))
        self.assertEqual([day.local_day for day in observed.days],
                         [SINCE + timedelta(days=n) for n in range(7)])
        first = observed.days[0]
        self.assertEqual((first.delivered, first.spend, first.impressions, first.link_clicks, first.purchases,
                          first.purchase_value),
                         (True, Decimal("12.34"), 1000, 25, 3, Decimal("89.70")))
        quiet = observed.days[1]
        self.assertEqual((quiet.delivered, quiet.spend, quiet.impressions, quiet.link_clicks, quiet.purchases,
                          quiet.purchase_value), (False, Decimal(0), 0, 0, 0, Decimal(0)),
                         "Meta omits days without delivery: present, delivered=false, zeros (#101 Q7)")
        self.assertEqual((observed.currency, observed.timezone_name, observed.ad_account_id),
                         ("USD", "America/Los_Angeles", "act_acme_charge_sandbox"))

    def test_another_ads_rows_never_change_this_ads_values(self):
        alone = self._parse(page(row(SINCE)))
        mixed = self._parse(page(row(SINCE, OTHER_AD, spend="999.00", impressions="99999"), row(SINCE),
                                 row(UNTIL, OTHER_AD)))
        self.assertEqual(alone, mixed)
        only_others = self._parse(page(row(SINCE, OTHER_AD), row(UNTIL, OTHER_AD)))
        self.assertFalse(any(day.delivered for day in only_others.days))

    def test_another_ads_row_must_still_belong_to_the_account_and_currency(self):
        for name, bad, reason in (
            ("account", row(SINCE, OTHER_AD, account_id="kestrel_power_account"), "account_mismatch"),
            ("currency", row(SINCE, OTHER_AD, account_currency="EUR"), "currency_mismatch"),
        ):
            with self.subTest(name), self.assertRaises(ScaleReadParseError) as raised:
                self._parse(page(row(SINCE), bad))
            self.assertEqual(raised.exception.reason, reason)

    def test_a_duplicate_row_is_refused(self):
        with self.assertRaises(ScaleReadParseError) as raised:
            self._parse(page(row(SINCE)), page(row(SINCE, spend="1.00")))
        self.assertEqual(raised.exception.reason, "duplicate_row")

    def test_a_currency_other_than_the_contracts_is_refused(self):
        with self.assertRaises(ScaleReadParseError) as raised:
            self._parse(page(row(SINCE, account_currency="CAD")))
        self.assertEqual(raised.exception.reason, "currency_mismatch")
        with self.assertRaises(ScaleReadParseError) as raised:
            self._parse(page(row(SINCE, account_currency=None)))
        self.assertEqual(raised.exception.reason, "currency_mismatch", "a row without its currency proves nothing")

    def test_negative_values_are_refused(self):
        for field, value in (("spend", "-0.01"), ("impressions", "-1"), ("inline_link_clicks", "-2"),
                             ("actions", [{"action_type": "purchase", "value": "-1"}]),
                             ("action_values", [{"action_type": "purchase", "value": "-5.00"}])):
            with self.subTest(field), self.assertRaises(ScaleReadParseError) as raised:
                self._parse(page(row(SINCE, **{field: value})))
            self.assertEqual(raised.exception.reason, "negative_value")

    def test_more_clicks_than_impressions_is_refused(self):
        for overrides in ({"inline_link_clicks": "1001", "clicks": "1001"}, {"clicks": "1500"}):
            with self.subTest(overrides), self.assertRaises(ScaleReadParseError) as raised:
                self._parse(page(row(SINCE, **overrides)))
            self.assertEqual(raised.exception.reason, "clicks_exceed_impressions")

    def test_a_row_outside_the_window_or_spanning_days_is_refused(self):
        for bad in (row(SINCE - timedelta(days=1)), row(UNTIL + timedelta(days=1)),
                    row(SINCE, date_stop=(SINCE + timedelta(days=1)).isoformat())):
            with self.subTest(bad["date_start"]), self.assertRaises(ScaleReadParseError) as raised:
                self._parse(page(bad))
            self.assertEqual(raised.exception.reason, "outside_window")

    def test_malformed_rows_are_refused(self):
        for overrides in ({"spend": "twelve"}, {"impressions": "10.5"}, {"spend": None}, {"spend": "NaN"},
                          {"actions": "3 purchases"}, {"date_start": "yesterday"},
                          {"actions": [{"action_type": "purchase", "value": "1.5"}]}):
            with self.subTest(overrides), self.assertRaises(ScaleReadParseError) as raised:
                self._parse(page(row(SINCE, **overrides)))
            self.assertEqual(raised.exception.reason, "malformed_row")
        with self.assertRaises(ScaleReadParseError) as raised:
            self._parse({"paging": {}})
        self.assertEqual(raised.exception.reason, "malformed_row")

    def test_purchases_count_only_the_contracts_action_type(self):
        observed = self._parse(page(row(SINCE, actions=[{"action_type": "add_to_cart", "value": "9"}],
                                        action_values=[{"action_type": "add_to_cart", "value": "50"}])))
        self.assertEqual((observed.days[0].purchases, observed.days[0].purchase_value), (0, Decimal(0)))


class WindowParser(unittest.TestCase):
    def _window_row(self, ad_id=AD, **overrides):
        values = {"date_stop": UNTIL.isoformat(), "reach": "700", "frequency": "1.428571", "impressions": "1000"}
        return row(SINCE, ad_id, **{**values, **overrides})

    def _parse(self, *pages):
        return parse_window(list(pages), expected=ACCOUNT, ad_id=AD, window_role="current_7d",
                            since=SINCE, until=UNTIL)

    def test_the_windows_own_reach_and_frequency(self):
        observed = self._parse(page(self._window_row(OTHER_AD, reach="5"), self._window_row()))
        self.assertEqual((observed.delivered, observed.reach, observed.frequency, observed.impressions,
                          observed.window_role), (True, 700, Decimal("1.428571"), 1000, "current_7d"))

    def test_a_window_without_delivery_has_no_frequency(self):
        observed = self._parse(page(self._window_row(OTHER_AD)))
        self.assertEqual((observed.delivered, observed.reach, observed.frequency, observed.spend),
                         (False, 0, None, Decimal(0)))

    def test_a_window_must_be_the_exact_range_once(self):
        with self.assertRaises(ScaleReadParseError) as raised:
            self._parse(page(row(SINCE, reach="700", frequency="1.4")))  # a daily row
        self.assertEqual(raised.exception.reason, "outside_window")
        with self.assertRaises(ScaleReadParseError) as raised:
            self._parse(page(self._window_row(), self._window_row()))
        self.assertEqual(raised.exception.reason, "duplicate_row")
        for overrides, reason in (({"reach": "1001"}, "malformed_row"), ({"reach": None}, "malformed_row"),
                                  ({"frequency": "-1"}, "negative_value")):
            with self.subTest(overrides), self.assertRaises(ScaleReadParseError) as raised:
                self._parse(page(self._window_row(**overrides)))
            self.assertEqual(raised.exception.reason, reason)


class BreakdownParser(unittest.TestCase):
    BREAKDOWNS = ("country",)

    def _parse(self, *pages):
        return parse_breakdown(list(pages), expected=ACCOUNT, ad_id=AD, breakdowns=self.BREAKDOWNS,
                               window_role="current_7d", since=SINCE, until=UNTIL)

    def test_segments_by_day_and_a_suppressed_segment_keeps_no_value(self):
        suppressed = {key: value for key, value in row(SINCE, country="GB").items()
                      if key not in ("spend", "impressions", "clicks", "inline_link_clicks", "actions",
                                     "action_values")}
        observed = self._parse(page(row(SINCE, country="US"), row(UNTIL, country="US", spend="1.00"),
                                    row(SINCE, country="CA"), suppressed,
                                    row(UNTIL, country="GB")))  # GB observed one day, withheld another
        by_key = {segment.segment_key: segment for segment in observed.segments}
        self.assertEqual(list(by_key), ["CA", "GB", "US"])
        self.assertEqual((by_key["GB"].state, by_key["GB"].suppression_reason, by_key["GB"].days),
                         ("privacy_suppressed", "api_privacy_threshold", ()),
                         "never zero, never a value: a typed placeholder (§13)")
        us = by_key["US"]
        self.assertEqual((us.state, len(us.days), us.days[0].spend, us.days[-1].spend, us.days[1].delivered),
                         ("observed", 7, Decimal("12.34"), Decimal("1.00"), False))

    def test_breakdown_negatives(self):
        cases = (
            (page(row(SINCE, country="US"), row(SINCE, country="US")), "duplicate_row"),
            (page(row(SINCE)), "malformed_row"),  # the segment value is missing
            (page(row(SINCE, country="US", inline_link_clicks="2000")), "clicks_exceed_impressions"),
            (page(row(SINCE, country="US", account_currency="GBP")), "currency_mismatch"),
        )
        for pages, reason in cases:
            with self.subTest(reason), self.assertRaises(ScaleReadParseError) as raised:
                self._parse(pages)
            self.assertEqual(raised.exception.reason, reason)


class ConfigurationParser(unittest.TestCase):
    def _pages(self, **overrides):
        account = {"id": "act_acme_charge_sandbox", "account_id": "acme_charge_sandbox", "currency": "USD",
                   "timezone_name": "America/Los_Angeles"}
        account.update(overrides.pop("account", {}))
        ad = {"id": AD, "account_id": "acme_charge_sandbox", "adset_id": "set_1", "campaign_id": "cmp_1",
              "creative": {"id": "creative_1"}, "status": "ACTIVE", "effective_status": "ACTIVE"}
        ad.update(overrides.pop("ad", {}))
        adset = {"id": "set_1", "campaign_id": "cmp_1", "status": "ACTIVE", "effective_status": "ACTIVE",
                 "daily_budget": "5000", "optimization_goal": "OFFSITE_CONVERSIONS",
                 "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                 "attribution_spec": [{"event_type": "CLICK_THROUGH", "window_days": 7}],
                 "targeting": {"geo_locations": {"countries": ["US"]}, "age_min": 25},
                 "learning_stage_info": {"status": "FAIL"}}
        adset.update(overrides.pop("adset", {}))
        campaign = {"id": "cmp_1", "status": "ACTIVE", "effective_status": "ACTIVE"}
        campaign.update(overrides.pop("campaign", {}))
        other_ad = dict(ad, id=OTHER_AD)
        return [("account", page(account)), ("ads", page(other_ad, ad)), ("adsets", page(adset)),
                ("campaigns", page(campaign))]

    def _parse(self, edge_pages):
        return parse_delivery_configuration(edge_pages, expected=ACCOUNT, ad_id=AD)

    def test_the_ads_configuration(self):
        observed = self._parse(self._pages())
        self.assertEqual((observed.ad_creative_id, observed.ad_status, observed.adset_id, observed.campaign_id,
                          observed.budget_source, observed.budget_amount_minor, observed.optimization_goal,
                          observed.bid_strategy, observed.learning_stage),
                         ("creative_1", "ACTIVE", "set_1", "cmp_1", "adset_daily", 5000, "OFFSITE_CONVERSIONS",
                          "LOWEST_COST_WITHOUT_CAP", "learning_limited"))
        self.assertEqual(len(observed.targeting_safety_hash), 64)
        self.assertNotIn("geo_locations", repr(observed), "targeting is kept only as a hash (§4.2)")
        campaign_budget = self._parse(self._pages(campaign={"lifetime_budget": "90000"}))
        self.assertEqual((campaign_budget.budget_source, campaign_budget.budget_amount_minor),
                         ("campaign_lifetime", 90000))
        unknown = self._parse(self._pages(adset={"learning_stage_info": None}))
        self.assertEqual(unknown.learning_stage, "unknown", "never guessed (§19)")

    def test_the_account_must_agree_with_the_contract(self):
        for overrides, reason in (
            ({"account": {"timezone_name": "Europe/London"}}, "timezone_mismatch"),
            ({"account": {"currency": "EUR"}}, "currency_mismatch"),
            ({"account": {"account_id": "northpeak_account", "id": "act_northpeak_account"}}, "account_mismatch"),
            ({"ad": {"account_id": "northpeak_account"}}, "account_mismatch"),
        ):
            with self.subTest(overrides), self.assertRaises(ScaleReadParseError) as raised:
                self._parse(self._pages(**overrides))
            self.assertEqual(raised.exception.reason, reason)

    def test_the_ad_must_resolve_to_one_ad_set_and_campaign(self):
        pages = self._pages()
        with self.assertRaises(ScaleReadParseError) as raised:
            parse_delivery_configuration(pages, expected=ACCOUNT, ad_id="ad_missing")
        self.assertEqual(raised.exception.reason, "ad_not_found")
        doubled = pages[:2] + [("adsets", pages[2][1]), ("adsets", pages[2][1])] + pages[3:]
        with self.assertRaises(ScaleReadParseError) as raised:
            self._parse(doubled)
        self.assertEqual(raised.exception.reason, "duplicate_row")
        for overrides in ({"adset": {"daily_budget": None}}, {"ad": {"creative": {}}},
                          {"ad": {"campaign_id": "cmp_other"}}):
            with self.subTest(overrides), self.assertRaises(ScaleReadParseError) as raised:
                self._parse(self._pages(**overrides))
            self.assertEqual(raised.exception.reason, "malformed_row")


class SandboxRoundTrip(unittest.TestCase):
    """The sandbox's pages parse into exactly what its timeline says."""

    ADS = ["ad_fake_acme_charge_1", "ad_fake_acme_charge_2", "ad_fake_acme_charge_3"]
    D = date(2026, 11, 9)

    def test_daily_pages_parse_to_the_timeline(self):
        days = [self.D - timedelta(days=n) for n in range(13, -1, -1)]
        rows = fake_adapter._daily_rows(ACCOUNT, self.ADS, days, FIELDS, ())
        pages, offset = [], 0
        while True:  # the sandbox's own paging, 25 rows a page
            current = fake_adapter._page(rows, offset, 25)
            pages.append(current.payload)
            if current.next_cursor is None:
                break
            offset += current.row_count
        self.assertEqual(len(pages), 2)
        for ad_id in self.ADS:
            observed = parse_daily(pages, expected=ACCOUNT, ad_id=ad_id, since=days[0], until=days[-1])
            states = [sandbox_timeline.day_state(ad_id, day) for day in days]
            self.assertEqual([day.delivered for day in observed.days],
                             [state == sandbox_timeline.DELIVERING for state in states])
            self.assertEqual(states.count(sandbox_timeline.PAUSED), 1)
            self.assertEqual(states.count(sandbox_timeline.ZERO_DELIVERY), 1)
            for day in observed.days:
                values = sandbox_timeline.day_values(ad_id, day.local_day)
                if values is not None:
                    self.assertEqual((day.impressions, day.link_clicks, day.spend),
                                     (values.impressions, values.link_clicks, Decimal(values.spend_cents) / 100))

    def test_window_pages_parse_to_reach_at_most_impressions(self):
        days = [self.D - timedelta(days=n) for n in range(6, -1, -1)]
        payload = fake_adapter._page(fake_adapter._window_rows(ACCOUNT, self.ADS, days, FIELDS), 0, 25).payload
        for ad_id in self.ADS:
            observed = parse_window([payload], expected=ACCOUNT, ad_id=ad_id, window_role="current_7d",
                                    since=days[0], until=days[-1])
            daily = [sandbox_timeline.day_values(ad_id, day) for day in days]
            self.assertEqual(observed.impressions, sum(values.impressions for values in daily if values))
            busiest = max(values.impressions for values in daily if values)
            self.assertTrue(busiest <= observed.reach <= observed.impressions)
            self.assertEqual(observed.frequency.quantize(Decimal("0.01")),
                             (Decimal(observed.impressions) / observed.reach).quantize(Decimal("0.01")))

    def test_breakdown_pages_carry_suppressed_segments(self):
        days = [self.D - timedelta(days=n) for n in range(6, -1, -1)]
        breakdowns = ("age", "gender")
        payload = fake_adapter._page(fake_adapter._daily_rows(ACCOUNT, self.ADS, days, FIELDS, breakdowns),
                                     0, 10_000).payload
        states = set()
        for ad_id in self.ADS:
            observed = parse_breakdown([payload], expected=ACCOUNT, ad_id=ad_id, breakdowns=breakdowns,
                                       window_role="current_7d", since=days[0], until=days[-1])
            states |= {segment.state for segment in observed.segments}
            for segment in observed.segments:
                if segment.state == "observed":
                    self.assertTrue(all(day.link_clicks <= day.impressions for day in segment.days))
        self.assertEqual(states, {"observed", "privacy_suppressed"})


class SandboxTimeline(unittest.TestCase):
    def test_every_14_days_hold_one_paused_and_one_idle_day_and_never_seven_quiet_in_a_row(self):
        ads = [f"ad_fake_acme_charge_{n}" for n in range(20)]
        start = date(2026, 1, 1)
        for ad_id in ads:
            states = [sandbox_timeline.day_state(ad_id, start + timedelta(days=n)) for n in range(120)]
            for first in range(len(states) - 13):
                window = states[first:first + 14]
                self.assertEqual((window.count(sandbox_timeline.PAUSED), window.count(sandbox_timeline.ZERO_DELIVERY)),
                                 (1, 1))
            quiet_run = longest = 0
            for state in states:
                quiet_run = quiet_run + 1 if state != sandbox_timeline.DELIVERING else 0
                longest = max(longest, quiet_run)
            self.assertLess(longest, 7)
        phases = {tuple(sandbox_timeline.day_state(ad, start + timedelta(days=n)) for n in range(14)) for ad in ads}
        self.assertGreater(len(phases), 5, "each ad has its own phase")

    def test_values_are_deterministic_and_consistent(self):
        day = date(2026, 11, 9)
        for ad_id in ("ad_fake_acme_charge_1", "ad_fake_acme_charge_2"):
            first, second = sandbox_timeline.day_values(ad_id, day), sandbox_timeline.day_values(ad_id, day)
            self.assertEqual(first, second)
            if first is not None:
                self.assertTrue(first.link_clicks <= first.clicks <= first.impressions)
        status = {sandbox_timeline.configuration_for_ad("ad_fake_acme_charge_1", date(2026, 11, 1) + timedelta(days=n))
                  ["status"] for n in range(14)}
        self.assertEqual(status, {"ACTIVE", "PAUSED"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
