"""Beta default ScalePolicyVersion v2: what ``beta-v2`` evaluates (#132, #133).

* #132: each 7-day window's economic floor comes from the Policy's table, by
  the Episode's currency; a currency in the table is judged against its own
  floor (``>=``, so exactly the floor counts), and one outside it stays at
  monitorable with ``spend_floor_currency_unsupported`` on both economic
  signals, no breakdown, and delivery and CTR health still evaluated;
* #132 X-A (contract §9.2): below economics_ready, ``cpa_worsening`` and
  ``roas_worsening`` are unavailable with ``economics_not_ready``, so no
  ``economics_decline`` forms;
* #133 (contract §13): a placement or country segment forms
  ``review_delivery_mix`` only on its second consecutive hit, and a gap
  starts the count again;
* beta-v1 replay stays byte-identical: result hashes and v1-v3 snapshot
  hashes of fixed scenarios equal the values main ``683020c`` computed
  before this change, and a v3 snapshot sealed under v1 still reassembles
  and evaluates under ``beta-v1``.

Fictional brands only: Acme Charge.
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import app.scale.evidence as evidence_module
from app.scale._contracts import (
    BreakdownSegment,
    InvalidScaleSnapshot,
    PriorStreakState,
    ScaleTargetSnapshot,
)
from app.scale.evaluator import CURRENT_ALGORITHM_VERSION, evaluate_scale
from app.scale.evidence import (
    SnapshotAssemblyError,
    _policy_snapshot,
    _snapshot_hash,
    assemble_snapshot,
    seal_snapshot,
)
from app.scale.models import ScaleEvidenceSnapshotRecord, ScalePolicyVersionRecord
from app.scale.platform_policy import BETA_DEFAULT_POLICY_V1, BETA_DEFAULT_POLICY_V2
from app.scale.types import (
    BreakdownEvidenceState,
    BreakdownType,
    EvidenceMaturity,
    ScaleHealth,
    SignalState,
)
from tests import test_scale_evaluator as ev
from tests.test_scale_evidence import EvidenceTestBase

ECONOMIC = ("cpa_worsening", "roas_worsening")
NON_ECONOMIC = ("frequency_high", "frequency_growth", "ctr_decline", "cvr_decline", "cpm_increase",
                "delivery_decline", "configuration_drift", "no_delivery_complete_day")


def _v2_policy(currency: str):
    """v2 as the assembler freezes it for an Episode in ``currency``."""
    return _policy_snapshot(ScalePolicyVersionRecord(**BETA_DEFAULT_POLICY_V2.row()), currency)


def _target(currency: str, target_cpa: str = "1") -> ScaleTargetSnapshot:
    return ScaleTargetSnapshot(
        target_version_id="target-acme-charge",
        currency=currency,
        target_cpa=Decimal(target_cpa),
        breakeven_roas=None,
        minimum_economic_spend=Decimal("0"),
        frequency_level_threshold_override=None,
        frequency_growth_threshold_override=None,
        ctr_decline_threshold_override=None,
        cvr_decline_threshold_override=None,
        cpm_increase_threshold_override=None,
        delivery_decline_threshold_override=None,
        cpa_worsening_threshold_override=None,
        roas_worsening_threshold_override=None,
    )


def _week_with_spend(start, total_spend: str):
    """Seven days of the flat fixture whose spend adds up to ``total_spend``:
    six days of 13 and the rest on the last day."""
    last = Decimal(total_spend) - 6 * 13
    return ev._uniform_week(start, 2000, 200, 1, 13, 39,
                            last_day_override=lambda day: ev._day(day, 2000, 200, 1, last, last * 3))


def _signals(result):
    return {signal.signal_id: signal for signal in result.signals}


def _advice(result):
    return sorted(candidate.advice_type.value for candidate in result.advice_candidates)


def _economic_reasons(result):
    signals = _signals(result)
    return {key: (signals[key].state, signals[key].reason_code) for key in ECONOMIC}


class EconomicFloorPerCurrency(unittest.TestCase):
    """#132 with the published v2 constant."""

    def test_the_v2_snapshot_runs_the_deployed_algorithm(self):
        result = evaluate_scale(ev._flat_snapshot(policy=_v2_policy("USD")))
        self.assertEqual(result.algorithm_version, CURRENT_ALGORITHM_VERSION)
        self.assertEqual(CURRENT_ALGORITHM_VERSION, "beta-v2")

    def test_a_currency_in_the_table_is_judged_against_its_own_floor(self):
        # The flat fixture spends 140 per window: above EUR's 90 ...
        eur = evaluate_scale(ev._flat_snapshot(policy=_v2_policy("EUR"), currency="EUR"))
        self.assertEqual(eur.maturity, EvidenceMaturity.ECONOMICS_READY)
        self.assertEqual(_economic_reasons(eur), {key: (SignalState.MISS, "ok") for key in ECONOMIC})
        self.assertEqual(eur.health, ScaleHealth.HEALTHY)
        # ... far below JPY's 16,000, which v1's single 100 did not tell apart.
        jpy = evaluate_scale(ev._flat_snapshot(policy=_v2_policy("JPY"), currency="JPY"))
        self.assertEqual(jpy.maturity, EvidenceMaturity.MONITORABLE)
        self.assertEqual(_economic_reasons(jpy),
                         {key: (SignalState.UNAVAILABLE, "economics_not_ready") for key in ECONOMIC})

    def test_spend_exactly_at_the_floor_is_economics_ready(self):
        def evaluate(total_spend):
            return evaluate_scale(ev._snapshot(
                policy=_v2_policy("EUR"), currency="EUR",
                current_daily=_week_with_spend(ev.CURRENT_START, total_spend),
                baseline_daily=_week_with_spend(ev.BASELINE_START, total_spend),
            ))

        at_floor, below = evaluate("90"), evaluate("89.99")
        self.assertEqual(at_floor.maturity, EvidenceMaturity.ECONOMICS_READY)
        self.assertEqual(_economic_reasons(at_floor), {key: (SignalState.MISS, "ok") for key in ECONOMIC})
        self.assertEqual(below.maturity, EvidenceMaturity.MONITORABLE)
        self.assertEqual(_economic_reasons(below),
                         {key: (SignalState.UNAVAILABLE, "economics_not_ready") for key in ECONOMIC})

    def test_a_currency_outside_the_table_stays_monitorable_with_a_typed_reason(self):
        policy = _v2_policy("CHF")
        self.assertIsNone(policy.spend_floor)
        result = evaluate_scale(ev._flat_snapshot(policy=policy, currency="CHF"))
        self.assertEqual(result.maturity, EvidenceMaturity.MONITORABLE)
        self.assertEqual(_economic_reasons(result),
                         {key: (SignalState.UNAVAILABLE, "spend_floor_currency_unsupported") for key in ECONOMIC})
        signals = _signals(result)
        self.assertEqual({key: (signals[key].state, signals[key].reason_code) for key in NON_ECONOMIC},
                         {key: (SignalState.MISS, "ok") for key in NON_ECONOMIC}, "delivery and CTR evaluated")
        # #132 P-H A: healthy needs all eight core signals.
        self.assertEqual((result.health, result.health_reason), (None, "signal_unavailable"))
        self.assertEqual(result.advice_candidates, ())

    def test_outside_the_table_delivery_health_still_forms(self):
        # CPM up 40% with CTR and CVR stable. CPA rises too, but it is not
        # evaluated, so this is a delivery anomaly, not an economics decline.
        result = evaluate_scale(ev._flat_snapshot(
            policy=_v2_policy("CHF"), currency="CHF",
            current_daily=ev._uniform_week(ev.CURRENT_START, 2000, 200, 1, 28, 60),
        ))
        self.assertEqual(result.health, ScaleHealth.DELIVERY_ANOMALY)
        self.assertEqual(_advice(result), ["investigate_delivery_anomaly"])
        self.assertEqual(_signals(result)["cpm_increase"].state, SignalState.HIT)

    def test_outside_the_table_no_breakdown_runs(self):
        segment = BreakdownSegment(
            breakdown_type=BreakdownType.PLACEMENT, segment_key="feed",
            evidence_state=BreakdownEvidenceState.OBSERVED,
            daily=ev._uniform_week(ev.CURRENT_START, 200, 15, 1, 4, 6), suppression_reason=None,
        )

        def breakdown_signals(currency):
            result = evaluate_scale(ev._flat_snapshot(policy=_v2_policy(currency), currency=currency,
                                                      target=_target(currency), breakdowns=(segment,)))
            return [signal_id for signal_id in _signals(result) if signal_id.startswith("breakdown:")]

        self.assertEqual(breakdown_signals("USD"), ["breakdown:placement:feed:cpa"], "the segment passes the gates")
        self.assertEqual(breakdown_signals("CHF"), [])


