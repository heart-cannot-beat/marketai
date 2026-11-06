"""The web board fixtures are the server projection's own output, so board UI tests cannot drift from the contract.

After a deliberate projection change, regenerate them and format with prettier:
    .venv/bin/python -m tests.test_marketing_board_web_fixtures --write
    (cd web && npx prettier --write __tests__/fixtures/marketing-*.json)
"""
import json
import sys
import unittest
from pathlib import Path

from app.marketing.board import project
from app.marketing.workflow_contracts import MarketingBoard

FIXTURES = Path(__file__).resolve().parents[1] / "web" / "__tests__" / "fixtures"
ROUNDS = (("marketing-workflow.json", ("bound", "preview", "evaluated")), ("marketing-ai-qc.json", ("row",)))
SCENARIOS = "marketing-board.json"


def facts(row):
    """The execution facts `workflow.round_dict` passes for a round's assets."""
    sha = {e["asset_version_id"]: e["content_sha256"] for e in (row["handoff"] or {}).get("entries", [])}
    return [{"asset_version_id": a["asset_version_id"], "combination_id": a["combination_id"],
        "parent_version_id": a.get("parent_version_id"), "content_sha256": sha.get(a["asset_version_id"], "sha-" + a["asset_version_id"]),
        "created_at": a.get("created_at") or f"2026-10-24T00:00:{n:02d}", "reused": a.get("edit_operation") == "reuse",
        "quality": (a["quality"] or {}).get("quality"), "ready": a["ready"], "references_current": True, "blocking": False,
        **{key: {**a[key], "quality_suggestion": (a[key].get("result") or {}).get("quality_suggestion")} if a.get(key) else None
           for key in ("ai_qc", "paired_ai_qc")}}
        for n, a in enumerate(row["assets"])]


def round_board(row):
    handoff, bindings = row["handoff"], {}
    for b in row["bindings"]:
        bindings.setdefault(b["entry_id"], []).append(b["ad_id"])
    # Fixture batches carry counts, not rows: a confirmed batch is taken to report every bound ad.
    data = {ad for ads in bindings.values() for ad in ads} if any(b["state"] == "confirmed" for b in row["batches"]) else set()
    return project(row["snapshot"]["plan"], facts(row), round_state=row["state"],
        handoff={"state": handoff["state"], "entries": {e["asset_version_id"]: e["entry_id"] for e in handoff["entries"]}} if handoff else None,
        bindings=bindings, data_ad_ids=data, evaluation_current=bool(row["evaluation"] and row["evaluation"]["current"]))


def expression(eid, name, mechanism):
    return {"id": eid, "name": name, "description": name, "fit_reason": name, "required_evidence": [],
        "how_to_tell": name, "visual_mechanism": mechanism, "evidence_basis": {"fact_ids": ["f1"], "gap": ""}}


def plan(stage, question, angles, combinations, groups, executions=None):
    return {"stage": stage, "question": question, "comparison_kind": "whole_combinations", "changed": ["Proposition and expression"],
        **({"executions_per_combination": executions} if executions else {}),
        "angles": [{"id": a, "proposition": p, "expressions": xs} for a, p, xs in angles],
        "combinations": [{"id": c, "angle_id": a, "expression_id": x, "headline": h} for c, a, x, h in combinations],
        "groups": [{"id": g, "name": n, "combination_ids": cs} for g, n, cs in groups]}


def execution(vid, cid, at, *, parent=None, quality=None, ai=None, paired=None, sha=None, photos=()):
    return {"asset_version_id": vid, "combination_id": cid, "parent_version_id": parent, "content_sha256": sha or f"sha-{vid}",
        "created_at": f"2026-10-24T00:00:{at:02d}", "reused": False, "quality": quality, "ready": False,
        "references_current": True, "blocking": False, "ai_qc": ai, "paired_ai_qc": paired,
        **({"product_appearance": {"status": "reference_backed", "reason": None, "reference_version_ids": list(photos)}} if photos else {})}


ROUTE, LIVE, STORY = (expression("x_route", "Route choice diagram", "route_map"),
    expression("x_live", "Live connection demo", "live_connection_demo"), expression("x_story", "Finding a charger story", "scenario_story"))
CERT, TEST = (expression("x_cert", "Certification evidence explained", "certification_walkthrough"),
    expression("x_test", "Test process explained", "test_process"))
OPTION, SAFE = "One more charging option", "Safer at high power"
def check(qid, state="succeeded", suggestion="ready_to_confirm"):
    return {"id": qid, "state": state, "current": True, "quality_suggestion": suggestion if state == "succeeded" else None}


AI_DONE = check("aiqc_route_1")


