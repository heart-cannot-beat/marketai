"""Which ad each Meta ad of a test round shows: suggested from its name, confirmed at once (design §5.7, #252).

Reading a report's ad names suggests a match from the two short keys MarketAI puts in names -- the ad package's
``MA-`` key first, then the image's ``MK-`` code -- among this round's ads only, and stores nothing. The user
then confirms every match in one command: all are written or none. Before the round's first report is
confirmed a match can be changed or taken away, each change a new record that keeps the old one; afterwards
matches can only be added. The round here is a test project's round whose ad package is seeded directly, as in
``tests.test_marketing_test_reports``.
"""
import csv
import hashlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import AuditEventRecord, WorkspaceRecord, new_id
from app.learnings.codes import asset_code
from app.marketing import export_text, workflow
from app.marketing.ad_test_reports_contracts import MarketingTestBindings, MarketingTestBindingSuggestions
from app.marketing.workflow_models import MarketingAdBindingRecord as Binding, MarketingHandoffRecord as Handoff
from app.platform.operations import create_operation
from tests.test_marketing_test_reports import ACCOUNT, TestReportFixtures

COLUMNS = ["Day", "Ad ID", "Ad name", "Amount spent (USD)", "Impressions", "Link clicks", "Purchases",
           "Purchases conversion value (USD)"]


class BindingFixtures(TestReportFixtures):
    def package_round(self, ctx=None, *, count=2):
        """A test round whose ad package is ready and none of whose ads is matched to a Meta ad yet."""
        ctx = ctx or self.context
        test_id, round_id = self.make_test(ctx)
        with tenant_session(ctx) as session:
            row = workflow.get_round(session, ctx, round_id)
            entries = []
            for index in range(count):
                entry = self.entry(row.snapshot_json["plan"], index)
                # As the export makes them: an entry id from the round and the image, its key in the ad name.
                entry_id = hashlib.sha256(f"{row.id}:{entry['asset_version_id']}".encode()).hexdigest()[:24]
                entries.append({**entry, "entry_id": entry_id,
                                "ad_name": f"{export_text.ad_key(entry_id)} Fast charging / Travel #{index + 1}"})
            operation = create_operation(session, command_type="marketing.export")
            session.add(Handoff(id=new_id("mh"), workspace_id=ctx.workspace_id, round_id=row.id,
                                operation_id=operation.id, state="ready", manifest_json={"entries": entries}))
            row.state = "handoff"
            self.start, self.end = row.start_date, row.end_date
        return test_id, round_id, entries

    @staticmethod
    def key(entry):
        return export_text.ad_key(entry["entry_id"])

    def code(self, entry, ctx=None):
        return asset_code((ctx or self.context).workspace_id, entry["asset_version_id"])

    def named_csv(self, names, days=(1, 2)):
        """Meta's daily export with its ad name column: ``names`` maps each Meta ad ID to the name it has."""
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(COLUMNS)
        for number in days:
            for ad, name in names.items():
                writer.writerow([self.day(number), ad, name, 10, 1000, 30, 1, 40])
        return out.getvalue()

    def upload(self, test_id, round_id, names, **kwargs):
        return self.preview(test_id, round_id, self.named_csv(names, **kwargs))

    def suggest(self, test_id, round_id, batch_id, status=200):
        response = self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/bindings/suggest",
                                    json={"batch_id": batch_id})
        self.assertEqual(response.status_code, status, response.text)
        if status == 200:
            MarketingTestBindingSuggestions.model_validate(response.json())
        return response.json()

    def bind(self, test_id, round_id, choices, status=200, *, revision=None, key=None, account=ACCOUNT):
        """Confirm ``choices``, ``{Meta ad ID: entry id, or None to take its match away}``, in one command."""
        body = {"ad_account_id": f"act_{account}",
                "bindings": [{"ad_id": ad, "entry_id": entry} for ad, entry in choices.items()]}
        response = self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/bindings",
                                    headers=self.headers(revision or self.revision(round_id), key), json=body)
        self.assertEqual(response.status_code, status, response.text)
        if status == 200:
            MarketingTestBindings.model_validate(response.json())
            self.assertEqual(response.headers["etag"], f'"{self.revision(round_id)}"')
        return response.json()

    def current(self, round_id, ctx=None):
        """The round's matches now: Meta ad ID -> entry id."""
        ctx = ctx or self.context
        with tenant_session(ctx) as session:
            row = workflow.get_round(session, ctx, round_id)
            return {b.ad_id: b.entry_id for b in workflow.round_bindings(session, ctx, row)}

    def records(self, round_id):
        """Every binding record of the round, oldest first: (Meta ad ID, entry id, removed, replaces another)."""
        with tenant_session(self.context) as session:
            rows = session.scalars(select(Binding).where(Binding.workspace_id == self.context.workspace_id,
                                                         Binding.round_id == round_id)).all()
            return [(b.ad_id, b.entry_id, b.removed, b.replaces_id is not None)
                    for b in sorted(rows, key=lambda b: (b.created_at, b.ad_id, b.id))]

    def events(self, round_id, event):
        with tenant_session(self.context) as session:
            return session.scalars(select(AuditEventRecord.id).where(
                AuditEventRecord.workspace_id == self.context.workspace_id, AuditEventRecord.resource_id == round_id,
                AuditEventRecord.event_type == "marketing." + event)).all()

    def round_row(self, round_id):
        return self.client.get(f"/api/v1/marketing/rounds/{round_id}").json()["data"]