class EconomicSignalsOnlyAtEconomicsReady(unittest.TestCase):
    """#132 X-A, contract §9.2: beta-v2 withholds economic decline below
    economics_ready; the beta-v1 replay keeps evaluating it."""

    def _below_floor(self, policy):
        # 49 vs 35 per window, under USD's 100: CPA up 40% with CTR stable.
        return ev._flat_snapshot(
            policy=policy,
            current_daily=ev._uniform_week(ev.CURRENT_START, 2000, 200, 1, 7, 21),
            baseline_daily=ev._uniform_week(ev.BASELINE_START, 2000, 200, 1, 5, 15),
        )

    def test_below_the_floor_no_economics_decline_forms(self):
        v1 = evaluate_scale(self._below_floor(ev._policy()))
        self.assertEqual((v1.maturity, v1.health), (EvidenceMaturity.MONITORABLE, ScaleHealth.ECONOMICS_DECLINE))
        self.assertIn("review_offer_or_landing_page", _advice(v1))

        v2 = evaluate_scale(self._below_floor(_v2_policy("USD")))
        self.assertEqual(v2.maturity, EvidenceMaturity.MONITORABLE)
        self.assertEqual(_economic_reasons(v2),
                         {key: (SignalState.UNAVAILABLE, "economics_not_ready") for key in ECONOMIC})
        self.assertNotEqual(v2.health, ScaleHealth.ECONOMICS_DECLINE)
        self.assertNotIn("review_offer_or_landing_page", _advice(v2))

    def test_warming_withholds_economic_signals(self):
        result = evaluate_scale(ev._flat_snapshot(policy=_v2_policy("USD"), complete_days=5, active_days=3))
        self.assertEqual(result.maturity, EvidenceMaturity.WARMING)
        self.assertEqual(_economic_reasons(result),
                         {key: (SignalState.UNAVAILABLE, "economics_not_ready") for key in ECONOMIC})
        self.assertEqual((result.health, result.health_reason), (None, "signal_unavailable"))


