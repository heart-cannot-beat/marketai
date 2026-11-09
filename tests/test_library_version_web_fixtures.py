"""The web's Library version fixture is what FastAPI really answers, so the version toolbar cannot drift from the server (#25).

`web/__tests__/fixtures/library-versions.json` holds `GET /library/versions/{id}`
for an Owner and a Member, a Withdraw impact preview, a withdrawn version,
the 404 for another Workspace's version id and the refusal of a Clone Product
asked to copy Offers (ADR 0016). Ids, hashes and times are replaced
by stable placeholders; everything else is the response as sent.

After a deliberate change to these responses, regenerate and format:
    .venv/bin/python -m tests.test_library_version_web_fixtures --write
    (cd web && npx prettier --write __tests__/fixtures/library-versions.json)
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

# Before `app.main`: sets the isolated per-process database (see test_v1_library_routes).
from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.models import new_id
from app.library.contracts import ENTRY_OFFER, ENTRY_PERSON, ENTRY_PRODUCT, ROLE_MEMBER
from app.main import app
from app.platform.http_identity import request_tenant_context

FIXTURE = Path(__file__).resolve().parents[1] / "web" / "__tests__" / "fixtures" / "library-versions.json"

_ID = re.compile(r"\b([a-z]+)_([0-9a-f]{32})\b")
_HASH = re.compile(r"\b[0-9a-f]{64}\b")
_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")


def _normalize(value, ids: dict[str, str]):
    """Stable placeholders for generated ids, content hashes and timestamps."""
    if isinstance(value, dict):
        return {key: _normalize(item, ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item, ids) for item in value]
    if not isinstance(value, str):
        return value

    def _id(match: re.Match) -> str:
        whole, prefix = match.group(0), match.group(1)
        if whole not in ids:
            ids[whole] = f"{prefix}_{sum(1 for v in ids.values() if v.startswith(prefix + '_')) + 1:02d}"
        return ids[whole]

    text = _ID.sub(_id, value)
    text = _HASH.sub("0" * 64, text)
    return _TIME.sub("2026-11-08T00:00:00+00:00", text)


def responses(fx: LibraryFixtures) -> dict:
    """The responses, built with the shared Library fixture helpers on ``fx``."""
    client = TestClient(app, raise_server_exceptions=False)

    def as_(ctx):
        app.dependency_overrides[request_tenant_context] = lambda: ctx
        return client

    try:
        member = fx.ctx(ROLE_MEMBER, actor="member_a")
        owner = fx.owner_ctx(member)

        _ref_entry, ref_version = fx._reference_version(member, name="Acme Charge front")
        _group_entry, group_version = fx._group_version(member, ref_version["id"])
        product_content = fx._product_content(
            ref_version["id"], group_version["id"],
            name="Acme Charge 65W", description="65W GaN charger",
            audience="Commuters", selling_points=["Compact", "Three ports"],
            brand_constraints="Keep the logo whole",
        )
        product_content["must_match"][0]["description"] = "Front logo stays readable"
        product_content["reference_bindings"][0]["note"] = "Hero"
        _product_entry, product = fx._publish(member, ENTRY_PRODUCT, "Acme Charge 65W", product_content)
        fx._publish(owner, ENTRY_OFFER, "Acme Charge 65W single", fx._offer_content(
            product["id"],
            item={"sku": "ACME-65W", "variant_label": "Black", "quantity": 1, "bundle_components": []},
            landing_page_url="https://acme-charge.example/65w",
            claim_constraints=["No medical claims"],
        ))
        _person_entry, person = fx._publish(member, ENTRY_PERSON, "Nova", fx._person_content(
            ref_version["id"], wardrobe="Business casual", temperament="Calm",
            prompt_fragments=["calm person in business casual"],
            rights={"state": "granted", "scope": "Fictional character", "regions": ["US"], "channels": ["meta"]},
        ))

        def version(ctx, version_id):
            resp = as_(ctx).get(f"/api/v1/library/versions/{version_id}")
            fx.assertEqual(resp.status_code, 200, resp.text)
            return resp.json()

        result = {
            "owner_effective_product": version(owner, product["id"]),
            "member_effective_product": version(member, product["id"]),
            "member_effective_person": version(member, person["id"]),
        }
        impact = as_(owner).post(f"/api/v1/library/versions/{product['id']}/withdraw-impact")
        fx.assertEqual(impact.status_code, 201, impact.text)
        result["owner_withdraw_impact"] = impact.json()
        # ADR 0016: Offers, prices, mappings and landing pages are never copied.
        refused_clone = as_(owner).post(
            f"/api/v1/library/versions/{product['id']}/clone-product",
            json={"display_name": "Acme Charge 65W (copy)", "include_optional": ["offers"]},
            headers={"Idempotency-Key": f"ik-fixture-clone-{new_id('k')}"},
        )
        fx.assertEqual(refused_clone.status_code, 400, refused_clone.text)
        withdrawn = as_(owner).post(
            f"/api/v1/library/versions/{product['id']}/withdraw",
            json={"reason": "Recalled for legal review",
                  "impact_id": impact.json()["impact_id"], "impact_hash": impact.json()["impact_hash"]},
            headers={"Idempotency-Key": f"ik-fixture-withdraw-{new_id('k')}"},
        )
        fx.assertEqual(withdrawn.status_code, 200, withdrawn.text)
        result["owner_withdrawn_product"] = version(owner, product["id"])

        other = fx.owner_ctx(fx.ctx(ROLE_MEMBER))
        missing = as_(other).get(f"/api/v1/library/versions/{product['id']}")
        fx.assertEqual(missing.status_code, 404, missing.text)
        result["other_workspace_not_found"] = missing.json()
        result["clone_with_offers_refused"] = refused_clone.json()
        return _normalize(result, {})
    finally:
        app.dependency_overrides.pop(request_tenant_context, None)
        client.close()


class TestLibraryVersionWebFixture(LibraryFixtures):
    def test_fixture_is_what_the_server_answers(self):
        self.assertEqual(json.loads(FIXTURE.read_text()), responses(self))

    def test_fixture_covers_what_the_version_toolbar_distinguishes(self):
        stored = json.loads(FIXTURE.read_text())
        actions = {
            key: {a["action"]: (a["allowed"], a["reason_code"]) for a in body["allowed_actions"]}
            for key, body in stored.items() if "allowed_actions" in body
        }
        self.assertEqual(actions["owner_effective_product"], {
            "clone_product": (True, None), "fork": (True, None), "withdraw": (True, None)})
        self.assertEqual(actions["member_effective_product"]["withdraw"], (False, "owner_required"))
        self.assertEqual(actions["member_effective_person"]["clone_product"], (False, "entry_type_mismatch"))
        self.assertEqual(set(actions["owner_withdrawn_product"].values()), {(False, "invalid_state")})
        self.assertEqual(stored["owner_withdrawn_product"]["state"], "withdrawn")
        self.assertEqual(stored["owner_withdraw_impact"]["projection"]["dependent_count"], 1)
        self.assertEqual(stored["other_workspace_not_found"]["status"], 404)
        self.assertEqual(stored["clone_with_offers_refused"]["code"], "library_clone_field_forbidden")


def write():
    LibraryFixtures.setUpClass()
    FIXTURE.write_text(json.dumps(responses(LibraryFixtures()), ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    if "--write" in sys.argv:
        write()
    else:
        unittest.main()