class SuggestionTests(BindingFixtures):
    def test_each_short_key_prefills_the_ad_it_names_and_nothing_is_stored(self):
        test_id, round_id, (first, second) = self.package_round()
        # The package's ad name kept as it is; and an ad named after the downloaded image file.
        report = self.upload(test_id, round_id, {"1001": first["ad_name"], "1002": f"Acme Charge ad-{self.code(second)}"})
        revision = self.revision(round_id)
        suggested = self.suggest(test_id, round_id, report["id"])
        self.assertEqual((suggested["round_id"], suggested["batch_id"], suggested["ad_account_id"],
                          suggested["ad_name_column"], suggested["locked"]),
                         (round_id, report["id"], ACCOUNT, "Ad name", False))
        self.assertEqual([(e["entry_id"], e["asset_code"]) for e in suggested["entries"]],
                         [(first["entry_id"], self.code(first)), (second["entry_id"], self.code(second))])
        self.assertEqual([(a["ad_id"], a["status"], a["entry_id"], a["key_kind"]) for a in suggested["ads"]],
                         [("1001", "suggested", first["entry_id"], "ma_key"),
                          ("1002", "suggested", second["entry_id"], "mk_code")])
        self.assertEqual(suggested["ads"][0]["ad_names"], [first["ad_name"]])
        # Reading the names writes nothing: no match, no round change, no audit event.
        self.assertEqual((self.records(round_id), self.revision(round_id)), ([], revision))
        self.assertEqual(self.events(round_id, "test_bindings_suggested"), [])

    def test_a_renamed_ad_or_a_name_with_two_keys_is_not_prefilled(self):
        test_id, round_id, (first, second) = self.package_round()
        _, _, (elsewhere, _) = self.package_round()
        report = self.upload(test_id, round_id, {
            "1001": "Acme Charge summer promo",                                    # renamed in Meta: no key left
            "1002": f"{self.key(first)} / {self.key(second)}",                     # two package keys
            "1003": f"ad-{self.code(first)} ad-{self.code(second)}",               # two image codes
            "1004": f"{self.key(first)} ad-{self.code(second)}",                  # the package key goes first
            "1005": elsewhere["ad_name"],                                          # another test's ad
            "1006": f"{self.key(first)} - Copy {self.key(first).lower()}",         # one key, twice
        })
        ads = {a["ad_id"]: a for a in self.suggest(test_id, round_id, report["id"])["ads"]}
        self.assertEqual({ad: (a["status"], a["entry_id"], a["key_kind"]) for ad, a in ads.items()}, {
            "1001": ("no_match", None, None), "1002": ("ambiguous", None, "ma_key"),
            "1003": ("ambiguous", None, "mk_code"), "1004": ("suggested", first["entry_id"], "ma_key"),
            "1005": ("no_match", None, "ma_key"), "1006": ("suggested", first["entry_id"], "ma_key")})
        # The user picks among the ads the keys name, or skips.
        self.assertEqual(ads["1002"]["candidate_entry_ids"], [first["entry_id"], second["entry_id"]])
        self.assertEqual(ads["1003"]["candidate_entry_ids"], [first["entry_id"], second["entry_id"]])
        self.assertEqual((ads["1001"]["candidate_entry_ids"], ads["1005"]["candidate_entry_ids"]), ([], []))

    def test_a_meta_ad_already_matched_elsewhere_is_listed_for_the_user_to_choose(self):
        test_id, round_id, (first, second) = self.package_round()
        other_test, other_round, (other_entry, _) = self.package_round()
        self.bind(other_test, other_round, {"1001": other_entry["entry_id"]})
        self.bind(test_id, round_id, {"1002": second["entry_id"]})
        report = self.upload(test_id, round_id, {"1001": first["ad_name"], "1002": first["ad_name"]})
        ads = {a["ad_id"]: a for a in self.suggest(test_id, round_id, report["id"])["ads"]}
        self.assertEqual((ads["1001"]["status"], ads["1001"]["entry_id"], ads["1001"]["other_round_ids"]),
                         ("bound_elsewhere", None, [other_round]))
        self.assertEqual(ads["1001"]["candidate_entry_ids"], [first["entry_id"]])
        # A Meta ad matched in this round keeps its match, whatever its name says now.
        self.assertEqual((ads["1002"]["status"], ads["1002"]["entry_id"]), ("bound", second["entry_id"]))
        # The other round runs on the same days, so it cannot be matched here as well.
        refused = self.bind(test_id, round_id, {"1001": first["entry_id"]}, status=409)
        self.assertEqual(refused["code"], "overlapping_ad_binding")

    def test_a_report_without_ad_names_suggests_nothing(self):
        test_id, round_id, _ = self.package_round()
        report = self.preview(test_id, round_id, self.meta_csv(range(1, 3)).replace("Ad name", "Campaign name"))
        suggested = self.suggest(test_id, round_id, report["id"])
        self.assertIsNone(suggested["ad_name_column"])
        self.assertEqual({(a["status"], a["entry_id"]) for a in suggested["ads"]}, {("no_match", None)})