class ConsecutiveBreakdownHits(unittest.TestCase):
    """#133, contract §13, on the evaluator fixture's relaxed sample gates."""

    POLICY = ev._policy(algorithm_version="beta-v2", consecutive_breakdown_hits_required=2)
    KEY = "breakdown:placement:feed:cpa"

    def _evaluate(self, prior=None, *, worse_than_target=True):
        case = ev.TestBreakdownAnalysis("test_segment_worse_than_target_cpa_produces_review_delivery_mix")
        # The segment's CPA is 2: target 1 makes it a hit, target 5 a miss.
        target = case._target(Decimal("1") if worse_than_target else Decimal("5"))
        return evaluate_scale(ev._flat_snapshot(policy=self.POLICY, target=target, breakdowns=(case._segment(),),
                                                prior_streak=prior))

    def _prior(self, result) -> PriorStreakState:
        return PriorStreakState(
            evaluation_id="seval_previous_day",
            evaluated_local_day=ev.AS_OF - timedelta(days=1),
            compatibility=result.streak_state.compatibility,
            health=result.health,
            healthy_evaluations=result.streak_state.healthy_evaluations,
            counters=result.streak_state.counters,
        )

    def _streak(self, result) -> int:
        return {counter.signal_key: counter.consecutive_hits for counter in result.streak_state.counters}[self.KEY]

    def test_a_first_hit_forms_no_review_delivery_mix(self):
        first = self._evaluate()
        self.assertEqual(_signals(first)[self.KEY].state, SignalState.HIT)
        self.assertEqual(self._streak(first), 1)
        self.assertNotIn("review_delivery_mix", _advice(first))

    def test_the_second_consecutive_hit_forms_it(self):
        second = self._evaluate(self._prior(self._evaluate()))
        self.assertEqual(self._streak(second), 2)
        [candidate] = [c for c in second.advice_candidates if c.advice_type.value == "review_delivery_mix"]
        self.assertEqual(candidate.signal_ids, (self.KEY,))

    def test_a_gap_starts_the_count_again(self):
        hit = self._evaluate()
        gap = self._evaluate(self._prior(hit), worse_than_target=False)
        self.assertEqual(_signals(gap)[self.KEY].state, SignalState.MISS)
        self.assertEqual(self._streak(gap), 0)
        again = self._evaluate(self._prior(gap))
        self.assertEqual(self._streak(again), 1)
        self.assertNotIn("review_delivery_mix", _advice(again))
        self.assertIn("review_delivery_mix", _advice(self._evaluate(self._prior(again))))

    def test_beta_v2_needs_the_field_and_beta_v1_needs_a_floor(self):
        with self.assertRaises(InvalidScaleSnapshot) as missing_hits:
            evaluate_scale(ev._flat_snapshot(policy=ev._policy(algorithm_version="beta-v2")))
        self.assertEqual(missing_hits.exception.code, "invalid_policy")
        with self.assertRaises(InvalidScaleSnapshot) as missing_floor:
            evaluate_scale(ev._flat_snapshot(policy=ev._policy(spend_floor=None)))
        self.assertEqual(missing_floor.exception.code, "invalid_policy")


