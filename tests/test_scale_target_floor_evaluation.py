"""A Target published below the platform floor evaluates as before (#134).

#134 refuses, at preview and publish, a Target whose minimum economic spend
misses the platform floor (``tests/test_scale_targets.py``). Targets
published before that check stay in force and are not rewritten: the
evaluator already holds every Target to the largest of the Policy floor,
the Target's minimum and 3x its CPA (contract §9.2), so the platform floor
never was lowered at evaluation.

These tests freeze two such Targets the way a live run does
(``app.scale.evidence._target_snapshot``) and evaluate them under the
published v2 Policy:

* the platform floor, not the Target's lower minimum, decides
  economics_ready -- spend exactly at the floor is ready, a cent below is
  not;
* the results hash to the values main ``59d8643`` computed before #134,
  so the change leaves their evaluation byte-identical.

Fictional brands only: Acme Charge.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from app.scale.evaluator import evaluate_scale
from app.scale.evidence import _policy_snapshot, _target_snapshot
from app.scale.models import ScalePolicyVersionRecord, ScaleTargetVersionRecord
from app.scale.platform_policy import BETA_DEFAULT_POLICY_V2
from app.scale.types import EvidenceMaturity
from tests import test_scale_evaluator as ev
from tests.test_scale_policy_v2 import _week_with_spend


def _published_before_the_check(
    target_id: str, *, breakeven_roas: str, minimum_economic_spend: str | None,
) -> ScaleTargetVersionRecord:
    """An active USD Target with only a ROAS, as #134 now refuses it."""
    return ScaleTargetVersionRecord(
        id=target_id,
        workspace_id="ws-acme-charge",
        product_version_id="pv-acme-charge",
        offer_version_id="ov-acme-charge",
        market_code="US",
        currency="USD",
        target_cpa=None,
        breakeven_roas=Decimal(breakeven_roas),
        minimum_economic_spend=(
            Decimal(minimum_economic_spend) if minimum_economic_spend is not None else None
        ),
        lifecycle_state="active",
        creator_id="user-owner",
        revision=1,
    )


#: Only a ROAS, and a minimum economic spend of 10 against USD's floor of 100.
BELOW_FLOOR = _published_before_the_check(
    "stgt_acme_below_floor", breakeven_roas="2.0", minimum_economic_spend="10",
)
#: Only a ROAS and no minimum at all.
NO_MINIMUM = _published_before_the_check(
    "stgt_acme_no_minimum", breakeven_roas="2.0", minimum_economic_spend=None,
)


def _evaluate(target: ScaleTargetVersionRecord, window_spend: str):
    """Both 7-day windows spend ``window_spend`` in USD under v2."""
    policy = _policy_snapshot(ScalePolicyVersionRecord(**BETA_DEFAULT_POLICY_V2.row()), "USD")
    return evaluate_scale(ev._snapshot(
        policy=policy,
        target=_target_snapshot(target),
        currency="USD",
        current_daily=_week_with_spend(ev.CURRENT_START, window_spend),
        baseline_daily=_week_with_spend(ev.BASELINE_START, window_spend),
    ))


#: ``result_hash`` of each (Target, window spend) on main 59d8643, before
#: #134 added the publication check.
RESULT_HASHES_BEFORE_134 = {
    ("stgt_acme_below_floor", "99.99"):
        "b3b3b678c278a5e1f43937cfa151e4bfdd73715c33c3eb7b0bff75eb110f894b",
    ("stgt_acme_below_floor", "100"):
        "2911cc0743d2565fc271bf8672e18b27083c69c5a249d4ceac714baf91716517",
    ("stgt_acme_no_minimum", "99.99"):
        "474dff33e926a712569c2f51c7aab464c6ad1839cd1d602bd9c2650d66279ba8",
    ("stgt_acme_no_minimum", "100"):
        "17b89cd88d9bc1fcc2293aee88224d4b0c9eb8fac925314dac2f43fcb2f94b39",
}


class PublishedTargetBelowTheFloor(unittest.TestCase):
    def test_the_platform_floor_decides_economics_ready(self):
        for target in (BELOW_FLOOR, NO_MINIMUM):
            with self.subTest(target=target.id):
                self.assertEqual(_evaluate(target, "100").maturity, EvidenceMaturity.ECONOMICS_READY)
                self.assertEqual(_evaluate(target, "99.99").maturity, EvidenceMaturity.MONITORABLE)

    def test_it_evaluates_exactly_as_before_the_publication_check(self):
        for (target_id, window_spend), expected in RESULT_HASHES_BEFORE_134.items():
            target = {t.id: t for t in (BELOW_FLOOR, NO_MINIMUM)}[target_id]
            with self.subTest(target=target_id, window_spend=window_spend):
                self.assertEqual(_evaluate(target, window_spend).result_hash, expected)


if __name__ == "__main__":
    unittest.main()