def scenarios():
    """Two stages of the CCS → NACS adapter example from the J-12 interface sketches."""
    # Screening angles: A and B compare as whole groups; Live connection demo is shared, the rest are unique.
    screen = project(plan("screen_angles", "Which buying proposition is worth exploring further?",
            [("a_option", OPTION, [ROUTE, LIVE, STORY]), ("a_safe", SAFE, [CERT, LIVE])],
            [("c_route", "a_option", "x_route", "Pick the route, not the charger"),
             ("c_live_a", "a_option", "x_live", "Plug in and charge"),
             ("c_story", "a_option", "x_story", "One more stop on the map"),
             ("c_cert", "a_safe", "x_cert", "Certified for high power"),
             ("c_live_b", "a_safe", "x_live", "Plug in and charge safely")],
            [("g_a", "Group A", ["c_route", "c_live_a", "c_story"]), ("g_b", "Group B", ["c_cert", "c_live_b"])]),
        [execution("v_route_1", "c_route", 1, ai=AI_DONE, photos=["ref_main"]),
         execution("v_route_2", "c_route", 2, ai=check("aiqc_route_2", "running")),
         execution("v_route_1_copy", "c_route", 7, sha="sha-v_route_1"),
         execution("v_live_a_1", "c_live_a", 3, quality="needs_changes"),
         execution("v_cert_1", "c_cert", 4, ai=check("aiqc_cert_1")),
         execution("v_live_b_1", "c_live_b", 5),
         execution("v_live_b_1r", "c_live_b", 6, parent="v_live_b_1", ai=check("aiqc_live_b_1r"),
                   paired=check("aiqc_live_b_1r_pair", suggestion="needs_changes"))],
        round_state="reviewing", generation_open=True)
    # Matching expressions: the angle with the most arranged expressions is compared within itself,
    # even though it is not the first angle of the plan.
    match = project(plan("match_expressions", "How should “One more charging option” be told?",
            [("a_safe", SAFE, [CERT, TEST]), ("a_option", OPTION, [ROUTE, LIVE, STORY])],
            [("m_route", "a_option", "x_route", "Pick the route, not the charger"),
             ("m_live", "a_option", "x_live", "Plug in and charge"),
             ("m_story", "a_option", "x_story", "One more stop on the map"),
             ("m_cert", "a_safe", "x_cert", "Certified for high power"),
             ("m_test", "a_safe", "x_test", "Tested before it ships")],
            [("g_a", "Group A", ["m_route", "m_live", "m_story"]), ("g_b", "Group B", ["m_cert", "m_test"])], executions=2),
        [execution("w_route_1", "m_route", 1), execution("w_route_2", "m_route", 2), execution("w_live_1", "m_live", 3),
         execution("w_story_1", "m_story", 4), execution("w_cert_1", "m_cert", 5), execution("w_test_1", "m_test", 6)],
        round_state="reviewing", generating={"m_story"})
    return {"product_name": "CCS to NACS adapter", "screen": screen, "match": match}


def rounds():
    for name, keys in ROUNDS:
        data = json.loads((FIXTURES / name).read_text())
        yield name, data, keys


class TestWebBoardFixtures(unittest.TestCase):
    def test_round_fixtures_carry_the_server_board(self):
        for name, data, keys in rounds():
            for key in keys:
                with self.subTest(fixture=name, round=key):
                    MarketingBoard.model_validate(data[key]["board"])
                    self.assertEqual(data[key]["board"], round_board(data[key]))

    def test_scenario_fixture_is_the_server_board(self):
        stored = json.loads((FIXTURES / SCENARIOS).read_text())
        for key in ("screen", "match"):
            MarketingBoard.model_validate(stored[key])
        self.assertEqual(stored, scenarios())

    def test_scenarios_cover_the_board_states_the_ui_distinguishes(self):
        result = scenarios()
        nodes = [x for a in result["screen"]["tree"]["angles"] for e in a["expressions"] for c in e["combinations"] for x in c["executions"]]
        self.assertTrue({"single_qc_pending", "single_qc_running", "single_qc_done", "paired_qc_pending", "paired_qc_done", "blocked",
                         "pending_generation"} <= {n["stage"] for n in nodes})
        self.assertIn(None, [cell for row in result["screen"]["matrix"]["cells"] for cell in row])
        self.assertTrue(any(r["shared"] for r in result["screen"]["matrix"]["rows"]))
        self.assertEqual({n["asset_version_id"]: n["duplicate_of"] for n in nodes}["v_route_1_copy"], "v_route_1")
        self.assertEqual({n["asset_version_id"]: n["execution_version"] for n in nodes}["v_live_b_1r"], 2)
        self.assertEqual(result["screen"]["product_appearance"]["status"], "partial")
        self.assertEqual(result["match"]["primary_comparison"]["angle_id"], "a_option")
        self.assertNotEqual(result["match"]["tree"]["angles"][0]["angle_id"], "a_option")


def write():
    for name, data, keys in rounds():
        for key in keys:
            data[key]["board"] = round_board(data[key])
        (FIXTURES / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    (FIXTURES / SCENARIOS).write_text(json.dumps(scenarios(), ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    if "--write" in sys.argv:
        write()
    else:
        unittest.main()
