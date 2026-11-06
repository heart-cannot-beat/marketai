"""Board projection: comparison scope, execution progress and matrix identity come from the server."""
import json
import random
import unittest

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetVersionRecord
from app.marketing.workflow import board_thumbnail_refs
from app.marketing.board import comparison_scope, legacy_qc_projection, project
from app.marketing.workflow_contracts import MarketingBoard
from tests.test_marketing_workflow import MarketingWorkflowFixtures


def expression(eid, name=None, mechanism="scenario_story"):
    details = {"how_to_tell": "Tell it", "visual_mechanism": mechanism,
               "evidence_basis": {"fact_ids": ["f1"], "gap": ""}} if mechanism else {}
    return {"id": eid, "name": name or eid, "description": "d", "fit_reason": "f", "required_evidence": [], **details}


def plan(angles, combos, groups, stage="screen_angles", kind="whole_combinations", changed=("Proposition and expression",)):
    return {"stage": stage, "question": "Which combination is worth exploring?", "comparison_kind": kind, "changed": list(changed),
        "angles": [{"id": a, "proposition": f"P {a}", "expressions": xs} for a, xs in angles],
        "combinations": [{"id": c, "angle_id": a, "expression_id": e, "headline": f"H {c}"} for c, a, e in combos],
        "groups": [{"id": g, "name": f"G {g}", "combination_ids": cs} for g, cs in groups]}


def execution(vid, cid, at, *, sha=None, parent=None, reused=False, quality=None, ready=False, refs=True, blocking=False, ai=None, paired=None):
    return {"asset_version_id": vid, "combination_id": cid, "parent_version_id": parent, "content_sha256": sha or f"sha-{vid}",
        "created_at": f"2026-10-24T00:00:{at:02d}", "reused": reused, "quality": quality, "ready": ready,
        "references_current": refs, "blocking": blocking, "ai_qc": ai, "paired_ai_qc": paired}


def check(state, current=True, suggestion=None, qid="q"):
    return {"id": qid, "state": state, "current": current, "quality_suggestion": suggestion}


def board(*args, **kwargs):
    result = project(*args, **{"round_state": "reviewing", **kwargs})
    MarketingBoard.model_validate(result)
    return result


def executions_of(result):
    return [x for a in result["tree"]["angles"] for e in a["expressions"] for c in e["combinations"] for x in c["executions"]]


def by_version(result):
    return {x["asset_version_id"]: x for x in executions_of(result)}


def unsigned(result):
    """The board with each thumbnail link cut to its object path: expiry and signature change per request."""
    result = json.loads(json.dumps(result))
    for node in executions_of(result):
        if node["thumbnail"]:
            node["thumbnail"] = node["thumbnail"]["url"].split("?")[0]
    return result


SCREEN = plan([("a1", [expression("x1", mechanism="live_connection_demo"), expression("x2", mechanism="feature_closeup"),
                       expression("x3", mechanism="qa_objection")]),
               ("a2", [expression("y1", mechanism="route_visualization"), expression("y2", mechanism="scenario_story")])],
              [("c1", "a1", "x1"), ("c2", "a1", "x2"), ("c3", "a1", "x3"), ("c4", "a2", "y1"), ("c5", "a2", "y2")],
              [("g1", ["c1", "c2", "c3"]), ("g2", ["c4", "c5"])])


