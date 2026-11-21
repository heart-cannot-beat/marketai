"""Test history of a product (ADR 0019, step 2 design §5.10, #255).

``GET /marketing/tests?product_id=`` lists the product's test projects with each round and each report upload
and correction -- when (the batch's ``created_at``) and by whom (the audit event that recorded it) -- and the
tests made the earlier way (plan first) that stand for the product through its card lineage
(``app.marketing.card_lineage``). Test projects are built directly, as in ``tests/test_marketing_tests.py``;
their report batches are written the way a confirmed upload stores them until the test-report commands (#251)
exist. The earlier test runs the ordinary plan-first loop through HTTP, uploads included.
"""
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import select

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, ProductRecord, new_id
from app.main import app
from app.marketing import ad_test_schedule, ad_tests, ideas, service, workflow
from app.marketing.ad_tests_contracts import MarketingTestHistory, MoveMarketingTestStart
from app.marketing.models import MarketingProjectRecord
from app.marketing.workflow_models import MarketingMetricBatchRecord
from app.platform.http_identity import request_tenant_context
from tests.test_marketing_tests import SOURCE, MarketingTestFixtures


class MarketingTestHistoryFixtures(MarketingTestFixtures):
    #: The product card the next plan-first test starts from (``None``: pasted text only).
    card = None

    def create(self):
        response = self.client.post("/api/v1/marketing/projects?include_languages=true&include_plan_review=true"
                                    "&include_brand_style=true&include_execution_plan=true", headers=self.headers(),
                                    json={"source_text": SOURCE, **({"product_id": self.card} if self.card else {})})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def history(self, product_id, status=200):
        response = self.client.get("/api/v1/marketing/tests", params={"product_id": product_id})
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def ideas_of(self, product_id):
        with tenant_session(self.context) as session:
            return ideas.create_ideas_project(session, self.context, product_id=product_id, source_text=SOURCE,
                                              market="United States", ad_language="en-US").id

    def report(self, round_id, ctx, *, replaces=None, days=3):
        """A confirmed report upload of a test's round, stored like a confirmed batch, its event by ``ctx``'s person."""
        with tenant_session(ctx) as session:
            row = workflow.get_round(session, ctx, round_id)
            batch = MarketingMetricBatchRecord(id=new_id("mmb"), workspace_id=ctx.workspace_id, round_id=round_id,
                replaces_id=replaces, request_hash=new_id("req"), raw_hash="0" * 64, raw_csv="date,ad_id\n",
                metadata_json={"source_mode": "customer_file"}, issues_json=[], state="confirmed",
                rows_json=[{"date": f"2030-01-{day:02d}", "ad_id": "1001"} for day in range(1, days + 1)])
            session.add(batch)
            session.flush()
            workflow.audit(session, row, "metrics_confirmed", {"batch_id": batch.id, "replaces_id": replaces})
            return batch.id

    def earlier_test(self, product_id):
        """A plan-first test started from ``product_id``'s card, run to two confirmed uploads, the second a correction.

        A Member uploads the first report and the Owner confirms it; the Owner uploads and confirms the correction.
        A third upload whose checks fail is never confirmed."""
        member, owner = self.context, self.owner_ctx(self.context)
        self.card = product_id
        try:
            project, row = self.bound()
        finally:
            self.card = None
        source = self.csv(row)
        row = self.command(row, "metrics/preview", source, 201)
        first = row["batches"][-1]["id"]
        self.context = owner
        try:
            row = self.command(row, "metrics/confirm", {"batch_id": first})
            doubled = "\n".join(",".join([*line.split(",")[:2], str(int(line.split(",")[2]) * 2), *line.split(",")[3:]])
                                if line and line[0].isdigit() else line for line in source["csv_text"].split("\n"))
            row = self.command(row, "metrics/preview", self.csv(row, csv_text=doubled, replaces_batch_id=first), 201)
            second = row["batches"][-1]["id"]
            row = self.command(row, "metrics/confirm", {"batch_id": second})
        finally:
            self.context = member
        row = self.command(row, "metrics/preview", self.csv(row, source_mode="customer_file"), 201)
        self.assertTrue(row["batches"][-1]["issues"])  # previewed, never confirmed: not part of the history
        return project["id"], row, (first, second)

    def batch_times(self, *batch_ids):
        with tenant_session(self.context) as session:
            return [session.get(MarketingMetricBatchRecord, batch_id).created_at.isoformat() for batch_id in batch_ids]