#: Computed by main 683020c, before #132 and #133, from the scenarios below:
#: (result hash, snapshot hash under schema v1, v2, v3).
_MAIN_683020C = {
    "healthy": (
        "12e0d3bae29416f1b2c78df509df1b901f39e7887d3e6130e37c82b5ec108dab",
        "c23df916ffd93036240c3a8ed0599a6aaf041879d36cb13881665aa3031fff3f",
        "0d547750e4b05751679d022a4068053b699d8c5c0e54edd3fe5b6f4c2fe950ef",
        "8b109fe7ef3d0da82e6f92e61bff6f1630c2aaa52ac7131092011509ef76f594",
    ),
    "warming": (
        "ea468c842594e73045ffece1183fc117b7aeff6f5e3186a5a0e0547ead8355bb",
        "d5fe4ccc537e36c61d3f83198f9dd5e9d1a4a279c0d91e686a39f63547a01487",
        "732c264bbff3162a8b233dd215fe3737b8b4dee0278b16ff61e95845047d7d92",
        "3a0946c224f51988cfaf0fce8a68213e10e8beb4435b7770ed7bd60e0e612aa8",
    ),
    "fatigue_first_hit": (
        "bcb24918c6746f54f690bef5f611e0b37a3d27177e34714577a64cbeabf053ff",
        "bf3e909c5ed9e96543c2d7f1b6f678fd53df38882ae3b753da47774431bdf969",
        "553521398b808f8be0b30c5d837581edc981a59a2c8115dd6f8cf2b7aa204f9c",
        "c12bb88a62ba8f435a5f920e4be64e7e2512a8072477184436cf2978ef3e80a2",
    ),
    "economics_decline_below_floor": (
        "30dc7cefa8913d6f5484e74b30f26f81f9929a9a3d5779e66c0150df4b81675a",
        "564acf990de6eaf2c1406d2462cb9f1097273ae23f4d95eb371c952dfae8238f",
        "570ca2a4888ed956888790de15baf89e0883bfe35225c8080128b522c5019f2d",
        "0d42172dc146d19dc4dba3e4d25c9499785336cc7ea170211b645d8b05f15293",
    ),
    "breakdown_first_hit": (
        "8e79d5834a8d5dd6cfde19981beea42fbd2102f8d7edd027468cbe586c39914f",
        "1945033a3194f4f7d6a5c137a80892d1983d9cbc482a1058f39cdebd44ade04f",
        "66d9fb7fcece1e04e51bddcc1104511a4f816bc82be3615da3d7b4ba909d8118",
        "7d996532a67f70972c83690af0055a4c41af5c8ba9a489e05b2be7f88069560f",
    ),
    "not_delivering": (
        "0498b4f66a27934ce44cd1e40faeba418a24f6da48f6abc6492e70b018cd69cb",
        "d336f157919c7c17a8974e32b15cb8a3b7a27e5588f56ad490e18c73f8ef257a",
        "b13e5123dc873208271d492fbd828a7c0293e957daa443b60519b05cd67ab678",
        "ad8c89bb3e14183bc5716c4f43268928d2524c2f04e1011b75deeefd64a086a5",
    ),
}
_PRE_V4_SCHEMAS = ("scale-snapshot-v1", "scale-snapshot-v2", "scale-snapshot-v3")