class TestBoardProjection(unittest.TestCase):
    def test_scenario_2_unique_expressions_stay_visible_and_untested_cells_are_null(self):
        assets = [execution(f"v{n}", f"c{n}", n) for n in range(1, 6)]
        result = board(SCREEN, assets)
        self.assertEqual(result["primary_comparison"], {"kind": "between_groups", "groups": [
            {"id": "g1", "name": "G g1", "angle_ids": ["a1"], "combination_ids": ["c1", "c2", "c3"]},
            {"id": "g2", "name": "G g2", "angle_ids": ["a2"], "combination_ids": ["c4", "c5"]}]})
        self.assertEqual(result["secondary_observation"], {"kind": "expression_coverage", "angle_id": None})
        tree = result["tree"]["angles"]
        self.assertEqual([[e["expression_id"] for e in a["expressions"]] for a in tree], [["x1", "x2", "x3"], ["y1", "y2"]])
        self.assertEqual(len(executions_of(result)), 5)
        matrix = result["matrix"]
        self.assertEqual([r["expression_id"] for r in matrix["rows"]], ["x1", "x2", "x3", "y1", "y2"])
        self.assertEqual([c["angle_id"] for c in matrix["cols"]], ["a1", "a2"])
        self.assertEqual(matrix["cells"], [[["c1"], None], [["c2"], None], [["c3"], None], [None, ["c4"]], [None, ["c5"]]])
        self.assertFalse(any(r["shared"] for r in matrix["rows"]))
        nodes = by_version(result)
        self.assertEqual(nodes["v1"]["compare_with"], ["v4", "v5"])
        self.assertEqual(nodes["v5"]["compare_with"], ["v1", "v2", "v3"])
        for node in nodes.values():
            self.assertEqual((node["stage"], node["gap"], node["next_action"]), ("single_qc_pending", "single_qc", "single_qc"))
            self.assertEqual(node["execution_index"], 1)

    def test_missing_counterpart_waits_and_placeholder_has_no_asset_version(self):
        result = board(SCREEN, [execution("v1", "c1", 1), execution("v4", "c4", 2)], generating={"c5"}, generation_open=True)
        nodes = by_version(result)
        self.assertEqual(nodes["v1"]["awaiting_combination_ids"], ["c5"])
        # Single-image QC never waits for the rest of the round; only the paired check does.
        self.assertEqual((nodes["v1"]["stage"], nodes["v1"]["gap"], nodes["v1"]["next_action"]), ("single_qc_pending", "single_qc", "single_qc"))
        nodes = by_version(board(SCREEN, [execution("v1", "c1", 1, ai=check("succeeded")), execution("v4", "c4", 2)], generating={"c5"}))
        self.assertEqual((nodes["v1"]["stage"], nodes["v1"]["gap"], nodes["v1"]["next_action"]), ("paired_qc_pending", "paired_qc", "none"))
        self.assertEqual(nodes["v4"]["awaiting_combination_ids"], ["c2", "c3"])
        placeholders = [x for x in executions_of(result) if x["asset_version_id"] is None]
        self.assertEqual({x["combination_id"]: (x["stage"], x["next_action"]) for x in placeholders},
            {"c2": ("pending_generation", "generate"), "c3": ("pending_generation", "generate"), "c5": ("generating", "none")})
        self.assertTrue(all(x["execution_index"] == 1 and x["gap"] == "pending_generation" for x in placeholders))

    def test_shared_expression_needs_explicit_identity_not_the_same_name(self):
        shared = expression("x", name="Demo", mechanism="live_connection_demo")
        lookalike = expression("z", name="Demo", mechanism="live_connection_demo")
        result = board(plan([("a1", [shared]), ("a2", [shared, lookalike])],
            [("c1", "a1", "x"), ("c2", "a2", "x"), ("c3", "a2", "z")], [("g1", ["c1"]), ("g2", ["c2", "c3"])]), [])
        rows = result["matrix"]["rows"]
        self.assertEqual([(r["expression_id"], r["angle_ids"], r["shared"]) for r in rows], [("x", ["a1", "a2"], True), ("z", ["a2"], False)])
        self.assertEqual(result["matrix"]["cells"], [[["c1"], ["c2"]], [None, ["c3"]]])
        self.assertEqual([e["shared"] for a in result["tree"]["angles"] for e in a["expressions"]], [True, True, False])

    def test_conflicting_shared_definition_is_not_aligned(self):
        result = board(plan([("a1", [expression("x", mechanism="live_connection_demo")]), ("a2", [expression("x", mechanism="scenario_story")])],
            [("c1", "a1", "x"), ("c2", "a2", "x")], [("g1", ["c1"]), ("g2", ["c2"])]), [])
        self.assertEqual([(r["expression_id"], r["angle_ids"]) for r in result["matrix"]["rows"]], [("x", ["a1"]), ("x", ["a2"])])
        self.assertEqual(result["matrix"]["cells"], [[["c1"], None], [None, ["c2"]]])

    def test_legacy_expression_without_three_elements_keeps_identity_and_is_flagged(self):
        legacy = expression("x", name="Usage demonstration", mechanism=None)
        result = board(plan([("a1", [legacy, expression("w")]), ("a2", [legacy])], [("c1", "a1", "x"), ("c2", "a1", "w"), ("c3", "a2", "x")],
            [("g1", ["c1", "c2"]), ("g2", ["c3"])]), [])
        row = result["matrix"]["rows"][0]
        self.assertEqual((row["expression_id"], row["visual_mechanism"], row["details_missing"], row["angle_ids"]), ("x", None, True, ["a1", "a2"]))
        self.assertFalse(result["matrix"]["rows"][1]["details_missing"])
        self.assertTrue(result["tree"]["angles"][0]["expressions"][0]["details_missing"])

    def test_scenario_7_single_success_needs_an_independent_new_execution(self):
        retest = plan([("a1", [expression("x1")])], [("c1", "a1", "x1")], [("g1", ["c1"])],
            stage="confirm_combination", kind="business_retest")
        original = execution("orig", "c1", 1, sha="ad-file", reused=True)
        result = board(retest, [original], generation_open=True)
        self.assertEqual(result["primary_comparison"], {"kind": "retest", "original_asset_version_ids": ["orig"], "new_execution_ids": []})
        node, pending = result["tree"]["angles"][0]["expressions"][0]["combinations"][0]["executions"]
        self.assertEqual((node["asset_version_id"], node["execution_index"], node["awaiting_combination_ids"], node["stage"]),
            ("orig", 1, ["c1"], "single_qc_pending"))
        self.assertEqual((pending["asset_version_id"], pending["execution_index"], pending["gap"], pending["next_action"]),
            (None, 2, "pending_generation", "generate"))

        copy = execution("copy", "c1", 2, sha="ad-file")
        result = board(retest, [original, copy], generation_open=True)
        nodes = by_version(result)
        self.assertEqual((nodes["copy"]["duplicate_of"], nodes["copy"]["execution_index"], nodes["copy"]["compare_with"]), ("orig", 1, []))
        self.assertEqual(result["primary_comparison"]["new_execution_ids"], [])
        self.assertIn(None, nodes, "a duplicate file does not satisfy the new-execution requirement")

        fresh, revised = execution("new", "c1", 3), execution("new-r1", "c1", 4, parent="new")
        result = board(retest, [revised, copy, fresh, original], generation_open=True)
        nodes = by_version(result)
        self.assertNotIn(None, nodes)
        self.assertEqual(result["primary_comparison"], {"kind": "retest", "original_asset_version_ids": ["orig"],
            "new_execution_ids": ["new", "new-r1"]})
        self.assertEqual(nodes["orig"]["compare_with"], ["new", "new-r1"])
        self.assertEqual(nodes["new"]["compare_with"], ["orig"])
        self.assertEqual((nodes["new"]["execution_index"], nodes["new-r1"]["execution_index"], nodes["new-r1"]["parent_version_id"]), (2, 2, "new"))
        self.assertEqual(nodes["orig"]["awaiting_combination_ids"], [])

    def test_specific_factor_pairs_follow_explicit_angle_and_expression_identity(self):
        factor = plan([("a1", [expression("x1"), expression("x2", mechanism="feature_closeup"), expression("x3", mechanism="qa_objection")])],
            [("c1", "a1", "x1"), ("c2", "a1", "x2"), ("c3", "a1", "x2"), ("c4", "a1", "x1"), ("c5", "a1", "x3")],
            [("g1", ["c1", "c2"]), ("g2", ["c3", "c4", "c5"])], stage="optimize_elements", kind="specific_factor",
            changed=["Certification prominence"])
        assets = [execution(f"v{n}", f"c{n}", n) for n in range(1, 6)]
        result = board(factor, assets)
        self.assertEqual(result["primary_comparison"], {"kind": "factor", "changed": ["Certification prominence"],
            "pairs": [{"combination_ids": ["c1", "c4"], "group_ids": ["g1", "g2"]}, {"combination_ids": ["c2", "c3"], "group_ids": ["g1", "g2"]}],
            "unpaired_combination_ids": ["c5"]})
        nodes = by_version(result)
        self.assertEqual(nodes["v1"]["compare_with"], ["v4"])
        self.assertEqual(nodes["v3"]["compare_with"], ["v2"])
        self.assertEqual(nodes["v5"]["compare_with"], ["v1", "v2"])
        self.assertEqual(result["matrix"]["cells"], [[["c1", "c4"]], [["c2", "c3"]], [["c5"]]])
        self.assertIsNone(result["secondary_observation"])

    def test_match_expressions_current_angle_comes_from_the_plan(self):
        match = plan([("a1", [expression("x1")]), ("a2", [expression("y1"), expression("y2", mechanism="qa_objection"), expression("y3", mechanism="fit_guide")])],
            [("c1", "a1", "x1"), ("c2", "a2", "y1"), ("c3", "a2", "y3")], [("g1", ["c1", "c2"]), ("g2", ["c3"])], stage="match_expressions")
        result = board(match, [execution("v1", "c1", 1), execution("v2", "c2", 2), execution("v3", "c3", 3)])
        self.assertEqual(result["primary_comparison"], {"kind": "within_angle", "angle_id": "a2", "expression_ids": ["y1", "y3"]})
        self.assertEqual(result["secondary_observation"], {"kind": "same_angle", "angle_id": "a2"})
        nodes = by_version(result)
        self.assertEqual((nodes["v2"]["compare_with"], nodes["v3"]["compare_with"], nodes["v1"]["compare_with"]), (["v3"], ["v2"], []))
        self.assertEqual(result["matrix"]["cells"][2], [None, None], "y2 is suggested but unarranged: untested, not failed")

    def test_same_snapshot_projects_identically(self):
        assets = [execution(f"v{n}", f"c{n}", n, ai={"id": f"q{n}", "state": "succeeded", "current": True, "quality_suggestion": "ready_to_confirm"})
                  for n in range(1, 6)] + [execution("v1b", "c1", 9, parent="v1")]
        handoff = {"state": "ready", "entries": {"v1": "e1", "v2": "e2"}}
        kwargs = {"handoff": handoff, "bindings": {"e1": ["200", "100"]}, "data_ad_ids": {"100"}, "round_state": "awaiting_data"}
        first = project(SCREEN, assets, **kwargs)
        shuffled = assets[:]
        random.Random(7).shuffle(shuffled)
        second = project(json.loads(json.dumps(SCREEN)), shuffled, **kwargs)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first, project(SCREEN, assets, **kwargs))

    def test_execution_stage_gap_and_next_action_follow_persisted_state(self):
        ai = check
        assets = [execution("v1", "c1", 1, ai=ai("queued")), execution("v2", "c2", 2, ai=ai("succeeded")),
                  execution("v3", "c3", 3, quality="needs_changes"), execution("v4", "c4", 4, refs=False),
                  execution("v5", "c5", 5, quality="approved", ready=True), execution("v6", "c5", 6, ai=ai("failed")),
                  execution("v7", "c4", 7, blocking=True), execution("v8", "c4", 8, ai=ai("succeeded", current=False))]
        nodes = by_version(board(SCREEN, assets))
        expected = {"v1": ("single_qc_running", "single_qc", "none", None), "v2": ("single_qc_done", "paired_qc", "paired_qc", None),
            "v3": ("blocked", "review", "revise", "quality_changes_requested"), "v4": ("blocked", "single_qc", "revise", "reference_not_current"),
            "v5": ("ready", "export", "none", None), "v6": ("single_qc_pending", "single_qc", "single_qc", None),
            "v7": ("blocked", "single_qc", "revise", "file_qc_failed"), "v8": ("single_qc_pending", "single_qc", "single_qc", None)}
        self.assertEqual({k: (v["stage"], v["gap"], v["next_action"], v["blocked_reason"]) for k, v in nodes.items()}, expected)
        self.assertNotIn("v3", nodes["v1"]["compare_with"], "a rejected execution is not a comparison object")
        self.assertEqual(nodes["v1"]["compare_with"], ["v5", "v6", "v8"])
        self.assertEqual(nodes["v6"]["awaiting_combination_ids"], ["c3"], "group 2 waits for a usable c3 counterpart")

        ready = [execution(f"v{n}", f"c{n}", n, quality="approved", ready=True) for n in range(1, 6)]
        self.assertEqual({v["next_action"] for v in by_version(board(SCREEN, ready)).values()}, {"export"})
        entries = {f"v{n}": f"e{n}" for n in range(1, 5)}
        queued = by_version(board(SCREEN, ready, handoff={"state": "queued", "entries": entries}, round_state="handoff"))
        self.assertEqual((queued["v1"]["stage"], queued["v1"]["next_action"]), ("ready", "none"))
        self.assertEqual((queued["v5"]["gap"], queued["v5"]["next_action"], queued["v5"]["delivery"]), ("none", "none", None))
        failed = by_version(board(SCREEN, ready, handoff={"state": "failed", "entries": entries}, round_state="handoff"))
        self.assertEqual(failed["v1"]["next_action"], "export")
        delivered = dict(handoff={"state": "ready", "entries": entries}, bindings={"e2": ["900"], "e3": ["901"], "e4": ["902"]},
                         data_ad_ids={"901", "902"}, round_state="awaiting_data")
        nodes = by_version(board(SCREEN, ready, **delivered))
        self.assertEqual({k: (v["stage"], v["gap"], v["next_action"]) for k, v in nodes.items() if k != "v5"},
            {"v1": ("exported", "bind", "bind"), "v2": ("bound", "data", "import"), "v3": ("has_data", "none", "evaluate"),
             "v4": ("has_data", "none", "evaluate")})
        self.assertEqual(nodes["v3"]["delivery"], {"entry_id": "e3", "ad_ids": ["901"], "has_data": True})
        nodes = by_version(board(SCREEN, ready, **{**delivered, "round_state": "evaluated"}, evaluation_current=True))
        self.assertEqual(nodes["v3"]["next_action"], "none")
        withdrawn = [execution("v1", "c1", 1, quality="approved", ready=False)] + ready[1:]
        nodes = by_version(board(SCREEN, withdrawn, **delivered))
        self.assertEqual((nodes["v1"]["stage"], nodes["v1"]["blocked_reason"], nodes["v1"]["next_action"]), ("blocked", "review_withdrawn", "none"))

    def test_single_then_paired_stages_and_the_legacy_names(self):
        def node(single=None, paired=None, quality=None):
            assets = [execution("v1", "c1", 1, ai=single, paired=paired, quality=quality)] + [execution(f"v{n}", f"c{n}", n) for n in (4, 5)]
            result = board(SCREEN, assets)
            return result, by_version(result)["v1"]
        cases = [((check("succeeded"), check("running")), ("paired_qc_running", "paired_qc", "none")),
                 ((check("succeeded"), check("uncertain")), ("paired_qc_running", "paired_qc", "none")),
                 ((check("succeeded"), check("succeeded", current=False)), ("single_qc_done", "paired_qc", "paired_qc")),
                 ((check("succeeded"), check("succeeded", suggestion="needs_changes")), ("paired_qc_done", "review", "review")),
                 # A current paired result never stands in for a missing single-image result.
                 ((check("failed"), check("succeeded")), ("single_qc_pending", "single_qc", "single_qc")),
                 ((None, None, "approved"), ("paired_qc_done", "review", "review"))]
        for args, expected in cases:
            result, v1 = node(*args)
            self.assertEqual((v1["stage"], v1["gap"], v1["next_action"]), expected, args)
            self.assertEqual(v1["paired_ai_qc"], args[1])
        legacy = {"single_qc_running": ("single_qc_pending", "none"), "single_qc_done": ("paired_qc_pending", "ai_qc"),
                  "paired_qc_running": ("paired_qc_pending", "none"), "paired_qc_done": ("single_qc_done", "review"),
                  "single_qc_pending": ("single_qc_pending", "ai_qc")}
        for args, (stage, _, action) in cases[:4] + cases[-2:-1] + [((check("queued"), None), ("single_qc_running", "", "none"))]:
            result, _ = node(*args)
            old = by_version(legacy_qc_projection(result))["v1"]
            self.assertNotIn("paired_ai_qc", old)
            self.assertEqual((old["stage"], old["next_action"]), legacy[stage])

    def test_paired_scope_is_the_board_comparison_and_names_what_is_missing(self):
        assets = [execution("v1", "c1", 1), execution("v2", "c2", 2), execution("v4", "c4", 3, quality="needs_changes"),
                  execution("v4b", "c4", 4, parent="v4"), execution("v5", "c5", 5, blocking=True)]
        scope = comparison_scope(SCREEN, assets)
        self.assertEqual(scope["v1"], {"combination_ids": ["c4", "c5"], "compare_with": ["v4b"], "awaiting": ["c5"]})
        self.assertEqual(scope["v4b"], {"combination_ids": ["c1", "c2", "c3"], "compare_with": ["v1", "v2"], "awaiting": ["c3"]})
        nodes = by_version(board(SCREEN, assets))
        self.assertEqual({k: (n["compare_with"], n["awaiting_combination_ids"]) for k, n in nodes.items() if k},
            {k: (v["compare_with"], v["awaiting"]) for k, v in scope.items()})

    def test_closed_round_keeps_gaps_but_offers_no_new_generation(self):
        result = board(SCREEN, [execution("v1", "c1", 1)], round_state="closed", generation_open=False)
        nodes = executions_of(result)
        self.assertEqual({x["next_action"] for x in nodes}, {"none"})
        self.assertEqual([x["gap"] for x in nodes if x["asset_version_id"] is None], ["pending_generation"] * 4)