class ConfirmationTests(BindingFixtures):
    def test_one_confirmation_writes_every_match_and_the_rows_count_from_then_on(self):
        test_id, round_id, (first, second) = self.package_round()
        report = self.upload(test_id, round_id, {"1001": first["ad_name"], "1002": second["ad_name"]})
        self.assertEqual(self.counted(round_id), [])
        confirmed = self.bind(test_id, round_id, {"1001": first["entry_id"], "1002": second["entry_id"]})
        self.assertEqual({(b["ad_id"], b["entry_id"], b["actor_id"]) for b in confirmed["bindings"]},
                         {("1001", first["entry_id"], self.context.initiator_id),
                          ("1002", second["entry_id"], self.context.initiator_id)})
        self.assertEqual((confirmed["ad_account_id"], confirmed["locked"], len(confirmed["history"])), (ACCOUNT, False, 2))
        self.assertEqual(len(self.events(round_id, "test_bindings_confirmed")), 1)
        self.confirm(test_id, round_id, report)
        self.assertEqual({(r["ad_id"], r["entry_id"]) for r in self.counted(round_id)},
                         {("1001", first["entry_id"]), ("1002", second["entry_id"])})
        # The round read shows the same matches.
        self.assertEqual({b["ad_id"] for b in self.round_row(round_id)["bindings"]}, {"1001", "1002"})

    def test_when_one_match_is_refused_none_is_written(self):
        test_id, round_id, (first, second) = self.package_round()
        other_test, other_round, (other_entry, _) = self.package_round()
        self.bind(other_test, other_round, {"1003": other_entry["entry_id"]})
        revision = self.revision(round_id)
        for choices, code, status in (
            ({"1001": first["entry_id"], "1002": "entry_of_another_round"}, "entry_not_found", 422),
            ({"1001": first["entry_id"], "1002": second["entry_id"], "1003": first["entry_id"]},
             "overlapping_ad_binding", 409),
        ):
            self.assertEqual(self.bind(test_id, round_id, choices, status=status)["code"], code)
            self.assertEqual((self.records(round_id), self.revision(round_id)), ([], revision))
        # The same Meta ad twice in one confirmation is refused before anything runs.
        body = {"ad_account_id": ACCOUNT, "bindings": [{"ad_id": "1001", "entry_id": first["entry_id"]},
                                                       {"ad_id": "1001", "entry_id": second["entry_id"]}]}
        response = self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/bindings",
                                    headers=self.headers(revision), json=body)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.records(round_id), [])

    def test_a_round_uses_one_ad_account(self):
        test_id, round_id, (first, second) = self.package_round()
        self.bind(test_id, round_id, {"1001": first["entry_id"]})
        refused = self.bind(test_id, round_id, {"1002": second["entry_id"]}, status=422, account="999")
        self.assertEqual(refused["code"], "account_mismatch")
        self.assertEqual(self.current(round_id), {"1001": first["entry_id"]})

    def test_before_the_first_confirmed_report_a_match_can_be_changed_or_taken_away(self):
        test_id, round_id, (first, second) = self.package_round()
        report = self.upload(test_id, round_id, {"1001": first["ad_name"], "1002": second["ad_name"]})
        self.bind(test_id, round_id, {"1001": first["entry_id"], "1002": second["entry_id"]})
        changed = self.bind(test_id, round_id, {"1001": second["entry_id"], "1002": None})
        self.assertEqual(self.current(round_id), {"1001": second["entry_id"]})
        # Each change is a new record; the earlier ones stay as they were.
        self.assertEqual(self.records(round_id), [
            ("1001", first["entry_id"], False, False), ("1002", second["entry_id"], False, False),
            ("1001", second["entry_id"], False, True), ("1002", second["entry_id"], True, True)])
        self.assertEqual([(h["ad_id"], h["entry_id"], h["current"]) for h in changed["history"]], [
            ("1001", first["entry_id"], False), ("1002", second["entry_id"], False),
            ("1001", second["entry_id"], True), ("1002", None, False)])
        # A Meta ad taken away can be matched again; matching it to what it already shows changes nothing.
        self.bind(test_id, round_id, {"1002": first["entry_id"], "1001": second["entry_id"]})
        self.assertEqual(self.current(round_id), {"1001": second["entry_id"], "1002": first["entry_id"]})
        self.assertEqual(len(self.records(round_id)), 5)
        self.confirm(test_id, round_id, report)
        self.assertEqual({(r["ad_id"], r["entry_id"]) for r in self.counted(round_id)},
                         {("1001", second["entry_id"]), ("1002", first["entry_id"])})

    def test_after_the_first_confirmed_report_matches_can_only_be_added(self):
        test_id, round_id, (first, second) = self.package_round()
        self.bind(test_id, round_id, {"1001": first["entry_id"]})
        self.confirm(test_id, round_id, self.upload(test_id, round_id, {"1001": first["ad_name"]}))
        for choices in ({"1001": second["entry_id"]}, {"1001": None},
                        # One refused change keeps the addition beside it out as well.
                        {"1002": second["entry_id"], "1001": second["entry_id"]}):
            self.assertEqual(self.bind(test_id, round_id, choices, status=409)["code"], "test_bindings_locked")
        self.assertEqual(self.current(round_id), {"1001": first["entry_id"]})
        added = self.bind(test_id, round_id, {"1001": first["entry_id"], "1002": second["entry_id"]})
        self.assertTrue(added["locked"])
        self.assertEqual(self.current(round_id), {"1001": first["entry_id"], "1002": second["entry_id"]})
        self.assertEqual(self.suggest(test_id, round_id, self.upload(test_id, round_id, {"1002": "x"}, days=(1, 2, 3))["id"])["locked"], True)

    def test_the_same_confirmation_is_answered_once(self):
        test_id, round_id, (first, second) = self.package_round()
        key, revision = new_id("key"), self.revision(round_id)
        once = self.bind(test_id, round_id, {"1001": first["entry_id"]}, revision=revision, key=key)
        again = self.bind(test_id, round_id, {"1001": first["entry_id"]}, revision=revision, key=key)
        self.assertEqual(once, again)
        self.assertEqual(len(self.records(round_id)), 1)
        # Stale revision: the page reloads before confirming again.
        refused = self.bind(test_id, round_id, {"1002": second["entry_id"]}, status=409, revision=revision)
        self.assertEqual(refused["code"], "stale_round_revision")

    def test_matches_wait_for_the_ad_package(self):
        test_id, round_id = self.make_test()
        refused = self.bind(test_id, round_id, {"1001": "entry_1"}, status=409)
        self.assertEqual(refused["code"], "handoff_not_ready")
        self.assertEqual(self.records(round_id), [])

    def test_a_round_that_has_ended_still_takes_a_new_meta_ad(self):
        test_id, round_id, (first, _) = self.package_round()
        self.close(round_id)
        self.bind(test_id, round_id, {"1001": first["entry_id"]})
        self.assertEqual(self.round_row(round_id)["state"], "closed")
        self.assertEqual(self.current(round_id), {"1001": first["entry_id"]})