def _v1_scenarios():
    breakdown = ev.TestBreakdownAnalysis("test_segment_worse_than_target_cpa_produces_review_delivery_mix")
    fatigue = ev.TestFatigueWatchAndConfirmedFatigue("test_first_hit_is_fatigue_watch")
    return {
        "healthy": ev._flat_snapshot(),
        "warming": ev._flat_snapshot(complete_days=5, active_days=3),
        "fatigue_first_hit": fatigue._fatigue_snapshot(),
        "economics_decline_below_floor": ev._flat_snapshot(
            current_daily=ev._uniform_week(ev.CURRENT_START, 2000, 200, 1, 7, 21),
            baseline_daily=ev._uniform_week(ev.BASELINE_START, 2000, 200, 1, 5, 15),
        ),
        "breakdown_first_hit": ev._flat_snapshot(target=breakdown._target(Decimal("1")),
                                                 breakdowns=(breakdown._segment(),)),
        "not_delivering": ev._flat_snapshot(
            current_daily=ev._uniform_week(ev.CURRENT_START, 0, 0, 0, 0, 0, delivered=False),
            baseline_daily=ev._uniform_week(ev.BASELINE_START, 2000, 200, 1, 20, 0),
        ),
    }


class BetaV1ReplayIsByteIdentical(unittest.TestCase):
    """Contract §10.1 and §11: what froze v1 is never reinterpreted."""

    def test_beta_v1_results_equal_main_before_the_change(self):
        for name, snapshot in _v1_scenarios().items():
            with self.subTest(name):
                self.assertEqual(snapshot.policy.algorithm_version, "beta-v1")
                result = evaluate_scale(snapshot)
                self.assertEqual(result.algorithm_version, "beta-v1")
                self.assertEqual(result.result_hash, _MAIN_683020C[name][0])

    def test_v1_to_v3_snapshot_hashes_equal_main_before_the_change(self):
        for name, snapshot in _v1_scenarios().items():
            for schema, expected in zip(_PRE_V4_SCHEMAS, _MAIN_683020C[name][1:]):
                with self.subTest(name, schema=schema):
                    self.assertEqual(_snapshot_hash(replace(snapshot, canonical_schema_version=schema)), expected)

    def test_only_v4_hashes_the_consecutive_breakdown_hits(self):
        snapshot = ev._flat_snapshot()
        two, three = (replace(snapshot, policy=replace(snapshot.policy, consecutive_breakdown_hits_required=n))
                      for n in (2, 3))
        v4 = "scale-snapshot-v4"
        self.assertNotEqual(_snapshot_hash(replace(two, canonical_schema_version=v4)),
                            _snapshot_hash(replace(three, canonical_schema_version=v4)))
        self.assertEqual(_snapshot_hash(replace(two, canonical_schema_version="scale-snapshot-v3")),
                         _snapshot_hash(replace(three, canonical_schema_version="scale-snapshot-v3")))