THUMBNAILS = "?include_board=true&include_board_thumbnails=true"


class TestBoardProjectionHttp(MarketingWorkflowFixtures):
    def round(self, row, flag="?include_board=true&include_qc_kinds=true"):
        response = self.client.get(f"/api/v1/marketing/rounds/{row['id']}{flag}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_round_board_projects_every_controlled_mock_execution_and_is_tenant_scoped(self):
        project = self.confirmed()
        self.generate(project)
        row = self.start_round(project)
        self.assertNotIn("board", self.round(row, ""))
        data = self.round(row)
        nodes = executions_of(data["board"])
        self.assertEqual(sorted(n["asset_version_id"] for n in nodes), sorted(a["asset_version_id"] for a in data["assets"]))
        groups = {c: g["id"] for g in data["snapshot"]["plan"]["groups"] for c in g["combination_ids"]}
        for node in nodes:
            for key in ("angle_id", "expression_id", "group_id", "compare_with", "gap", "next_action"):
                self.assertIn(key, node)
            self.assertEqual(node["group_id"], groups[node["combination_id"]])
            self.assertTrue(node["compare_with"])
            self.assertFalse({groups[a["combination_id"]] for a in data["assets"] if a["asset_version_id"] in node["compare_with"]} & {node["group_id"]})
            self.assertEqual((node["stage"], node["next_action"]), ("single_qc_pending", "single_qc"))
        legacy = executions_of(self.round(row, "?include_board=true")["board"])
        self.assertEqual({(n["stage"], n["next_action"]) for n in legacy}, {("single_qc_pending", "ai_qc")})
        self.assertFalse(any("paired_ai_qc" in n for n in legacy))
        self.assertEqual(data["board"]["primary_comparison"]["kind"], "between_groups")
        self.assertEqual(sum(cell is None for line in data["board"]["matrix"]["cells"] for cell in line), 5)
        self.assertEqual(self.round(row)["board"], data["board"])
        workflow = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow?include_board=true&include_qc_kinds=true").json()
        self.assertEqual(workflow["current"]["board"], data["board"])
        # Strict BFF clients that did not ask for thumbnails never see the key.
        self.assertFalse(any("thumbnail" in node for node in nodes))

        self.context = self._make_context()
        self.assertEqual(self.client.get(f"/api/v1/marketing/rounds/{row['id']}?include_board=true").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow?include_board=true").status_code, 404)

    def test_board_tiles_carry_the_signed_512px_derivative_or_null(self):
        project = self.confirmed()
        self.generate(project)
        row = self.start_round(project)
        ids = [a["asset_version_id"] for a in row["assets"]]
        bare = ids[0]
        with tenant_session(self.context) as session:
            versions = {v.id: v for v in session.scalars(select(AssetVersionRecord).where(
                AssetVersionRecord.workspace_id == self.context.workspace_id, AssetVersionRecord.id.in_(ids)))}
            keys = {vid: (v.thumbnail_object_key, v.committed_object_key) for vid, v in versions.items()}
            # A legacy version still awaiting the backfill has no derivative.
            versions[bare].thumbnail_object_key = versions[bare].thumbnail_storage_version_id = None
        self.assertTrue(all(thumb for thumb, _ in keys.values()))

        board_data = self.round(row, THUMBNAILS)["board"]
        MarketingBoard.model_validate(board_data)
        nodes = by_version(board_data)
        self.assertEqual(set(nodes), set(ids))
        self.assertIsNone(nodes[bare]["thumbnail"])
        for vid in ids[1:]:
            link = nodes[vid]["thumbnail"]
            thumb, original = keys[vid]
            self.assertIn(thumb, link["url"])
            self.assertNotIn(original, link["url"])  # the derivative, never the original
            self.assertEqual(link["mime_type"], "image/jpeg")
            self.assertLessEqual(max(link["width"], link["height"]), 512)
            self.assertTrue(link["expires_at"])
        current = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow{THUMBNAILS}").json()["current"]
        self.assertEqual(unsigned(current["board"]), unsigned(board_data))
        self.assertFalse(any("thumbnail" in node for node in executions_of(self.round(row)["board"])))

        # Another Workspace reads neither the round nor a link for its versions, even through the board lookup itself.
        mine = self.context
        self.context = self._make_context()
        self.assertEqual(self.client.get(f"/api/v1/marketing/rounds/{row['id']}{THUMBNAILS}").status_code, 404)
        with tenant_session(self.context) as session:
            self.assertEqual(board_thumbnail_refs(session, self.context, board_data), {})
        with tenant_session(mine) as session:
            self.assertEqual(set(board_thumbnail_refs(session, mine, board_data)), set(ids[1:]))

    def test_board_follows_the_frozen_delivery_to_imported_data(self):
        _, row = self.bound()
        nodes = executions_of(self.round(row)["board"])
        self.assertEqual({(n["stage"], n["gap"], n["next_action"]) for n in nodes}, {("bound", "data", "import")})
        self.assertTrue(all(n["delivery"]["entry_id"] and len(n["delivery"]["ad_ids"]) == 1 for n in nodes))
        row = self.command(row, "metrics/preview", self.csv(row), 201)
        row = self.command(row, "metrics/confirm", {"batch_id": row["batches"][-1]["id"]})
        nodes = executions_of(self.round(row)["board"])
        self.assertEqual({(n["stage"], n["next_action"]) for n in nodes}, {("has_data", "evaluate")})
        row = self.command(row, "evaluate")
        self.assertEqual({n["next_action"] for n in executions_of(self.round(row)["board"])}, {"none"})

    def test_scenario_7_reused_ad_waits_for_an_independent_new_execution(self):
        project = self.confirmed()
        self.generate(project)
        first = self.start_round(project)
        source = next(a for a in first["assets"] if a["combination_id"] == "c1_1")
        self.command(first, "close", {"reason": "user_stopped", "note": "Only one ad is carried into confirmation"})
        project = self.read(project)
        plan = project["plan"]
        plan.update(stage="confirm_combination", comparison_kind="business_retest", conclusion_scope="asset",
            combinations=[c for c in plan["combinations"] if c["id"] == "c1_1"], angles=plan["angles"][:1],
            groups=[{**plan["groups"][0], "combination_ids": ["c1_1"]}])
        response = self.edit(project, "confirm_plan", plan=plan)
        self.assertEqual(response.status_code, 200, response.text)
        project = response.json()
        row = self.start_round(project)
        row = self.command(row, "creatives/reuse", {"asset_version_id": source["asset_version_id"], "combination_id": "c1_1"}, 201)
        retest = self.round(row)["board"]
        self.assertEqual(retest["primary_comparison"], {"kind": "retest", "original_asset_version_ids": [source["asset_version_id"]], "new_execution_ids": []})
        nodes = executions_of(retest)
        self.assertEqual([(n["asset_version_id"], n["execution_index"], n["next_action"]) for n in nodes],
            [(source["asset_version_id"], 1, "single_qc"), (None, 2, "generate")])

        self.generate(project)  # the controlled Mock repeats the exact file: not a new execution
        nodes = by_version(self.round(row)["board"])
        copies = [n for n in nodes.values() if n["duplicate_of"] == source["asset_version_id"]]
        self.assertEqual([n["execution_index"] for n in copies], [1])
        self.assertIn(None, nodes)
        signed = by_version(self.round(row, THUMBNAILS)["board"])
        self.assertIsNone(signed[None]["thumbnail"])  # a placeholder has no image to preview
        self.assertIn("/generation_thumbnail/", signed[source["asset_version_id"]]["thumbnail"]["url"])

        self.generate(project, colour=(90, 10, 10))
        retest = self.round(row)["board"]
        nodes = by_version(retest)
        self.assertNotIn(None, nodes)
        fresh = retest["primary_comparison"]["new_execution_ids"]
        self.assertEqual(len(fresh), 1)
        self.assertEqual(nodes[fresh[0]]["compare_with"], [source["asset_version_id"]])
        self.assertEqual(nodes[source["asset_version_id"]]["compare_with"], fresh)
        self.assertEqual(nodes[fresh[0]]["execution_index"], 2)
