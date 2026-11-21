"""Full reports for a test project's rounds (ADR 0019 decision 6, step 2 design §5.7, #251).

The user uploads Meta's report "from the start to today" each time. A later confirmed report replaces the
earlier one's overlapping days, both stay on record, and rows of other ads or days outside the round are
stored but not counted. The round here is a test project's round (``tests.test_marketing_tests``) whose ad
package and matched Meta ads are seeded directly: the commands that make them for a test come with #249, #250
and #252.
"""
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import WorkspaceRecord, new_id
from app.marketing import workflow
from app.marketing.ad_test_reports_contracts import MarketingTestReport
from app.marketing.workflow_models import (MarketingAdBindingRecord as Binding, MarketingHandoffRecord as Handoff,
                                           MarketingMetricBatchRecord as Batch)
from app.platform.operations import create_operation
from tests.test_marketing_tests import MarketingTestFixtures

ACCOUNT = "4815162342"
OURS = ("1001", "1002")
#: A Meta ad the user runs alongside the test (a Kestrel Power retargeting ad, say): not one of this round's.
OTHER = "7007"


class TestReportFixtures(MarketingTestFixtures):
    def report_round(self, ctx=None, *, package=True):
        """A test project's round with its ad package ready and its two ads matched to Meta ads ``OURS``."""
        ctx = ctx or self.context
        test_id, round_id = self.make_test(ctx)
        if package:
            with tenant_session(ctx) as session:
                row = workflow.get_round(session, ctx, round_id)
                operation = create_operation(session, command_type="marketing.export")
                entries = [self.entry(row.snapshot_json["plan"], index) for index in range(len(OURS))]
                handoff = Handoff(id=new_id("mh"), workspace_id=ctx.workspace_id, round_id=row.id,
                                  operation_id=operation.id, state="ready", manifest_json={"entries": entries})
                session.add(handoff)
                session.flush()
                for entry, ad in zip(entries, OURS):
                    session.add(Binding(id=new_id("mab"), workspace_id=ctx.workspace_id, round_id=row.id,
                        handoff_id=handoff.id, entry_id=entry["entry_id"], ad_account_id=ACCOUNT, ad_id=ad,
                        start_date=row.start_date, end_date=row.end_date,
                        conditions_json={"conditions_match": True, "deviations": []}))
                row.state = "awaiting_data"
        with tenant_session(ctx) as session:
            row = workflow.get_round(session, ctx, round_id)
            self.start, self.end = row.start_date, row.end_date
        return test_id, round_id

    @staticmethod
    def entry(plan, index):
        """An ad-package entry for the plan's ``index``-th combination, as the export writes one."""
        combination = plan["combinations"][index % len(plan["combinations"])]
        group = next(g for g in plan["groups"] if combination["id"] in g["combination_ids"])
        angle = next(a for a in plan["angles"] if a["id"] == combination["angle_id"])
        expression = next(e for e in angle["expressions"] if e["id"] == combination["expression_id"])
        return {"entry_id": f"entry_{index + 1}", "asset_version_id": new_id("av"), "group_id": group["id"],
                "group_name": group["name"], "combination_id": combination["id"], "angle_id": angle["id"],
                "expression_id": expression["id"], "proposition": angle["proposition"],
                "expression_name": expression["name"], "ad_name": f"MA-ACME-{index + 1}",
                "headline": "Acme Charge 65W travel charger", "body_copy": "Three ports, one charger, every trip.",
                "content_sha256": "0" * 64, "mime_type": "image/png", "recipe_hash": "0" * 64}

    def day(self, number):
        """The round's ``number``-th day: day 1 is its start date."""
        return self.start + timedelta(days=number - 1)

    def meta_csv(self, days, ads=OURS, *, spend=10, purchases=1, currency="USD", extra=()):
        """Meta's daily export, its columns as Ads Manager names them; ``extra`` adds ``(day, ad)`` rows."""
        lines = [f"Day,Ad ID,Ad name,Amount spent ({currency}),Impressions,Link clicks,Purchases,"
                 f"Purchases conversion value ({currency})"]
        for when, ad in [(self.day(d), ad) for d in days for ad in ads] + list(extra):
            lines.append(f"{when},{ad},Acme Charge {ad},{spend},1000,30,{purchases},{purchases * 40}")
        return "\n".join(lines) + "\n"

    def body(self, csv_text, **overrides):
        return {"csv_text": csv_text, "filename": "acme-charge-meta-report.csv", "ad_account_id": f"act_{ACCOUNT}",
                "account_timezone": "UTC", "currency": "USD", "attribution_window": "7d_click_1d_view",
                "source_mode": "customer_file", **overrides}

    def revision(self, round_id):
        with tenant_session(self.context) as session:
            return workflow.get_round(session, self.context, round_id).revision

    def post(self, test_id, round_id, step, body, *, revision=None, key=None):
        return self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/report/{step}",
                                headers=self.headers(revision or self.revision(round_id), key), json=body)

    def preview(self, test_id, round_id, csv_text, **overrides):
        response = self.post(test_id, round_id, "preview", self.body(csv_text, **overrides))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers["etag"], f'"{self.revision(round_id)}"')
        MarketingTestReport.model_validate(response.json())
        return response.json()

    def confirm(self, test_id, round_id, report, status=200):
        response = self.post(test_id, round_id, "confirm", {"batch_id": report["id"]})
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def counted(self, round_id, ctx=None):
        ctx = ctx or self.context
        with tenant_session(ctx) as session:
            return workflow.counted_report_rows(session, ctx, workflow.get_round(session, ctx, round_id))

    def batches(self, round_id):
        with tenant_session(self.context) as session:
            return [(b.state, len(b.rows_json)) for b in sorted(session.scalars(select(Batch).where(
                Batch.workspace_id == self.context.workspace_id, Batch.round_id == round_id)).all(),
                key=lambda b: (b.created_at, b.id))]