class BindingScopeTests(BindingFixtures):
    def test_another_workspace_can_neither_read_names_nor_confirm(self):
        test_id, round_id, (first, _) = self.package_round()
        report = self.upload(test_id, round_id, {"1001": first["ad_name"]})
        other = self._make_context()
        self.use(other)
        try:
            refused = self.suggest(test_id, round_id, report["id"], status=404)
            self.assertEqual(refused["code"], "marketing_test_not_found")
            response = self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/bindings",
                                        headers=self.headers(1), json={"ad_account_id": ACCOUNT,
                                        "bindings": [{"ad_id": "1001", "entry_id": first["entry_id"]}]})
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_test_not_found"))
        finally:
            self.use(self.context)
        self.assertEqual(self.records(round_id), [])

    def test_a_round_and_a_report_are_reached_only_through_their_own_test(self):
        test_id, round_id, (first, _) = self.package_round()
        other_test, other_round, (other_entry, _) = self.package_round()
        other_report = self.upload(other_test, other_round, {"1001": other_entry["ad_name"]})
        self.assertEqual(self.suggest(test_id, other_round, other_report["id"], status=404)["code"], "round_not_found")
        self.assertEqual(self.suggest(test_id, round_id, other_report["id"], status=404)["code"], "batch_not_found")
        refused = self.bind(test_id, other_round, {"1001": other_entry["entry_id"]}, status=404,
                            revision=self.revision(other_round))
        self.assertEqual(refused["code"], "round_not_found")
        # Another round's ad is not one of this round's to match.
        self.assertEqual(self.bind(test_id, round_id, {"1001": other_entry["entry_id"]}, status=422)["code"],
                         "entry_not_found")
        self.assertEqual((self.records(round_id), self.records(other_round)), ([], []))

    def test_a_workspace_without_current_authorization_cannot_confirm(self):
        test_id, round_id, (first, _) = self.package_round()
        # Confirming needs the If-Match revision.
        response = self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/bindings",
                                    headers=self.headers(), json={"ad_account_id": ACCOUNT,
                                    "bindings": [{"ad_id": "1001", "entry_id": first["entry_id"]}]})
        self.assertEqual(response.status_code, 428, response.text)
        with platform_session() as session:
            session.get(WorkspaceRecord, self.context.workspace_id).lifecycle = "suspended"
        try:
            self.assertEqual(self.bind(test_id, round_id, {"1001": first["entry_id"]}, status=403)["code"],
                             "workspace_inactive")
        finally:
            with platform_session() as session:
                session.get(WorkspaceRecord, self.context.workspace_id).lifecycle = "active"
        self.assertEqual(self.records(round_id), [])
        # A Member and the Owner both can.
        self.use(self.owner_ctx(self.context))
        try:
            confirmed = self.bind(test_id, round_id, {"1001": first["entry_id"]})
        finally:
            self.use(self.context)
        self.assertEqual([b["actor_id"] for b in confirmed["bindings"]], [self.owner_ctx(self.context).initiator_id])