class SealedSnapshotsAcrossTheSchemaChange(EvidenceTestBase):
    """The persistence assembler around v4 (#132, #133)."""

    def setUp(self) -> None:
        super().setUp()
        # Evaluable as sealed: the fixture's reference predates its sync.
        self.run.freshness_reference_at = self.manifest.terminal_at
        self.session.flush()

    def _add_v2_policy(self) -> ScalePolicyVersionRecord:
        policy = ScalePolicyVersionRecord(**BETA_DEFAULT_POLICY_V2.row())
        self.session.add(policy)
        self.run.policy_version_id = policy.id
        self.run.algorithm_version = BETA_DEFAULT_POLICY_V2.algorithm_version
        self.session.flush()
        return policy

    def test_a_v3_snapshot_under_v1_reassembles_and_replays_under_beta_v1(self):
        with mock.patch.object(evidence_module, "CANONICAL_SCHEMA_VERSION", "scale-snapshot-v3"):
            snapshot_id = seal_snapshot(self.session, self.context, self.run.id)
        stored = self.session.get(ScaleEvidenceSnapshotRecord, snapshot_id)
        snapshot = assemble_snapshot(self.session, self.context, snapshot_id)
        self.assertEqual((stored.canonical_schema_version, snapshot.canonical_hash),
                         ("scale-snapshot-v3", stored.canonical_hash))
        self.assertIsNone(snapshot.policy.consecutive_breakdown_hits_required)
        self.assertEqual(evaluate_scale(snapshot).algorithm_version, "beta-v1")

    def test_v4_freezes_the_floor_of_the_episode_currency(self):
        self._add_v2_policy()
        snapshot_id = seal_snapshot(self.session, self.context, self.run.id)
        snapshot = assemble_snapshot(self.session, self.context, snapshot_id)
        self.assertEqual(snapshot.canonical_schema_version, "scale-snapshot-v4")
        self.assertEqual((snapshot.currency, snapshot.policy.spend_floor,
                          snapshot.policy.consecutive_breakdown_hits_required), ("USD", Decimal("100"), 2))
        self.assertEqual(evaluate_scale(snapshot).algorithm_version, "beta-v2")

    def test_v4_freezes_no_floor_for_a_currency_outside_the_table(self):
        self._add_v2_policy()
        self.episode.currency = "CHF"
        self.session.flush()
        snapshot = assemble_snapshot(self.session, self.context,
                                     seal_snapshot(self.session, self.context, self.run.id))
        self.assertEqual((snapshot.currency, snapshot.policy.spend_floor), ("CHF", None))
        self.assertEqual(_economic_reasons(evaluate_scale(snapshot)),
                         {key: (SignalState.UNAVAILABLE, "spend_floor_currency_unsupported") for key in ECONOMIC})

    def test_a_pre_v4_snapshot_naming_v2_does_not_reassemble(self):
        # Only code from before v4 could seal this, and it could not
        # evaluate it: beta-v2 was not registered there.
        self._add_v2_policy()
        with mock.patch.object(evidence_module, "CANONICAL_SCHEMA_VERSION", "scale-snapshot-v3"):
            snapshot_id = seal_snapshot(self.session, self.context, self.run.id)
        with self.assertRaises(SnapshotAssemblyError) as refused:
            assemble_snapshot(self.session, self.context, snapshot_id)
        self.assertEqual(refused.exception.code, "provenance_mismatch")

    def test_v1_stays_readable_beside_v2(self):
        self._add_v2_policy()
        v1 = _policy_snapshot(ScalePolicyVersionRecord(**BETA_DEFAULT_POLICY_V1.row()), "CHF")
        self.assertEqual((v1.algorithm_version, v1.spend_floor), ("beta-v1", Decimal("100")))


if __name__ == "__main__":
    unittest.main()
