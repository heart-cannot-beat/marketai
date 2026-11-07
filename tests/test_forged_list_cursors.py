"""Forged list cursors are ``400 invalid_cursor``, never a 500 or a wrong page (#29).

``decode_cursor`` already rejects a cursor that is not base64 JSON, is not a
cursor object, or was minted for another Workspace or filter set. Each case
below instead keeps the real cursor's Workspace and filter binding and only
rewrites its sort position, so the route's own shape check is what answers:

* offset cursors (audit, Library, Meta, Localization, Generation lists and the
  competitor-scan observations) must carry exactly one non-negative integer;
* the asset gallery's keyset cursor must carry an ISO timestamp and a version
  id string of 1 to 64 characters.

Every module also pages forward with the real ``next_cursor`` and rejects that
cursor when it is replayed from another Workspace or under other filters.
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import timedelta

# Import order matters: tests.support.library_fixtures points the database and
# data dir at a per-process temp dir before app.config is imported.
from tests.support.library_fixtures import LibraryFixtures

from fastapi.testclient import TestClient

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import utcnow
from app.library.contracts import ENTRY_PRODUCT
from app.main import app
from app.platform.audit import record_event
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from tests import test_asset_gallery_page as gallery_tests
from tests import test_competitor_scans_api as scan_tests
from tests import test_meta_api_routes as meta_tests
from tests import test_v1_localization_routes as localization_tests


def _encode(payload) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _with_sort(cursor: str, sort: list) -> str:
    """``cursor`` with its sort position replaced; Workspace and filters kept."""
    payload = json.loads(base64.urlsafe_b64decode(cursor))
    return _encode({**payload, "s": sort})


def _shape_cases(cursor: str) -> list[tuple[str, str]]:
    return [
        ("not base64 JSON", "abc"),
        ("not a cursor object", _encode(1)),
        ("empty sort position", _with_sort(cursor, [])),
    ]


def offset_cases(cursor: str) -> list[tuple[str, str]]:
    return _shape_cases(cursor) + [
        (label, _with_sort(cursor, sort))
        for label, sort in (
            ("offset is a string", ["x"]),
            ("offset is a numeric string", ["1"]),
            ("negative offset", [-1]),
            ("fractional offset", [1.5]),
            ("boolean offset", [True]),
            ("null offset", [None]),
            ("two offsets", [1, 1]),
        )
    ]


def keyset_cases(cursor: str) -> list[tuple[str, str]]:
    stamp, version_id = json.loads(base64.urlsafe_b64decode(cursor))["s"]
    return _shape_cases(cursor) + [
        (label, _with_sort(cursor, sort))
        for label, sort in (
            ("only one element", [stamp]),
            ("three elements", [stamp, version_id, "x"]),
            ("time is not ISO", ["yesterday", version_id]),
            ("time is not a string", [1767225600, version_id]),
            ("id is not a string", [stamp, 5]),
            ("id is empty", [stamp, ""]),
            ("id is over 64 characters", [stamp, "v" * 65]),
            ("elements swapped", [version_id, stamp]),
        )
    ]


class ForgedCursorChecks:
    """Assertions shared by every module below (mixed into a TestCase)."""

    def assert_invalid_cursor(self, response, reference=None) -> dict:
        self.assertEqual(response.status_code, 400, response.text)
        body = response.json()
        self.assertEqual(body["code"], "invalid_cursor", body)
        if reference is not None:
            # The route's existing 400 body: same fields as a cursor that
            # never decoded at all.
            self.assertEqual(set(body), set(reference), body)
        return body

    def assert_forged_cursors_rejected(self, get, cases) -> None:
        reference = self.assert_invalid_cursor(get("abc"))
        for label, forged in cases:
            with self.subTest(label):
                self.assert_invalid_cursor(get(forged), reference)

    def assert_second_page(self, get, *, first_ids, second_ids, key: str = "id") -> str:
        """Pages forward with the real ``next_cursor``; returns that cursor."""
        first = get(None)
        self.assertEqual(first.status_code, 200, first.text)
        cursor = first.json()["next_cursor"]
        self.assertIsNotNone(cursor)
        self.assertEqual([item[key] for item in first.json()["items"]], first_ids)
        second = get(cursor)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual([item[key] for item in second.json()["items"]], second_ids)
        return cursor


# ---------------------------------------------------------------------------
# Meta (app/meta/api.py)
# ---------------------------------------------------------------------------


class MetaBindingCursorTests(ForgedCursorChecks, meta_tests.MetaApiRoutesTestCase):
    def _other_workspace(self) -> TenantContext:
        other = TenantContext(
            workspace_id=f"{self.ctx.workspace_id}_other", role="owner", initiator_id="usr_meta_api",
        )
        meta_tests._ensure_workspace(other.workspace_id)
        app.dependency_overrides[request_tenant_context] = lambda: other
        return other

    def _seed(self) -> tuple[str, list[str]]:
        connection = self._create_connection("act_cursor_meta")
        old = self._create_binding(connection["id"], "act_cursor_meta")
        resp = self.client.post(
            f"/api/v1/meta/connections/{connection['id']}/bindings/{old['id']}/supersede",
            json={"ad_account_id": "act_cursor_meta", "meta_mode": meta_tests.META_MODE},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return connection["id"], [old["id"], resp.json()["id"]]

    def _bindings(self, connection_id: str):
        def get(cursor: str | None):
            params = {"meta_mode": meta_tests.META_MODE, "limit": 1, **({"cursor": cursor} if cursor else {})}
            return self.client.get(f"/api/v1/meta/connections/{connection_id}/bindings", params=params)
        return get

    def test_meta_bindings_page_and_reject_forged_cursors(self):
        connection_id, ids = self._seed()
        get = self._bindings(connection_id)
        cursor = self.assert_second_page(get, first_ids=ids[:1], second_ids=ids[1:])
        self.assert_forged_cursors_rejected(get, offset_cases(cursor))

    def test_meta_binding_cursor_is_bound_to_workspace_and_filters(self):
        connection_id, _ = self._seed()
        cursor = self._bindings(connection_id)(None).json()["next_cursor"]
        self.assert_invalid_cursor(self._bindings("mcon_other")(cursor))
        self._other_workspace()
        # Same connection id, so only the Workspace differs.
        self.assert_invalid_cursor(self._bindings(connection_id)(cursor))


# ---------------------------------------------------------------------------
# Audit (app/main.py), Library, Localization, Generation lists and the asset gallery
# ---------------------------------------------------------------------------


class TenantClientFixtures(ForgedCursorChecks, LibraryFixtures):
    role = "member"

    def setUp(self):
        self.ctx = self._make_context(role=self.role)
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _other_workspace(self) -> TenantContext:
        other = self._make_context(role=self.role)
        app.dependency_overrides[request_tenant_context] = lambda: other
        return other


class AuditCursorTests(TenantClientFixtures):
    def _audit(self, cursor: str | None, **params):
        params = {"limit": 1, **params, **({"cursor": cursor} if cursor else {})}
        return self.client.get("/api/v1/audit", params=params)

    def _seed(self) -> list[str]:
        for n in range(2):
            record_event(self.ctx, event_type="cursor.seeded", resource_type="cursor_test", resource_id=f"r{n}")
        return [item["id"] for item in self.client.get("/api/v1/audit").json()["items"]]

    def test_audit_pages_and_rejects_forged_cursors(self):
        ids = self._seed()
        cursor = self.assert_second_page(self._audit, first_ids=ids[:1], second_ids=ids[1:2])
        self.assert_forged_cursors_rejected(self._audit, offset_cases(cursor))

    def test_audit_cursor_is_bound_to_workspace_and_filters(self):
        self._seed()
        cursor = self._audit(None).json()["next_cursor"]
        self.assert_invalid_cursor(self._audit(cursor, event_type="cursor.seeded"))
        self._other_workspace()
        self.assert_invalid_cursor(self._audit(cursor))


class LibraryCursorTests(TenantClientFixtures):
    def _entries(self, cursor: str | None, **params):
        params = {"limit": 2, **params, **({"cursor": cursor} if cursor else {})}
        return self.client.get("/api/v1/library/entries", params=params)

    def _seed(self) -> list[str]:
        for i in range(3):
            resp = self.client.post(
                "/api/v1/library/entries",
                json={"entry_type": ENTRY_PRODUCT, "display_name": f"Acme Charge {i}", "content": {}},
                headers={"Idempotency-Key": f"ik-cursor-{i}", "X-Command-Identity": f"command.cursor.{i}"},
            )
            self.assertEqual(resp.status_code, 201, resp.text)
        return [item["id"] for item in self.client.get("/api/v1/library/entries").json()["items"]]

    def test_library_entries_page_and_reject_forged_cursors(self):
        ids = self._seed()
        cursor = self.assert_second_page(self._entries, first_ids=ids[:2], second_ids=ids[2:])
        self.assert_forged_cursors_rejected(self._entries, offset_cases(cursor))

    def test_library_cursor_is_bound_to_workspace_and_filters(self):
        self._seed()
        cursor = self._entries(None).json()["next_cursor"]
        self.assert_invalid_cursor(self._entries(cursor, entry_type=ENTRY_PRODUCT))
        self._other_workspace()
        self.assert_invalid_cursor(self._entries(cursor))


class LocalizationCursorTests(TenantClientFixtures):
    role = "owner"
    # The winner chain a batch draft needs, borrowed rather than inherited so
    # this class does not re-run that module's tests.
    _winner = localization_tests.TestLocalizationV1Routes._winner
    _market = localization_tests.TestLocalizationV1Routes._market
    _draft_body = localization_tests.TestLocalizationV1Routes._draft_body

    def _batches(self, cursor: str | None, **params):
        params = {"limit": 1, **params, **({"cursor": cursor} if cursor else {})}
        return self.client.get("/api/v1/localization/batches", params=params)

    def _seed(self) -> list[str]:
        for tag in (f"cursor-{uuid.uuid4().hex[:8]}" for _ in range(2)):
            resp = self.client.post(
                "/api/v1/localization/batches", json=self._draft_body(tag),
                headers={"Idempotency-Key": f"ik-{tag}"},
            )
            self.assertEqual(resp.status_code, 201, resp.text)
        return [item["id"] for item in self.client.get("/api/v1/localization/batches").json()["items"]]

    def test_localization_batches_page_and_reject_forged_cursors(self):
        ids = self._seed()
        cursor = self.assert_second_page(self._batches, first_ids=ids[:1], second_ids=ids[1:])
        self.assert_forged_cursors_rejected(self._batches, offset_cases(cursor))

    def test_localization_cursor_is_bound_to_workspace_and_filters(self):
        self._seed()
        cursor = self._batches(None).json()["next_cursor"]
        self.assert_invalid_cursor(self._batches(cursor, phase="draft"))
        self._other_workspace()
        self.assert_invalid_cursor(self._batches(cursor))


class GenerationRunCursorTests(TenantClientFixtures):
    def _runs(self, cursor: str | None, **params):
        params = {"limit": 1, **params, **({"cursor": cursor} if cursor else {})}
        return self.client.get("/api/v1/generation/runs", params=params)

    def _seed(self) -> list[str]:
        with tenant_session(self.ctx) as session:
            for _ in range(2):
                self._create_generation_run(self.ctx, session)
        return [item["id"] for item in self.client.get("/api/v1/generation/runs").json()["items"]]

    def test_generation_runs_page_and_reject_forged_cursors(self):
        ids = self._seed()
        cursor = self.assert_second_page(self._runs, first_ids=ids[:1], second_ids=ids[1:])
        self.assert_forged_cursors_rejected(self._runs, offset_cases(cursor))

    def test_generation_run_cursor_is_bound_to_workspace_and_filters(self):
        self._seed()
        cursor = self._runs(None).json()["next_cursor"]
        self.assert_invalid_cursor(self._runs(cursor, status="running"))
        self._other_workspace()
        self.assert_invalid_cursor(self._runs(cursor))


class AssetGalleryCursorTests(TenantClientFixtures):
    _make_asset_version = gallery_tests.TestAssetGalleryPage._make_asset_version

    def _assets(self, cursor: str | None, **params):
        params = {"limit": 2, **params, **({"cursor": cursor} if cursor else {})}
        return self.client.get("/api/v1/generation/assets", params=params)

    def _seed(self) -> list[str]:
        base = utcnow()
        with tenant_session(self.ctx) as session:
            ids = [
                self._make_asset_version(self.ctx, session, created_at=base + timedelta(seconds=i))["id"]
                for i in range(3)
            ]
        return ids[::-1]  # newest first

    def test_asset_gallery_pages_and_rejects_forged_cursors(self):
        ids = self._seed()
        cursor = self.assert_second_page(self._assets, first_ids=ids[:2], second_ids=ids[2:])
        self.assert_forged_cursors_rejected(self._assets, keyset_cases(cursor))

    def test_asset_gallery_cursor_is_bound_to_workspace_and_filters(self):
        self._seed()
        cursor = self._assets(None).json()["next_cursor"]
        self.assert_invalid_cursor(self._assets(cursor, provider="mock"))
        self._other_workspace()
        self.assert_invalid_cursor(self._assets(cursor))


# ---------------------------------------------------------------------------
# Competitor-scan observations (app/competitors/scan_service.py)
# ---------------------------------------------------------------------------


class CompetitorScanCursorTests(ForgedCursorChecks, scan_tests.ScanFixtures):
    def _scan_with_ads(self) -> str:
        accepted = self._start(self._body([self._confirmed(scan_tests.ACME)], keywords=[]))
        self._drain()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual(scan["status"], "completed", scan["failure"])
        self.assertGreaterEqual(scan["observation_count"], 3)
        return accepted["scan_id"]

    def _ads(self, scan_id: str):
        def get(cursor: str | None, **params):
            params = {"limit": 1, **params, **({"cursor": cursor} if cursor else {})}
            return self.client.get(f"/api/v1/competitor-scans/{scan_id}/ads", params=params)
        return get

    def test_competitor_scan_ads_page_and_reject_forged_cursors(self):
        scan_id = self._scan_with_ads()
        get = self._ads(scan_id)
        ids = [ad["ad_id"] for ad in get(None, limit=2).json()["items"]]
        cursor = self.assert_second_page(get, first_ids=ids[:1], second_ids=ids[1:], key="ad_id")
        self.assert_forged_cursors_rejected(get, offset_cases(cursor))

        cursor = get(None).json()["next_cursor"]
        self.assert_invalid_cursor(get(cursor, origin_kind="page"))
        self.current = self._make_context()
        # The same scan id, so only the Workspace differs.
        self.assert_invalid_cursor(get(cursor))
