"""This round's schedule (#253, design §5.7, §5.9): a test round's moved start, early end and minimal result.

The commands that make a test round's ad package come with #249 and #250, and its own report upload with #251.
Until then these tests take the round of a confirmed plan-first test -- its package made, every ad bound to a
Meta ad -- and turn its project into a test project (``flow = "ads_test"``, naming ads-first ideas), and they
confirm a test round's report rows directly when the rows fall outside the frozen dates.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import WorkspaceRecord, new_id, utcnow
from app.marketing import ad_test_schedule, ad_tests, workflow
from app.marketing.ad_tests_contracts import MarketingTestResult, MarketingTestRoundSchedule
from app.marketing.evaluation import evaluate_snapshot
from app.marketing.models import MarketingProjectRecord
from app.marketing.workflow_models import (MarketingAdBindingRecord, MarketingEvaluationRecord,
    MarketingMetricBatchRecord, MarketingRoundRecord, MarketingRoundScheduleChangeRecord)
from tests.test_marketing_tests import MarketingTestFixtures


def days(first, count):
    return [first + timedelta(days=offset) for offset in range(count)]


def at(day):
    """A moment on ``day`` (UTC, the fixtures' account time zone)."""
    return datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)


class ScheduleFixtures(MarketingTestFixtures):
    def bound_test(self):
        """A test round, 2025-01-01 to 2025-01-07, with its ad package made and every ad bound to a Meta ad."""
        project, row = self.bound()
        ideas_id = self.ideas_project()
        with tenant_session(self.context) as session:
            record = session.get(MarketingProjectRecord, project["id"])
            record.flow, record.source_project_id = ad_tests.FLOW, ideas_id
        return project["id"], row

    def round_row(self, round_id):
        return self.client.get(f"/api/v1/marketing/rounds/{round_id}").json()["data"]

    def schedule_command(self, test_id, round_id, suffix, body, status=200, *, revision=None, key=None):
        revision = revision if revision is not None else self.round_row(round_id)["revision"]
        response = self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/{suffix}",
                                    headers=self.headers(revision, key), json=body)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def move_start(self, test_id, round_id, start, status=200, **kwargs):
        return self.schedule_command(test_id, round_id, "start-date",
                                     {"start_date": str(start), "reason": "The ads went live later than planned"},
                                     status, **kwargs)

    def end_early(self, test_id, round_id, end, status=200, **kwargs):
        return self.schedule_command(test_id, round_id, "end", {"end_date": str(end), "reason": "Stopped the ads"},
                                     status, **kwargs)

    def result(self, test_id, round_id):
        response = self.client.get(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/result")
        self.assertEqual(response.status_code, 200, response.text)
        MarketingTestResult.model_validate(response.json())
        return response.json()

    def evaluate(self, round_id):
        return self.command(self.round_row(round_id), "evaluate")

    def confirm_rows(self, round_id, report_days, purchases=10):
        """A confirmed report with one row per day and bound ad, spend varying by day (Acme Charge's Meta ads)."""
        with tenant_session(self.context) as session:
            bindings = workflow.scoped(session, self.context, MarketingAdBindingRecord, round_id=round_id)
            rows = [{"date": str(day), "ad_id": b.ad_id, "entry_id": b.entry_id, "spend": str(10 + index),
                     "impressions": 10000, "clicks": 100, "purchases": str(purchases), "purchase_value": "300"}
                    for index, day in enumerate(report_days) for b in sorted(bindings, key=lambda b: b.ad_id)]
            session.add(MarketingMetricBatchRecord(id=new_id("mmb"), workspace_id=self.context.workspace_id,
                round_id=round_id, request_hash=new_id("hash"), raw_hash=new_id("hash"), raw_csv="date,ad_id\n",
                metadata_json={"filename": "acme-charge-meta.csv", "field_mapping": {}, "ad_account_id": "123",
                               "account_timezone": "UTC", "currency": "USD",
                               "attribution_window": "7d_click_1d_view", "source_mode": "customer_file"},
                rows_json=rows, issues_json=[], state="confirmed"))

    def upload(self, round_id, report_days, purchases):
        """A report inside the frozen dates: a test round's full report (#251), an earlier test's own upload."""
        row = self.round_row(round_id)
        text = "date,ad_id,spend,impressions,clicks,purchases,purchase_value\n" + "".join(
            f"{day},{binding['ad_id']},{10 + index},10000,100,{purchases},300\n"
            for index, day in enumerate(report_days) for binding in row["bindings"])
        body = self.csv(row, csv_text=text)
        with tenant_session(self.context) as session:
            frozen = workflow.get_round(session, self.context, round_id)
            test_id = frozen.project_id if workflow.full_report_round(session, self.context, frozen) else None
        if test_id is None:
            row = self.command(row, "metrics/preview", body, 201)
            self.assertEqual(row["batches"][-1]["issues"], [])
            return self.command(row, "metrics/confirm", {"batch_id": row["batches"][-1]["id"]})
        path = f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/report"
        preview = self.client.post(f"{path}/preview", headers=self.headers(row["revision"]),
                                   json={**body, "source_mode": "customer_file"})
        self.assertEqual(preview.status_code, 201, preview.text)
        self.assertEqual(preview.json()["issues"], [])
        confirmed = self.client.post(f"{path}/confirm", headers=self.headers(int(preview.headers["etag"].strip('"'))),
                                     json={"batch_id": preview.json()["id"]})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        return self.round_row(round_id)

    def changes(self, round_id):
        with tenant_session(self.context) as session:
            return session.scalars(select(MarketingRoundScheduleChangeRecord).where(
                MarketingRoundScheduleChangeRecord.workspace_id == self.context.workspace_id,
                MarketingRoundScheduleChangeRecord.round_id == round_id)).all()


class MovedStartTests(ScheduleFixtures):
    def test_a_round_that_went_live_three_days_late_reaches_a_conclusion(self):
        test_id, row = self.bound_test()
        round_id, frozen = row["id"], self.round_row(row["id"])
        self.assertEqual((frozen["start_date"], frozen["end_date"]), ("2025-01-01", "2025-01-07"))

        # The ads went live on 4 January: the start moves there and the end with it, seven days in all.
        moved = self.move_start(test_id, round_id, date(2025, 1, 4))
        MarketingTestRoundSchedule.model_validate(moved)
        self.assertEqual({k: moved[k] for k in ("start_date", "end_date", "effective_start_date", "effective_end_date",
                                                 "ended_early", "result_date", "start_movable")},
                         {"start_date": "2025-01-01", "end_date": "2025-01-07", "effective_start_date": "2025-01-04",
                          "effective_end_date": "2025-01-10", "ended_early": False, "result_date": "2025-01-19",
                          "start_movable": True})
        self.assertEqual([(c["kind"], c["date"], c["actor_id"]) for c in moved["changes"]],
                         [("start_moved", "2025-01-04", self.context.initiator_id)])
        self.assertEqual(moved["revision"], frozen["revision"] + 1)

        # The report covers the seven days the ads ran.
        self.confirm_rows(round_id, days(date(2025, 1, 4), 7))
        evaluated = self.evaluate(round_id)
        self.assertEqual(evaluated["state"], "evaluated")
        with tenant_session(self.context) as session:
            evaluation = workflow.latest_evaluation(session, self.context, workflow.get_round(session, self.context, round_id))
            inputs = evaluation.input_json
        self.assertEqual((inputs["start_date"], inputs["end_date"], inputs["mature"]), ("2025-01-04", "2025-01-10", True))
        self.assertEqual(inputs["schedule"], {"change_ids": [moved["changes"][0]["id"]], "ended_early": False})
        groups = [r for r in evaluation.result_json["results"] if r["kind"] == "group"]
        self.assertTrue(groups)
        for group in groups:
            self.assertNotIn("missing_ad_day_data", group["reason_codes"])
            self.assertNotIn("final_checkpoint_not_mature", group["reason_codes"])

        result = self.result(test_id, round_id)
        self.assertEqual((result["mature"], result["current"], result["scope"], result["evaluation_id"]),
                         (True, True, "these_ads", evaluation.id))
        self.assertEqual(len(result["comparisons"]), len(groups))
        for comparison in result["comparisons"]:
            self.assertIn(comparison["status"], ("meets_goal", "misses_goal"))  # a conclusion, not "cannot tell yet"
            self.assertEqual(comparison["remaining_purchases"], 0)
            self.assertTrue(comparison["asset_version_ids"])

        # On the frozen dates the same report would leave three days missing and nothing concluded.
        frozen_inputs = {**inputs, "start_date": "2025-01-01", "end_date": "2025-01-07"}
        frozen_inputs.pop("schedule")
        for group in (r for r in evaluate_snapshot(frozen_inputs)["results"] if r["kind"] == "group"):
            self.assertEqual(group["business_status"], "insufficient")
            self.assertIn("missing_ad_day_data", group["reason_codes"])

        # The frozen snapshot is never rewritten; the test reads the effective dates.
        again = self.round_row(round_id)
        self.assertEqual((again["start_date"], again["end_date"], again["snapshot"]),
                         (frozen["start_date"], frozen["end_date"], frozen["snapshot"]))
        detail = self.client.get(f"/api/v1/marketing/tests/{test_id}").json()
        self.assertEqual([(r["start_date"], r["end_date"], r["effective_start_date"], r["effective_end_date"])
                          for r in detail["rounds"]], [("2025-01-01", "2025-01-07", "2025-01-04", "2025-01-10")])

        # Once a report is confirmed the start no longer moves.
        self.assertFalse(result["schedule"]["start_movable"])
        refused = self.move_start(test_id, round_id, date(2025, 1, 5), 409)
        self.assertEqual(refused["code"], "round_report_confirmed")
        self.assertEqual(len(self.changes(round_id)), 1)

    def test_the_start_moves_before_the_first_report_only_within_the_rules(self):
        test_id, round_id = self.make_test()  # byok, frozen today, planned to start tomorrow
        with tenant_session(self.context) as session:
            row = workflow.get_round(session, self.context, round_id)
            planned, frozen_on = row.start_date, row.created_at.date()
        # Real delivery cannot have started before its rules were frozen; the day they were is fine.
        refused = self.move_start(test_id, round_id, frozen_on - timedelta(days=1), 422)
        self.assertEqual((refused["code"], refused["params"]), ("start_date_out_of_range", {"earliest": str(frozen_on)}))
        self.assertEqual(self.move_start(test_id, round_id, planned, 409)["code"], "schedule_unchanged")
        moved = self.move_start(test_id, round_id, frozen_on)
        self.assertEqual(moved["effective_start_date"], str(frozen_on))
        # The user puts it back three days later; the latest move wins and both stay on record.
        later = self.move_start(test_id, round_id, planned + timedelta(days=3))
        self.assertEqual((later["effective_start_date"], later["effective_end_date"]),
                         (str(planned + timedelta(days=3)), str(date.fromisoformat(later["end_date"]) + timedelta(days=3))))
        self.assertEqual([c["kind"] for c in later["changes"]], ["start_moved", "start_moved"])
        self.assertEqual(self.client.get(f"/api/v1/marketing/tests/{test_id}").json()["rounds"][0]["effective_start_date"],
                         str(planned + timedelta(days=3)))
        # A stale revision and a replay: the first is refused, the second returns the same change.
        stale = self.move_start(test_id, round_id, planned + timedelta(days=4), 409, revision=later["revision"] - 1)
        self.assertEqual(stale["code"], "stale_round_revision")
        key = new_id("key")
        first = self.move_start(test_id, round_id, planned + timedelta(days=4), key=key)
        replay = self.move_start(test_id, round_id, planned + timedelta(days=4), key=key, revision=later["revision"])
        self.assertEqual(first, replay)
        self.assertEqual(len(self.changes(round_id)), 3)
        # A closed round keeps its schedule.
        self.close(round_id)
        self.assertEqual(self.move_start(test_id, round_id, planned, 409)["code"], "round_closed")


class EarlyEndTests(ScheduleFixtures):
    def test_an_early_ended_round_is_evaluated_after_its_last_day_and_the_attribution_lag(self):
        test_id, row = self.bound_test()
        round_id = row["id"]
        # The report covers five days, but the ads were stopped after the third.
        self.upload(round_id, days(date(2025, 1, 1), 5), purchases=1)
        ended = self.end_early(test_id, round_id, date(2025, 1, 3))
        self.assertEqual({k: ended[k] for k in ("effective_start_date", "effective_end_date", "ended_early", "result_date",
                                                 "start_movable")},
                         {"effective_start_date": "2025-01-01", "effective_end_date": "2025-01-03", "ended_early": True,
                          "result_date": "2025-01-12", "start_movable": False})
        # An early end keeps the start where it is.
        self.assertEqual(self.move_start(test_id, round_id, date(2025, 1, 2), 409)["code"], "round_ended_early")

        # On the last day plus the attribution lag (8 days) it is not mature yet ...
        with patch("app.marketing.workflow.utcnow", return_value=at(date(2025, 1, 11))), \
                patch("app.marketing.ad_test_schedule.utcnow", return_value=at(date(2025, 1, 11))):
            self.evaluate(round_id)
            early = self.result(test_id, round_id)
        self.assertFalse(early["mature"])
        self.assertEqual({c["status"] for c in early["comparisons"]}, {"not_clear"})
        # ... the day after, it is: the result says the round ended early and how many orders are missing.
        with patch("app.marketing.workflow.utcnow", return_value=at(date(2025, 1, 12))), \
                patch("app.marketing.ad_test_schedule.utcnow", return_value=at(date(2025, 1, 12))):
            self.evaluate(round_id)
            result = self.result(test_id, round_id)
        self.assertNotEqual(result["evaluation_id"], early["evaluation_id"])
        self.assertEqual((result["mature"], result["current"], result["schedule"]["ended_early"],
                          result["schedule"]["effective_end_date"], result["min_purchases"]),
                         (True, True, True, "2025-01-03", 20))
        with tenant_session(self.context) as session:
            evaluation = session.get(MarketingEvaluationRecord, result["evaluation_id"])
            inputs = evaluation.input_json
        # Only the three days up to the early end count; the two after it are kept in the report, not counted.
        self.assertEqual(sorted({r["date"] for r in inputs["rows"]}), ["2025-01-01", "2025-01-02", "2025-01-03"])
        self.assertTrue(inputs["mature"])
        self.assertTrue(inputs["schedule"]["ended_early"])
        groups = {r["id"].removeprefix("group:"): r for r in evaluation.result_json["results"] if r["kind"] == "group"}
        for comparison in result["comparisons"]:
            group = groups[comparison["id"]]
            purchases = 3 * len(group["entry_ids"])  # one order a day per ad, three days
            self.assertEqual((comparison["status"], comparison["purchases"], comparison["remaining_purchases"]),
                             ("not_clear", str(purchases), 20 - purchases))
            self.assertIn("insufficient_purchases", group["reason_codes"])
            self.assertNotIn("final_checkpoint_not_mature", group["reason_codes"])
        # The frozen snapshot is unchanged.
        self.assertEqual((self.round_row(round_id)["start_date"], self.round_row(round_id)["end_date"]),
                         ("2025-01-01", "2025-01-07"))

        # A later correction of the last day wins; the evaluation is no longer current until evaluated again.
        corrected = self.end_early(test_id, round_id, date(2025, 1, 5))
        self.assertEqual((corrected["effective_end_date"], [c["date"] for c in corrected["changes"]]),
                         ("2025-01-05", ["2025-01-03", "2025-01-05"]))
        self.assertFalse(self.result(test_id, round_id)["current"])

    def test_the_last_day_lies_between_the_start_today_and_the_planned_end(self):
        test_id, row = self.bound_test()
        round_id = row["id"]
        for day in (date(2024, 12, 31), date(2025, 1, 7), date(2025, 1, 9)):  # before the start, the planned end, after it
            refused = self.end_early(test_id, round_id, day, 422)
            self.assertEqual((refused["code"], refused["params"]),
                             ("end_date_out_of_range", {"earliest": "2025-01-01", "latest": "2025-01-06"}))
        with patch("app.marketing.ad_test_schedule.utcnow", return_value=at(date(2025, 1, 4))):
            refused = self.end_early(test_id, round_id, date(2025, 1, 5), 422)
            self.assertEqual(refused["params"]["latest"], "2025-01-04")  # never after today
            self.end_early(test_id, round_id, date(2025, 1, 4))
        self.assertEqual(self.end_early(test_id, round_id, date(2025, 1, 4), 409)["code"], "schedule_unchanged")

        # Before its ad package is made a round has not run: stopping it is cancelling the test (#250).
        other_test, other_round = self.make_test()
        self.assertEqual(self.end_early(other_test, other_round, utcnow().date(), 409)["code"], "handoff_not_ready")
        self.assertEqual(self.changes(other_round), [])


class EarlierTestsTests(ScheduleFixtures):
    def test_a_round_of_an_earlier_test_is_evaluated_exactly_as_before(self):
        project, row = self.bound()
        round_id = row["id"]
        self.upload(round_id, days(date(2025, 1, 1), 7), purchases=10)
        self.evaluate(round_id)
        with tenant_session(self.context) as session:
            evaluation = workflow.latest_evaluation(session, self.context, workflow.get_round(session, self.context, round_id))
            first_id, inputs = evaluation.id, evaluation.input_json
        self.assertNotIn("schedule", inputs)
        self.assertEqual((inputs["start_date"], inputs["end_date"]), ("2025-01-01", "2025-01-07"))

        # The schedule endpoints do not reach it ...
        for suffix, body in (("start-date", {"start_date": "2025-01-04"}), ("end", {"end_date": "2025-01-03"})):
            response = self.client.post(f"/api/v1/marketing/tests/{project['id']}/rounds/{round_id}/{suffix}",
                                        headers=self.headers(self.round_row(round_id)["revision"]), json=body)
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_test_not_found"), suffix)
        response = self.client.get(f"/api/v1/marketing/tests/{project['id']}/rounds/{round_id}/result")
        self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_test_not_found"))

        # ... and even a schedule row written beside it changes nothing: its evaluation keeps the frozen dates.
        with tenant_session(self.context) as session:
            session.add(MarketingRoundScheduleChangeRecord(id=new_id("mrsc"), workspace_id=self.context.workspace_id,
                round_id=round_id, kind="ended_early", date=date(2025, 1, 3), actor_id="user_a"))
        self.evaluate(round_id)
        with tenant_session(self.context) as session:
            row = workflow.get_round(session, self.context, round_id)
            self.assertEqual(ad_test_schedule.effective_dates(session, self.context, row), (date(2025, 1, 1), date(2025, 1, 7)))
            self.assertEqual([e.id for e in workflow.scoped(session, self.context, MarketingEvaluationRecord, round_id=round_id)],
                             [first_id])
            self.assertTrue(workflow.evaluation_current(session, self.context, row, workflow.latest_evaluation(session, self.context, row)))


class ScheduleAccessTests(ScheduleFixtures):
    def test_another_workspace_can_neither_read_nor_change_a_round_schedule(self):
        test_id, round_id = self.make_test()
        revision = self.round_row(round_id)["revision"]
        other = self._make_context()
        self.use(other)
        responses = {
            "start": self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/start-date",
                                      headers=self.headers(revision), json={"start_date": str(utcnow().date())}),
            "end": self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/end",
                                    headers=self.headers(revision), json={"end_date": str(utcnow().date())}),
            "result": self.client.get(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/result"),
        }
        missing = self.client.get(f"/api/v1/marketing/tests/{new_id('mp')}/rounds/{round_id}/result")
        for name, response in responses.items():
            self.assertEqual((response.status_code, response.json()["code"], response.json()["detail"]),
                             (404, "marketing_test_not_found", missing.json()["detail"]), name)
        with tenant_session(other) as session:
            self.assertEqual(session.scalars(select(MarketingRoundScheduleChangeRecord).where(
                MarketingRoundScheduleChangeRecord.workspace_id == other.workspace_id)).all(), [])
        self.use(self.context)
        self.assertEqual(self.changes(round_id), [])
        self.assertEqual(self.round_row(round_id)["revision"], revision)

    def test_a_round_of_another_test_is_not_found_under_this_one(self):
        test_id, _ = self.make_test()
        _, round_id = self.make_test()
        response = self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/start-date",
                                    headers=self.headers(self.round_row(round_id)["revision"]),
                                    json={"start_date": str(utcnow().date())})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "round_not_found"))
        response = self.client.get(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/result")
        self.assertEqual((response.status_code, response.json()["code"]), (404, "round_not_found"))
        self.assertEqual(self.changes(round_id), [])

    def test_members_and_owners_change_it_and_a_workspace_without_current_authorization_cannot(self):
        test_id, round_id = self.make_test()
        with tenant_session(self.context) as session:
            planned = workflow.get_round(session, self.context, round_id).start_date
        # Moving the start needs the If-Match revision.
        response = self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/start-date",
                                    headers=self.headers(), json={"start_date": str(planned + timedelta(days=1))})
        self.assertEqual(response.status_code, 428, response.text)
        # A suspended Workspace changes nothing.
        with platform_session() as session:
            session.get(WorkspaceRecord, self.context.workspace_id).lifecycle = "suspended"
        refused = self.move_start(test_id, round_id, planned + timedelta(days=1), 403)
        self.assertEqual(refused["code"], "workspace_inactive")
        with platform_session() as session:
            session.get(WorkspaceRecord, self.context.workspace_id).lifecycle = "active"
        self.assertEqual(self.changes(round_id), [])
        # A Member and the Owner both can; each change names who made it.
        self.move_start(test_id, round_id, planned + timedelta(days=1))
        owner = self.owner_ctx(self.context)
        self.use(owner)
        moved = self.move_start(test_id, round_id, planned + timedelta(days=2))
        self.assertEqual([c["actor_id"] for c in moved["changes"]], [self.context.initiator_id, owner.initiator_id])
        self.assertEqual(self.result(test_id, round_id)["schedule"]["changes"], moved["changes"])
        self.use(self.context)