class FullReportTests(TestReportFixtures):
    def test_a_later_full_report_replaces_the_overlapping_days_and_both_stay_on_record(self):
        test_id, round_id = self.report_round()
        first = self.preview(test_id, round_id, self.meta_csv(range(1, 4), spend=10, purchases=1))
        self.assertEqual((first["state"], first["issues"], first["excluded"]), ("preview", [], []))
        self.assertEqual((first["row_count"], first["counted_rows"], first["replaces_rows"]), (6, 6, 0))
        self.assertEqual((first["first_date"], first["last_date"]), (str(self.day(1)), str(self.day(3))))
        self.assertEqual(self.confirm(test_id, round_id, first)["state"], "confirmed")
        self.assertEqual({(r["date"], r["spend"]) for r in self.counted(round_id)},
                         {(str(self.day(d)), "10") for d in range(1, 4)})

        # Days 1-3 again, with later attributed orders, and days 4-5.
        second = self.preview(test_id, round_id, self.meta_csv(range(1, 6), spend=12, purchases=2))
        self.assertEqual((second["row_count"], second["counted_rows"], second["replaces_rows"]), (10, 10, 6))
        self.assertEqual(self.confirm(test_id, round_id, second)["state"], "confirmed")

        counted = self.counted(round_id)
        self.assertEqual([(r["date"], r["ad_id"]) for r in counted],
                         [(str(self.day(d)), ad) for d in range(1, 6) for ad in OURS])
        self.assertEqual({(r["spend"], r["purchases"]) for r in counted}, {("12", "2")})
        self.assertEqual({r["entry_id"] for r in counted}, {"entry_1", "entry_2"})
        # Both uploads are kept, unchanged, and confirmed.
        self.assertEqual(self.batches(round_id), [("confirmed", 6), ("confirmed", 10)])
        # The earlier report now counts nothing: every one of its days was reported again.
        again = self.confirm(test_id, round_id, first)
        self.assertEqual((again["state"], again["counted_rows"], again["replaced_rows"]), ("confirmed", 0, 6))
        response = self.client.get(f"/api/v1/marketing/rounds/{round_id}")
        self.assertEqual([b["state"] for b in response.json()["data"]["batches"]], ["confirmed", "confirmed"])

        # The result is the one only the later report would give.
        only_id, only_round = self.report_round()
        self.confirm(only_id, only_round, self.preview(only_id, only_round, self.meta_csv(range(1, 6), spend=12, purchases=2)))
        self.assertEqual(self.counted(only_round), counted)

        # The numbers that use the data read the counted rows: the spend and the evaluation's input.
        with tenant_session(self.context) as session:
            row = workflow.get_round(session, self.context, round_id)
            self.assertEqual(workflow.project_spend(session, self.context, [row]), Decimal("120"))
            evaluation = workflow.evaluate(session, self.context, row)
            self.assertEqual(evaluation.input_json["rows"], counted)
            self.assertEqual(evaluation.result_json["round_spend"], "120")
            self.assertTrue(workflow.evaluation_current(session, self.context, row, evaluation))

    def test_rows_of_other_ads_and_other_days_are_kept_but_not_counted(self):
        test_id, round_id = self.report_round()
        before, after = self.start - timedelta(days=2), self.end + timedelta(days=1)
        report = self.preview(test_id, round_id, self.meta_csv(range(1, 4), extra=[
            (self.day(1), OTHER), (self.day(2), OTHER), (self.day(3), OTHER),
            (before, OURS[0]), (before + timedelta(days=1), OURS[0]), (after, OURS[1])]))
        self.assertEqual(report["issues"], [])
        self.assertEqual((report["row_count"], report["counted_rows"]), (12, 6))
        self.assertEqual(report["excluded"], [
            {"ad_id": OURS[0], "reason": "before_start", "first_date": str(before),
             "last_date": str(before + timedelta(days=1)), "rows": 2, "spend": "20"},
            {"ad_id": OURS[1], "reason": "after_end", "first_date": str(after), "last_date": str(after),
             "rows": 1, "spend": "10"},
            {"ad_id": OTHER, "reason": "other_ad", "first_date": str(self.day(1)), "last_date": str(self.day(3)),
             "rows": 3, "spend": "30"}])
        confirmed = self.confirm(test_id, round_id, report)
        self.assertEqual((confirmed["state"], confirmed["excluded"]), ("confirmed", report["excluded"]))
        self.assertEqual({(r["date"], r["ad_id"]) for r in self.counted(round_id)},
                         {(str(self.day(d)), ad) for d in range(1, 4) for ad in OURS})
        self.assertEqual(self.batches(round_id), [("confirmed", 12)])  # stored whole

    def test_a_moved_start_moves_the_days_that_count(self):
        # The ads went live two days late (#253): the round's days move, the report's earliest days fall outside.
        test_id, round_id = self.report_round()
        moved = self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/start-date",
                                 headers=self.headers(self.revision(round_id)),
                                 json={"start_date": str(self.day(3)), "reason": "The ads went live two days late"})
        self.assertEqual(moved.status_code, 200, moved.text)
        days = (self.end - self.start).days + 1
        report = self.preview(test_id, round_id, self.meta_csv(range(1, days + 3)))
        self.assertEqual(report["counted_rows"], days * len(OURS))
        self.assertEqual([(g["ad_id"], g["reason"], g["first_date"], g["last_date"]) for g in report["excluded"]],
                         [(ad, "before_start", str(self.day(1)), str(self.day(2))) for ad in OURS])
        self.confirm(test_id, round_id, report)
        self.assertEqual(sorted({r["date"] for r in self.counted(round_id)}),
                         [str(self.day(d)) for d in range(3, days + 3)])
        # Once a report is confirmed, the start stays where it is.
        again = self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/start-date",
                                 headers=self.headers(self.revision(round_id)), json={"start_date": str(self.day(2))})
        self.assertEqual((again.status_code, again.json()["code"]), (409, "round_report_confirmed"))

    def test_a_report_in_another_currency_is_refused_by_name(self):
        test_id, round_id = self.report_round()
        # A Meta account billing in EUR, for a test set up in USD: the user says so, or the columns do.
        for overrides in ({"currency": "EUR"}, {}):
            report = self.preview(test_id, round_id, self.meta_csv(range(1, 4), currency="EUR"), **overrides)
            self.assertIn({"row": 0, "field": "currency", "code": "report_currency_mismatch",
                           "params": {"expected": "USD", "found": "EUR"}}, report["issues"])
            refused = self.confirm(test_id, round_id, report, status=409)
            self.assertEqual(refused["code"], "metrics_preflight_failed")
        self.assertEqual(self.counted(round_id), [])

    def test_timezone_attribution_and_account_differences_are_named(self):
        test_id, round_id = self.report_round()
        report = self.preview(test_id, round_id, self.meta_csv(range(1, 4)), account_timezone="Europe/Berlin",
                              attribution_window="1d_click", ad_account_id="act_999")
        self.assertEqual([i for i in report["issues"] if i["row"] == 0], [
            {"row": 0, "field": "context", "code": "report_timezone_mismatch",
             "params": {"expected": "UTC", "found": "Europe/Berlin"}},
            {"row": 0, "field": "attribution_window", "code": "report_attribution_mismatch",
             "params": {"expected": "7d_click_1d_view", "found": "1d_click"}},
            {"row": 0, "field": "ad_account_id", "code": "report_account_mismatch",
             "params": {"expected": ACCOUNT, "found": "999"}}])
        self.assertEqual(self.confirm(test_id, round_id, report, status=409)["code"], "metrics_preflight_failed")

    def test_bad_rows_keep_their_issue_codes(self):
        test_id, round_id = self.report_round()
        text = self.meta_csv(range(1, 3)) + f"{self.day(1)},{OURS[0]},again,5,10,1,0,0\n{self.day(3)},{OURS[1]},x,-1,10,1,0,0\n"
        report = self.preview(test_id, round_id, text)
        self.assertEqual({i["code"] for i in report["issues"]}, {"duplicate_ad_day", "invalid_nonnegative_number"})
        self.assertEqual(self.confirm(test_id, round_id, report, status=409)["code"], "metrics_preflight_failed")

    def test_only_a_customer_file_is_accepted(self):
        test_id, round_id = self.report_round()
        response = self.post(test_id, round_id, "preview", self.body(self.meta_csv(range(1, 3)), source_mode="controlled_mock"))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.batches(round_id), [])

    def test_a_report_older_than_a_confirmed_one_is_not_confirmed(self):
        test_id, round_id = self.report_round()
        older = self.preview(test_id, round_id, self.meta_csv(range(1, 3)))
        newer = self.preview(test_id, round_id, self.meta_csv(range(1, 4), spend=11))
        self.confirm(test_id, round_id, newer)
        refused = self.confirm(test_id, round_id, older, status=409)
        self.assertEqual(refused["code"], "test_report_outdated")
        self.assertEqual({r["spend"] for r in self.counted(round_id)}, {"11"})

    def test_the_same_upload_and_the_same_confirmation_are_answered_once(self):
        test_id, round_id = self.report_round()
        key, revision = new_id("key"), self.revision(round_id)
        body = self.body(self.meta_csv(range(1, 3)))
        first = self.post(test_id, round_id, "preview", body, revision=revision, key=key)
        replay = self.post(test_id, round_id, "preview", body, revision=revision, key=key)
        self.assertEqual((first.status_code, replay.status_code), (201, 201))
        self.assertEqual(first.json(), replay.json())
        again = self.preview(test_id, round_id, body["csv_text"])  # the same file uploaded again
        self.assertEqual(again["id"], first.json()["id"])
        self.confirm(test_id, round_id, again)
        self.assertEqual(self.confirm(test_id, round_id, again)["state"], "confirmed")
        self.assertEqual(self.batches(round_id), [("confirmed", 4)])

    def test_a_report_waits_for_the_ad_package(self):
        test_id, round_id = self.report_round(package=False)
        response = self.post(test_id, round_id, "preview", self.body(self.meta_csv(range(1, 3))))
        self.assertEqual((response.status_code, response.json()["code"]), (409, "handoff_not_ready"))
        self.assertEqual(self.batches(round_id), [])

    def test_a_stale_round_revision_is_refused(self):
        test_id, round_id = self.report_round()
        response = self.post(test_id, round_id, "preview", self.body(self.meta_csv(range(1, 3))),
                             revision=self.revision(round_id) + 1)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "stale_round_revision"))