class ProductTestHistoryTests(MarketingTestHistoryFixtures):
    def test_a_product_lists_its_tests_with_rounds_uploads_and_corrections_and_its_earlier_tests(self):
        product = self.product()
        earlier_id, earlier_round, (first, second) = self.earlier_test(product)
        ideas_id = self.ideas_of(product)
        older_test, older_round = self.make_test(source=ideas_id)
        self.close(older_round)
        test_id, round_id = self.make_test(source=ideas_id)
        owner = self.owner_ctx(self.context)
        member = self.context
        upload = self.report(round_id, member)
        correction = self.report(round_id, owner, replaces=upload, days=2)
        with tenant_session(self.context) as session:
            frozen = workflow.get_round(session, self.context, round_id)
            closed = workflow.get_round(session, self.context, older_round)
            earlier = session.get(MarketingProjectRecord, earlier_id)
            # The earlier test confirmed its understanding into a product anchor of its own; the card lineage
            # still ties it to the product it started from.
            self.assertNotEqual(earlier.product_id, product)
            test_row = session.get(MarketingProjectRecord, test_id)
            expected_test = ad_tests.detail(session, self.context, test_row)
            expected_older = ad_tests.detail(session, self.context, session.get(MarketingProjectRecord, older_test))
            earlier_name = earlier.understanding_json["product_name"]
            earlier_created = earlier.created_at.isoformat()
        first_at, second_at, uploaded_at, corrected_at = self.batch_times(first, second, upload, correction)

        for ctx in (self.context, owner):  # a Member and the Owner both read it
            self.use(ctx)
            body = self.history(product)
            MarketingTestHistory.model_validate(body)
            self.assertEqual(body["product_id"], product)
            # Newest test first; each keeps the fields of its detail, and each round lists its confirmed uploads.
            self.assertEqual([test["id"] for test in body["tests"]], [test_id, older_test])
            newest = body["tests"][0]
            self.assertEqual({k: newest[k] for k in ("id", "source_project_id", "comparison", "plan_summary",
                                                     "current_round_id", "created_at")},
                             {k: expected_test[k] for k in ("id", "source_project_id", "comparison", "plan_summary",
                                                            "current_round_id", "created_at")})
            self.assertEqual(newest["rounds"], [{
                **expected_test["rounds"][0],
                "start_date": str(frozen.start_date), "end_date": str(frozen.end_date),
                "effective_start_date": str(frozen.start_date), "effective_end_date": str(frozen.end_date),
                "reports": [
                    {"batch_id": upload, "kind": "upload", "replaces_batch_id": None, "uploaded_at": uploaded_at,
                     "uploaded_by": member.initiator_id, "row_count": 3},
                    {"batch_id": correction, "kind": "correction", "replaces_batch_id": upload,
                     "uploaded_at": corrected_at, "uploaded_by": owner.initiator_id, "row_count": 2},
                ]}])
            self.assertEqual(body["tests"][1]["current_round_id"], None)
            self.assertEqual(body["tests"][1]["rounds"], [{
                **expected_older["rounds"][0], "start_date": str(closed.start_date), "end_date": str(closed.end_date),
                "effective_start_date": str(closed.start_date), "effective_end_date": str(closed.end_date),
                "reports": []}])

            # The earlier test, found through the card it started from, with the uploads of its round: the Member
            # uploaded the first report (the Owner only confirmed it), the Owner uploaded the correction.
            self.assertEqual(body["earlier_tests"], [{
                "id": earlier_id, "state": "plan_confirmed", "product_name": earlier_name, "created_at": earlier_created,
                "rounds": [{
                    "id": earlier_round["id"], "ordinal": 1, "state": "awaiting_data",
                    "start_date": "2025-01-01", "end_date": "2025-01-07",
                    "effective_start_date": "2025-01-01", "effective_end_date": "2025-01-07",
                    "reports": [
                        {"batch_id": first, "kind": "upload", "replaces_batch_id": None, "uploaded_at": first_at,
                         "uploaded_by": member.initiator_id, "row_count": 35},
                        {"batch_id": second, "kind": "correction", "replaces_batch_id": first, "uploaded_at": second_at,
                         "uploaded_by": owner.initiator_id, "row_count": 35},
                    ]}]}])

    def test_another_products_tests_are_not_listed(self):
        product, elsewhere = self.product(), self.product()
        mine, _ = self.make_test(source=self.ideas_of(product))
        theirs, _ = self.make_test(source=self.ideas_of(elsewhere))
        self.card = elsewhere
        try:
            other_earlier = self.create()
        finally:
            self.card = None
        # An earlier test from this card whose source text was then revised no longer stands for the card.
        self.card = product
        try:
            revised = self.create()
        finally:
            self.card = None
        response = self.edit(revised, "revise_source", source_text=SOURCE + "\nNow in white.")
        self.assertEqual(response.status_code, 200, response.text)
        # And a test made from pasted text only stands for no product card.
        pasted, _, _ = self.confirmed_plan()

        body = self.history(product)
        self.assertEqual(([t["id"] for t in body["tests"]], body["earlier_tests"]), ([mine], []))
        body = self.history(elsewhere)
        self.assertEqual([t["id"] for t in body["tests"]], [theirs])
        self.assertEqual([t["id"] for t in body["earlier_tests"]], [other_earlier["id"]])
        self.assertEqual((body["earlier_tests"][0]["state"], body["earlier_tests"][0]["rounds"]), ("input", []))
        for listed in (self.history(product), self.history(elsewhere)):
            self.assertNotIn(pasted, [t["id"] for t in listed["earlier_tests"]])
            self.assertNotIn(revised["id"], [t["id"] for t in listed["earlier_tests"]])

    def test_a_pasted_text_test_is_listed_under_the_product_it_confirmed(self):
        # A plan-first test that never named a card is its own product on the product page: its confirmed anchor.
        pasted, _, _ = self.confirmed_plan()
        with tenant_session(self.context) as session:
            anchor = session.get(MarketingProjectRecord, pasted).product_id
        body = self.history(anchor)
        self.assertEqual(([t["id"] for t in body["earlier_tests"]], body["tests"]), ([pasted], []))

    def test_a_bridged_product_is_read_by_either_id_with_tests_that_recorded_its_library_entry(self):
        # A product bridged to its Library entry; a test from before the bridge recorded the entry id as its card,
        # on a compatibility product row of that id (``card_lineage.card_ids``).
        entry = new_id("le")
        with tenant_session(self.context) as session:
            product = ProductRecord(id=new_id("prod"), workspace_id=self.context.workspace_id, name="Acme Charge",
                                    product_profile_entry_id=entry)
            session.add_all([product, ProductRecord(id=entry, workspace_id=self.context.workspace_id, name="Acme Charge")])
            session.flush()
            product = product.id
            before_bridge = MarketingProjectRecord(id=new_id("mp"), workspace_id=self.context.workspace_id,
                product_id=entry, source_text=SOURCE, revision=1, state="input")
            session.add(before_bridge)
            session.flush()
            service.checkpoint(session, self.context, before_bridge, "created")
            before_bridge = before_bridge.id
        test_id, _ = self.make_test(source=self.ideas_of(product))
        for asked in (product, entry):
            body = self.history(asked)
            self.assertEqual((body["product_id"], [t["id"] for t in body["tests"]], [t["id"] for t in body["earlier_tests"]]),
                             (product, [test_id], [before_bridge]), asked)

    def test_a_moved_start_shows_in_the_history_with_the_frozen_dates_kept(self):
        # A test round whose ads went live two days late (#253): the history shows the effective dates the
        # evaluation uses, next to the frozen ones.
        product = self.product()
        test_id, round_id = self.make_test(source=self.ideas_of(product))
        with tenant_session(self.context) as session:
            row = workflow.get_round(session, self.context, round_id)
            frozen = (row.start_date, row.end_date)
            ad_test_schedule.move_start(session, self.context, row, MoveMarketingTestStart(
                start_date=row.start_date + timedelta(days=2), reason="The ads went live late"))
        (listed,) = self.history(product)["tests"][0]["rounds"]
        self.assertEqual((listed["start_date"], listed["end_date"]), tuple(str(day) for day in frozen))
        self.assertEqual((listed["effective_start_date"], listed["effective_end_date"]),
                         tuple(str(day + timedelta(days=2)) for day in frozen))
        detail_round = self.client.get(f"/api/v1/marketing/tests/{test_id}").json()["rounds"][0]
        self.assertEqual({k: v for k, v in listed.items() if k != "reports"}, detail_round)

    def test_a_product_without_tests_reads_an_empty_history(self):
        product = self.product()
        self.ideas_of(product)  # ads-first ideas are not tests
        self.assertEqual(self.history(product), {"product_id": product, "tests": [], "earlier_tests": []})
        with tenant_session(self.context) as session:
            self.assertEqual(ad_tests.history(session, self.context, product),
                             {"product_id": product, "tests": [], "earlier_tests": []})

    def test_another_workspace_gets_not_found_and_reads_nothing(self):
        product = self.product()
        test_id, round_id = self.make_test(source=self.ideas_of(product))
        self.report(round_id, self.context)
        self.earlier_test(product)
        other = self._make_context()
        self.use(other)
        missing = self.history(new_id("prod"), 404)
        foreign = self.history(product, 404)
        # Indistinguishable from a product id that never existed: nothing about the product or its tests leaks.
        self.assertEqual({k: v for k, v in foreign.items() if k != "trace_id"},
                         {k: v for k, v in missing.items() if k != "trace_id"})
        self.assertEqual(foreign["code"], "product_not_found")
        with tenant_session(other) as session:
            with self.assertRaises(service.MarketingError) as caught:
                ad_tests.history(session, other, product)
            self.assertEqual((caught.exception.code, caught.exception.status), ("product_not_found", 404))
        # Its own Workspace still reads both.
        self.use(self.context)
        body = self.history(product)
        self.assertEqual(([t["id"] for t in body["tests"]], len(body["earlier_tests"])), ([test_id], 1))

    def test_a_request_without_a_verified_identity_is_refused(self):
        product = self.product()
        self.make_test(source=self.ideas_of(product))
        app.dependency_overrides.pop(request_tenant_context)
        try:
            with patch.object(config, "LOCAL_IDENTITY_ENABLED", False):
                response = self.client.get("/api/v1/marketing/tests", params={"product_id": product})
        finally:
            app.dependency_overrides[request_tenant_context] = lambda: self.context
        self.assertEqual((response.status_code, response.json()["code"]), (401, "delegated_identity_required"))

    def test_the_product_id_is_required(self):
        response = self.client.get("/api/v1/marketing/tests")
        self.assertEqual(response.status_code, 422, response.text)

    def test_reading_the_history_writes_nothing(self):
        product = self.product()
        self.make_test(source=self.ideas_of(product))
        with tenant_session(self.context) as session:
            before = len(session.scalars(select(AuditEventRecord.id).where(
                AuditEventRecord.workspace_id == self.context.workspace_id)).all())
        self.history(product)
        with tenant_session(self.context) as session:
            after = len(session.scalars(select(AuditEventRecord.id).where(
                AuditEventRecord.workspace_id == self.context.workspace_id)).all())
        self.assertEqual(after, before)
