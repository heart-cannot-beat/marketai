"""Product settings persist immutable, tenant-scoped governed bindings (#226)."""
import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from tests.support.library_fixtures import LibraryFixtures, png_bytes
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import LibraryVersionRecord, ProductRecord, ReferenceObjectRecord, ReferenceBindingRecord
from app.library import service as library
from app.library.contracts import ENTRY_PRODUCT, ROLE_MEMBER
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from app.products import reference_sets
from app.products.v1_router import _reference_error


class ProductReferenceSetsTests(LibraryFixtures):
    def setUp(self):
        self.member = self.ctx()
        self.current = self.member
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        _, self.original_ref = self._reference_version(self.member)
        _, group = self._group_version(self.member, self.original_ref['id'])
        self.entry_id, self.original_version = self._publish(self.member, ENTRY_PRODUCT, 'Product',
            self._product_content(self.original_ref['id'], group['id'],
                                  model_variants=[{'label': 'A', 'specs': {}}, {'label': 'B', 'specs': {}}]))
        with tenant_session(self.member) as session:
            self.product_id = session.scalar(select(ProductRecord.id).where(
                ProductRecord.product_profile_entry_id == self.entry_id))
        self.path = f'/api/v1/products/{self.product_id}/references'

    def view(self):
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def command(self, method, *, body=None, item_id=None, revision=None, key=None):
        return self.client.request(method, self.path + (f'/{item_id}' if item_id else ''), json=body,
            headers={'Idempotency-Key': key or uuid4().hex, 'If-Match': str(revision or self.view()['revision'])})

    def reference(self, color=(100, 40, 40), ctx=None):
        _, version = self._reference_version(ctx or self.member, raw=png_bytes(colour=color))
        return version['id']

    def test_persists_roles_attestation_primary_and_soft_delete_in_library_history(self):
        before = self.view()
        self.assertEqual(before['items'][0]['identity_status'], 'unverified')
        ambiguous_verification = self.command('PATCH', item_id=before['items'][0]['item_id'],
                                              body={'identity_status': 'verified_physical'})
        self.assertEqual(ambiguous_verification.json()['code'], 'product_reference_model_required')
        reference_id = self.reference()
        added = self.command('POST', body={'reference_version_id': reference_id, 'model_variant': 'A',
            'role': 'primary', 'identity_status': 'verified_physical', 'note': 'Photographed in our studio'})
        self.assertEqual(added.status_code, 200, added.text)
        current = self.view()  # Independent session/read proves persistence after refresh.
        item = next(i for i in current['items'] if i['reference_version_id'] == reference_id)
        self.assertEqual(item['added_by'], self.member.initiator_id)
        self.assertTrue(item['available'])
        self.assertEqual(item['identity_status'], 'verified_physical')
        self.assertNotEqual(current['version_id'], before['version_id'])
        with tenant_session(self.member) as session:
            current_content = session.get(LibraryVersionRecord, current['version_id']).content_json
            self.assertEqual(current_content['reference_bindings'][0]['purpose'], 'hero')
            self.assertEqual(current_content['reference_bindings'][0]['viewpoint'], 'front')
            binding = session.scalar(select(ReferenceBindingRecord).where(
                ReferenceBindingRecord.owner_version_id == current['version_id'],
                ReferenceBindingRecord.reference_version_id == reference_id))
            self.assertIsNotNone(binding)
            self.assertEqual(binding.conditions_json['product_reference_item']['model_variant'], 'A')
        patched = self.command('PATCH', item_id=item['item_id'], body={'role': 'logo', 'note': 'Original logo'})
        self.assertEqual(patched.status_code, 200, patched.text)
        deleted = self.command('DELETE', item_id=item['item_id'])
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse(any(i['item_id'] == item['item_id'] for i in self.view()['items']))
        with tenant_session(self.member) as session:
            old = session.get(LibraryVersionRecord, current['version_id'])
            self.assertEqual(old.content_json['reference_bindings'][-1]['note'], 'Photographed in our studio')
            self.assertIsNotNone(session.get(ReferenceObjectRecord, item['reference_object_id']))
            self.assertEqual(len(session.scalars(select(ReferenceBindingRecord).where(
                ReferenceBindingRecord.owner_version_id == current['version_id'])).all()), 2)

    def test_object_reuse_idempotency_and_revision_precondition(self):
        reference_id = self.reference()
        with tenant_session(self.member) as session:
            version = session.get(LibraryVersionRecord, reference_id)
            obj = session.get(ReferenceObjectRecord, version.content_json['object_id'])
            obj.promoted_entry_id = version.entry_id
            object_id = obj.id
        revision = self.view()['revision']
        body = {'reference_object_id': object_id, 'model_variant': 'A'}
        added = self.command('POST', body=body, revision=revision, key='persistent-intent')
        self.assertEqual(added.status_code, 200, added.text)
        replayed = self.command('POST', body=body, revision=revision, key='persistent-intent')
        self.assertEqual(replayed.json(), added.json())
        conflict = self.command('POST', body={**body, 'note': 'different'}, revision=revision, key='persistent-intent')
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()['code'], 'idempotency_key_reused')
        stale = self.command('PATCH', item_id=added.json()['items'][0]['item_id'],
                             body={'note': 'stale'}, revision=revision)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()['code'], 'revision_conflict')

    def test_cross_workspace_object_version_product_and_item_are_refused(self):
        other = self.ctx()
        reference_id = self.reference(ctx=other)
        with tenant_session(other) as session:
            obj_id = session.get(LibraryVersionRecord, reference_id).content_json['object_id']
        for body in [{'reference_version_id': reference_id, 'model_variant': 'A'}, {'reference_object_id': obj_id, 'model_variant': 'A'}]:
            refused = self.command('POST', body=body)
            self.assertEqual(refused.status_code, 404, refused.text)
        self.current = other
        self.assertEqual(self.client.get(self.path).status_code, 404)
        self.assertEqual(self.command('DELETE', item_id='guessed', revision=1).status_code, 404)
        self.current = self.member
        self.assertEqual(self.command('DELETE', item_id='guessed').status_code, 404)

    def test_non_member_cannot_read_or_mutate_and_actor_cannot_be_spoofed(self):
        self.current = replace(self.member, role='system', executor='system:worker', identity_kind=None)
        self.assertEqual(self.client.get(self.path).status_code, 403)
        self.assertEqual(self.command('POST', body={'reference_version_id': self.original_ref['id']}, revision=1).status_code, 403)
        self.current = self.member
        spoofed = self.command('POST', body={'reference_version_id': self.original_ref['id'], 'added_by': 'another-user'})
        self.assertEqual(spoofed.status_code, 422, spoofed.text)

    def test_model_limit_duplicate_primary_and_size_enforced(self):
        reference_id = self.reference()
        invalid = self.command('POST', body={'reference_version_id': reference_id, 'model_variant': 'unknown'})
        self.assertEqual(invalid.json()['code'], 'product_reference_model_invalid')
        ambiguous = self.command('POST', body={'reference_version_id': reference_id})
        self.assertEqual(ambiguous.json()['code'], 'product_reference_model_required')
        shared_logo = self.command('POST', body={'reference_version_id': reference_id, 'role': 'logo'})
        self.assertEqual(shared_logo.status_code, 200, shared_logo.text)
        shared_item = shared_logo.json()['items'][-1]
        ambiguous_role = self.command('PATCH', item_id=shared_item['item_id'], body={'role': 'primary'})
        self.assertEqual(ambiguous_role.json()['code'], 'product_reference_model_required')
        first = self.command('POST', body={'reference_version_id': reference_id, 'model_variant': 'A', 'role': 'primary'})
        self.assertEqual(first.status_code, 200, first.text)
        duplicate = self.command('POST', body={'reference_version_id': reference_id, 'model_variant': 'A'})
        self.assertEqual(duplicate.json()['code'], 'product_reference_duplicate')
        for index in range(7):
            ref = self.reference((20 + index, 30, 50))
            result = self.command('POST', body={'reference_version_id': ref, 'model_variant': 'A', 'role': 'primary'})
            self.assertEqual(result.status_code, 200, result.text)
        model = [i for i in self.view()['items'] if i['model_variant'] == 'A']
        self.assertEqual(sum(i['role'] == 'primary' for i in model), 1)
        too_many = self.command('POST', body={'reference_version_id': self.reference((1, 2, 3)), 'model_variant': 'A'})
        self.assertEqual(too_many.json()['code'], 'product_reference_limit')
        oversized = self.reference((7, 8, 9))
        with tenant_session(self.member) as session:
            obj = session.get(ReferenceObjectRecord, session.get(LibraryVersionRecord, oversized).content_json['object_id'])
            obj.byte_size = 10 * 1024 * 1024 + 1
        refused = self.command('POST', body={'reference_version_id': oversized, 'model_variant': 'B'})
        self.assertEqual(refused.json()['code'], 'product_reference_too_large')

    def test_gate_rights_and_unrelated_draft_edits_are_not_bypassed(self):
        reference_id = self.reference()
        with tenant_session(self.member) as session:
            version = session.get(LibraryVersionRecord, reference_id)
            obj = session.get(ReferenceObjectRecord, version.content_json['object_id'])
            obj.state = 'quarantined'
        blocked = self.command('POST', body={'reference_version_id': reference_id, 'model_variant': 'A'})
        self.assertEqual(blocked.json()['code'], 'product_reference_unavailable')
        with tenant_session(self.member) as session:
            obj = session.get(ReferenceObjectRecord, obj.id)
            obj.state = 'released'
            version = session.get(LibraryVersionRecord, reference_id)
            content = dict(version.content_json)
            content['rights'] = {**content['rights'], 'state': 'revoked'}
            version.content_json = content
        blocked = self.command('POST', body={'reference_version_id': reference_id, 'model_variant': 'A'})
        self.assertEqual(blocked.json()['code'], 'product_reference_unavailable')
        entry = library.get_entry(self.member, self.entry_id)
        edited = {**entry['draft']['content'], 'name': 'Unpublished name'}
        library.save_draft(self.member, self.entry_id, edited, entry['draft']['revision'])
        view = self.view()
        self.assertTrue(view['has_unpublished_changes'])
        blocked = self.command('PATCH', item_id=view['items'][0]['item_id'], body={'note': 'must not overwrite'})
        self.assertEqual(blocked.json()['code'], 'product_reference_draft_conflict')
        self.assertEqual(library.get_entry(self.member, self.entry_id)['draft']['content']['name'], 'Unpublished name')