class ReportScopeTests(TestReportFixtures):
    def test_another_workspace_cannot_preview_or_confirm(self):
        test_id, round_id = self.report_round()
        report = self.preview(test_id, round_id, self.meta_csv(range(1, 3)))
        other = self._make_context()
        self.use(other)
        for step, body in (("preview", self.body(self.meta_csv(range(1, 3)))), ("confirm", {"batch_id": report["id"]})):
            response = self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/report/{step}",
                                        headers=self.headers(1), json=body)
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_test_not_found"), step)
        self.use(self.context)
        self.assertEqual(self.batches(round_id), [("preview", 4)])

    def test_a_round_is_reached_only_through_its_own_test(self):
        test_id, round_id = self.report_round()
        other_test, other_round = self.report_round()
        report = self.preview(other_test, other_round, self.meta_csv(range(1, 3)))
        for step, body in (("preview", self.body(self.meta_csv(range(1, 3)))), ("confirm", {"batch_id": report["id"]})):
            response = self.post(test_id, other_round, step, body)
            self.assertEqual((response.status_code, response.json()["code"]), (404, "round_not_found"), step)
        # Another round's upload is not this round's to confirm.
        response = self.post(test_id, round_id, "confirm", {"batch_id": report["id"]})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "batch_not_found"))

    def test_a_suspended_workspace_cannot_upload_or_confirm(self):
        test_id, round_id = self.report_round()
        report = self.preview(test_id, round_id, self.meta_csv(range(1, 3)))
        with platform_session() as session:
            session.get(WorkspaceRecord, self.context.workspace_id).lifecycle = "suspended"
        try:
            for step, body in (("preview", self.body(self.meta_csv(range(1, 4)))), ("confirm", {"batch_id": report["id"]})):
                response = self.post(test_id, round_id, step, body)
                self.assertEqual((response.status_code, response.json()["code"]), (403, "workspace_inactive"), step)
        finally:
            with platform_session() as session:
                session.get(WorkspaceRecord, self.context.workspace_id).lifecycle = "active"
        self.assertEqual(self.batches(round_id), [("preview", 4)])