class ScheduleMigrationDowngradeTests(unittest.TestCase):
    """Dropping the schedule changes would put moved rounds back on their frozen dates, so downgrade refuses."""
    ROOT = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-round-schedule-migration-")
        self.addCleanup(self.temp.cleanup)
        self.database = f"{self.temp.name}/migration.db"
        self.environment = {**os.environ, "MARKETAI_DATABASE_URL": f"sqlite:///{self.database}",
                            "MARKETAI_DATA_DIR": f"{self.temp.name}/data"}
        upgraded = self.alembic("upgrade", "head")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

    def alembic(self, *arguments):
        return subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=self.ROOT, env=self.environment,
                              capture_output=True, text=True, check=False)

    def tables(self):
        with sqlite3.connect(self.database) as connection:
            return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def test_downgrade_without_changes_drops_the_table(self):
        downgraded = self.alembic("downgrade", "zzt247a1b2c3")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        self.assertNotIn("marketing_round_schedule_changes", self.tables())

    def test_downgrade_refuses_while_a_change_exists(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO marketing_round_schedule_changes (id, workspace_id, round_id, kind, date,"
                               " actor_id, created_at) VALUES ('mrsc_1', 'ws_1', 'mround_1', 'start_moved', '2026-11-24',"
                               " 'user_a', '2026-11-21')")
        downgraded = self.alembic("downgrade", "zzt247a1b2c3")
        self.assertNotEqual(downgraded.returncode, 0)
        self.assertIn("round schedule changes exist", downgraded.stderr)
        self.assertIn("marketing_round_schedule_changes", self.tables())