class PlanFirstBindingTests(BindingFixtures):
    def test_plan_first_rounds_keep_their_binding_rules(self):
        project, row = self.bound()
        self.assertEqual(len(row["bindings"]), len(row["handoff"]["entries"]))
        # The same Meta ad on the same days is refused as before; a test's paths do not find the round.
        entry = row["handoff"]["entries"][0]["entry_id"]
        refused = self.command(row, "bindings", {"entry_id": entry, "ad_account_id": "act_123", "ad_id": "1000",
                                                 "conditions_match": True}, 409)
        self.assertEqual(refused["code"], "overlapping_ad_binding")
        response = self.client.post(f"/api/v1/marketing/tests/{project['id']}/rounds/{row['id']}/bindings/suggest",
                                    json={"batch_id": "mmb_x"})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_test_not_found"))
        with tenant_session(self.context) as session:
            records = session.scalars(select(Binding).where(Binding.workspace_id == self.context.workspace_id,
                                                            Binding.round_id == row["id"])).all()
            self.assertEqual({(b.replaces_id, b.removed) for b in records}, {(None, False)})

    def test_a_test_round_is_matched_only_on_the_test_paths(self):
        test_id, round_id, (first, _) = self.package_round()
        legacy = self.client.post(f"/api/v1/marketing/rounds/{round_id}/bindings", headers=self.headers(self.revision(round_id)),
                                  json={"entry_id": first["entry_id"], "ad_account_id": ACCOUNT, "ad_id": "1001",
                                        "conditions_match": True})
        self.assertEqual((legacy.status_code, legacy.json()["code"]), (409, "test_bindings_required"))
        self.assertEqual(self.records(round_id), [])

    def test_a_meta_ad_taken_away_from_a_test_round_can_go_to_an_earlier_test_round(self):
        test_id, round_id, (first, _) = self.package_round()
        self.bind(test_id, round_id, {"4242": first["entry_id"]})
        self.bind(test_id, round_id, {"4242": None})
        project, row = self.ready_handoff()
        with tenant_session(self.context) as session:
            # The earlier test's round on the test round's days, so only the current match can be in the way.
            plan_first = workflow.get_round(session, self.context, row["id"])
            plan_first.start_date, plan_first.end_date = self.start, self.end
        row = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()["data"]
        row = self.command(row, "bindings", {"entry_id": row["handoff"]["entries"][0]["entry_id"],
                                             "ad_account_id": ACCOUNT, "ad_id": "4242", "conditions_match": True}, 201)
        self.assertEqual([b["ad_id"] for b in row["bindings"]], ["4242"])