class PlanFirstRoundTests(TestReportFixtures):
    def test_plan_first_rounds_keep_their_upload_rules(self):
        project, row = self.bound()
        # A plan-first round is not a test's: the test report paths do not find it.
        with tenant_session(self.context) as session:
            plan_first = workflow.get_round(session, self.context, row["id"]).project_id
        response = self.client.post(f"/api/v1/marketing/tests/{plan_first}/rounds/{row['id']}/report/preview",
                                    headers=self.headers(row["revision"]), json=self.body("Day\n"))
        self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_test_not_found"))

        # Its own upload keeps refusing days it already has, and a correction must keep its scope.
        source = self.csv(row)
        row = self.command(row, "metrics/preview", source, 201)
        first = row["batches"][-1]
        row = self.command(row, "metrics/confirm", {"batch_id": first["id"]})
        row = self.command(row, "metrics/preview", self.csv(row, filename="meta-again.csv"), 201)
        self.assertIn("already_imported_ad_day", {i["code"] for i in row["batches"][-1]["issues"]})
        shorter = "\n".join(source["csv_text"].splitlines()[:-1]) + "\n"
        row = self.command(row, "metrics/preview", self.csv(row, csv_text=shorter, replaces_batch_id=first["id"]), 201)
        self.assertIn("replacement_scope_mismatch", {i["code"] for i in row["batches"][-1]["issues"]})
        self.assertEqual(len(self.counted(row["id"])), len(source["csv_text"].splitlines()) - 1)

    def test_a_test_round_takes_reports_only_on_the_test_paths(self):
        test_id, round_id = self.report_round()
        revision = self.revision(round_id)
        legacy = self.client.post(f"/api/v1/marketing/rounds/{round_id}/metrics/preview", headers=self.headers(revision),
                                  json={**self.body(self.meta_csv(range(1, 3))), "source_mode": "customer_file"})
        self.assertEqual((legacy.status_code, legacy.json()["code"]), (409, "test_report_required"))
        report = self.preview(test_id, round_id, self.meta_csv(range(1, 3)))
        legacy = self.client.post(f"/api/v1/marketing/rounds/{round_id}/metrics/confirm",
                                  headers=self.headers(self.revision(round_id)), json={"batch_id": report["id"]})
        self.assertEqual((legacy.status_code, legacy.json()["code"]), (409, "test_report_required"))
        self.assertEqual(self.batches(round_id), [("preview", 4)])