class ReferenceErrorMappingTests(unittest.TestCase):
    """A conflict keeps its own code; the two fixed conflicts stay fixed (#370)."""

    def test_conflict_with_only_a_code_keeps_it(self):
        error = _reference_error(library.LibraryConflict('Entry archived', code='entry_archived'))
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.detail['code'], 'entry_archived')

    def test_conflict_with_a_detail_code_keeps_it(self):
        error = _reference_error(library.LibraryConflict('Duplicate', {'code': 'product_reference_duplicate'}))
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.detail['code'], 'product_reference_duplicate')

    def test_conflict_with_a_code_and_a_detail_keeps_both(self):
        conflict = library.LibraryConflict('Unchanged', {'current_version_id': 'v'}, code='library_version_unchanged')
        error = _reference_error(conflict)
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.detail['code'], 'library_version_unchanged')
        self.assertEqual(conflict.detail, {'current_version_id': 'v'})

    def test_conflict_without_a_code_is_a_library_conflict(self):
        error = _reference_error(library.LibraryConflict('Changed'))
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.detail['code'], 'library_conflict')

    def test_revision_and_idempotency_conflicts_keep_their_fixed_codes(self):
        for conflict in [library.RevisionConflict('Stale', {'current_revision': 3}),
                         library.RevisionConflict('Stale', code='library_conflict')]:
            self.assertIsInstance(conflict, library.LibraryConflict)
            error = _reference_error(conflict)
            self.assertEqual(error.status_code, 409)
            self.assertEqual(error.detail['code'], 'revision_conflict')
        for conflict in [library.IdempotencyConflict('Reused', {'code': 'idempotency_key_reused'}),
                         library.IdempotencyConflict('Reused'),
                         library.IdempotencyConflict('Reused', code='library_conflict')]:
            self.assertIsInstance(conflict, library.LibraryConflict)
            error = _reference_error(conflict)
            self.assertEqual(error.status_code, 409)
            self.assertEqual(error.detail['code'], 'idempotency_key_reused')

    def test_not_found_forbidden_and_invalid_stay_merged(self):
        missing = _reference_error(library.LibraryNotFound('Missing', code='product_not_found'))
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.detail['code'], 'product_reference_not_found')
        forbidden = _reference_error(library.LibraryForbidden('Forbidden'))
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.detail['code'], 'product_reference_forbidden')
        invalid = _reference_error(library.LibraryValidationError('Invalid', {}))
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.detail['code'], 'product_reference_validation_failed')


class ReferenceConflictRouteTests(unittest.TestCase):
    def setUp(self):
        context = TenantContext(workspace_id='ws_reference_conflict', role=ROLE_MEMBER, initiator_id='user_a',
                                request_id='test-req', trace_id='trace-test')
        app.dependency_overrides[request_tenant_context] = lambda: context
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_command_conflict_reaches_the_page_with_its_own_code(self):
        conflict = library.LibraryConflict('Entry archived', code='entry_archived')
        with patch.object(reference_sets, 'mutate', side_effect=conflict):
            response = self.client.post('/api/v1/products/product-1/references',
                json={'reference_version_id': 'version-1', 'model_variant': 'A'},
                headers={'Idempotency-Key': uuid4().hex, 'If-Match': '1'})
        self.assertEqual(response.status_code, 409, response.text)
        problem = response.json()
        self.assertEqual(problem['code'], 'entry_archived')
        self.assertEqual(problem['status'], 409)
        self.assertTrue(problem['trace_id'])