class BindingHistoryConstraintTests(BindingFixtures):
    def test_the_database_keeps_one_line_of_records_per_meta_ad(self):
        test_id, round_id, (first, second) = self.package_round()
        self.bind(test_id, round_id, {"1001": first["entry_id"]})
        with tenant_session(self.context) as session:
            root = workflow.scoped(session, self.context, Binding, round_id=round_id)[0]
            fields = {c: getattr(root, c) for c in ("workspace_id", "round_id", "handoff_id", "ad_account_id",
                                                     "ad_id", "start_date", "end_date", "conditions_json")}
        for replaces in (None, root.id):
            with self.subTest(replaces=replaces):
                with self.assertRaises(IntegrityError):
                    with tenant_session(self.context) as session:
                        # A second first record of the same Meta ad, or a second change of the same record.
                        if replaces:
                            session.add(Binding(id=new_id("mab"), **fields, entry_id=second["entry_id"], replaces_id=replaces))
                            session.flush()
                        session.add(Binding(id=new_id("mab"), **fields, entry_id=second["entry_id"], replaces_id=replaces))
                        session.flush()
        self.assertEqual(self.current(round_id), {"1001": first["entry_id"]})


class BindingMigrationDowngradeTests(unittest.TestCase):
    """Dropping the change records would put changed matches back, so downgrade refuses while one exists."""
    ROOT = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-binding-history-migration-")
        self.addCleanup(self.temp.cleanup)
        self.database = f"{self.temp.name}/migration.db"
        self.environment = {**os.environ, "MARKETAI_DATABASE_URL": f"sqlite:///{self.database}",
                            "MARKETAI_DATA_DIR": f"{self.temp.name}/data"}
        upgraded = self.alembic("upgrade", "head")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

    def alembic(self, *arguments):
        return subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=self.ROOT, env=self.environment,
                              capture_output=True, text=True, check=False)

    def columns(self):
        with sqlite3.connect(self.database) as connection:
            return {row[1] for row in connection.execute("PRAGMA table_info(marketing_ad_bindings)")}

    def insert(self, connection, binding_id, replaces_id=None):
        connection.execute("INSERT INTO marketing_ad_bindings (id, workspace_id, round_id, handoff_id, entry_id,"
                           " ad_account_id, ad_id, start_date, end_date, conditions_json, created_at, replaces_id, removed)"
                           " VALUES (?, 'ws_1', 'mround_1', 'mh_1', 'entry_1', '123', '1001', '2026-11-23', '2026-11-29',"
                           " '{}', '2026-11-22', ?, 0)", (binding_id, replaces_id))

    def test_downgrade_without_changes_restores_one_binding_per_meta_ad(self):
        downgraded = self.alembic("downgrade", "zzu253a1b2c3")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        self.assertFalse({"replaces_id", "removed", "actor_id"} & self.columns())
        with sqlite3.connect(self.database) as connection:
            values = ("'ws_1', 'mround_1', 'mh_1', 'entry_1', '123', '1001', '2026-11-23', '2026-11-29', '{}',"
                      " '2026-11-22'")
            columns = ("id, workspace_id, round_id, handoff_id, entry_id, ad_account_id, ad_id, start_date, end_date,"
                       " conditions_json, created_at")
            connection.execute(f"INSERT INTO marketing_ad_bindings ({columns}) VALUES ('mab_1', {values})")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(f"INSERT INTO marketing_ad_bindings ({columns}) VALUES ('mab_2', {values})")

    def test_downgrade_refuses_while_a_change_exists(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            self.insert(connection, "mab_1")
            self.insert(connection, "mab_2", replaces_id="mab_1")
        downgraded = self.alembic("downgrade", "zzu253a1b2c3")
        self.assertNotEqual(downgraded.returncode, 0)
        self.assertIn("binding changes exist", downgraded.stderr)
        self.assertIn("replaces_id", self.columns())


class ScaleReadsCurrentMatchesTests(BindingFixtures):
    def test_scale_maps_a_meta_ad_by_its_current_match_only(self):
        from types import SimpleNamespace
        from app.scale.observed_ads import _marketing_ad_bindings
        test_id, round_id, (first, second) = self.package_round()
        self.bind(test_id, round_id, {"1001": first["entry_id"], "1002": second["entry_id"]})
        self.bind(test_id, round_id, {"1001": second["entry_id"], "1002": None})
        with tenant_session(self.context) as session:
            for ad, expected in (("1001", [second["asset_version_id"]]), ("1002", [])):
                observed = SimpleNamespace(ad_account_id=ACCOUNT, ad_remote_id=ad)
                found = list(_marketing_ad_bindings(session, self.context, observed))
                self.assertEqual([b.asset_version_id for b in found], expected, ad)
