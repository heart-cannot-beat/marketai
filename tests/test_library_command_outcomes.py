"""Gate 0.3 authoritative Creative Library command-outcome recovery tests."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    LibraryEntryRecord,
    LibraryIdempotencyRecord,
    LibraryVersionRecord,
    new_id,
    utcnow,
)
from app.library import command_outcomes, service
from app.library.contracts import ENTRY_PRODUCT
from tests.support.library_fixtures import LibraryFixtures


class LibraryCommandOutcomeTests(LibraryFixtures):
    def _create_with_outcome(
        self,
        context,
        *,
        identity: str = "command.identity.create.001",
        key: str = "command-key-create-001",
        name: str = "Recoverable",
    ) -> dict:
        content = {"name": name, "description": "d", "audience": "a"}
        fingerprint = service.compute_fingerprint(
            {
                "entry_type": ENTRY_PRODUCT,
                "display_name": name,
                "content": content,
            }
        )
        return service.create_entry(
            context,
            entry_type=ENTRY_PRODUCT,
            display_name=name,
            content=content,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            command_identity=identity,
        )

    def _source_product_version(self, context) -> str:
        entry_id = new_id("lib")
        version_id = new_id("libver")
        now = utcnow()
        content = {
            "name": "Source",
            "description": "source product",
            "audience": "operators",
            "selling_points": ["durable"],
        }
        with tenant_session(context) as session:
            entry = LibraryEntryRecord(
                id=entry_id,
                workspace_id=context.workspace_id,
                entry_type=ENTRY_PRODUCT,
                display_name="Source",
                lifecycle="active",
                current_version_id=version_id,
                created_at=now,
                updated_at=now,
            )
            session.add(entry)
            session.flush()
            session.add(
                LibraryVersionRecord(
                    id=version_id,
                    workspace_id=context.workspace_id,
                    entry_id=entry_id,
                    entry_type=ENTRY_PRODUCT,
                    version_no=1,
                    content_json=content,
                    content_hash=service.compute_fingerprint(content),
                    content_schema_version=1,
                    lifecycle_revision=1,
                    state="effective",
                    eligibility="eligible",
                    validation_json={"ok": True},
                    origin_command="create",
                    published_by=context.initiator_id or "",
                    published_at=now,
                )
            )
        return version_id

    def test_succeeded_outcome_replays_exact_entry_id_without_raw_identity_at_rest(self) -> None:
        context = self.ctx()
        identity = "command.identity.success.001"
        entry = self._create_with_outcome(context, identity=identity)

        outcome = service.get_command_outcome(
            context, identity, expected_command_kind="create_entry"
        )

        self.assertEqual(outcome["outcome"], "succeeded")
        self.assertEqual(outcome["contract_schema_version"], 2)
        self.assertEqual(outcome["command_kind"], "create_entry")
        self.assertEqual(outcome["command_identity"], identity)
        self.assertEqual(outcome["result"]["id"], entry["id"])
        self.assertEqual(
            outcome["result_reference"],
            {"resource_type": "library_entry", "resource_id": entry["id"]},
        )
        with tenant_session(context) as session:
            record = session.scalars(
                select(LibraryIdempotencyRecord).where(
                    LibraryIdempotencyRecord.workspace_id == context.workspace_id,
                    LibraryIdempotencyRecord.command_identity_hash
                    == command_outcomes.hash_command_identity(identity),
                )
            ).one()
            self.assertNotIn(identity, repr(record.__dict__))

    def test_response_loss_is_recovered_from_committed_outcome(self) -> None:
        context = self.ctx()
        identity = "command.identity.response.loss.001"
        committed = self._create_with_outcome(context, identity=identity)
        del committed  # simulate losing the HTTP response after commit

        recovered = service.get_command_outcome(context, identity)
        self.assertEqual(recovered["outcome"], "succeeded")
        self.assertTrue(recovered["result"]["id"].startswith("lib_"))

    def test_missing_record_requires_reconciliation_and_never_claims_not_applied(self) -> None:
        context = self.ctx()
        outcome = service.get_command_outcome(
            context,
            "command.identity.missing.001",
            expected_command_kind="fork",
        )
        self.assertEqual(outcome["outcome"], "reconciliation_required")
        self.assertEqual(outcome["command_kind"], "fork")
        self.assertEqual(outcome["reason_code"], "command_outcome_not_found")

    def test_crash_before_mutation_reconciles_to_authoritative_not_applied(self) -> None:
        context = self.ctx()
        identity = "command.identity.crash.before.001"
        key = "command-key-crash-before-001"
        fingerprint = service.compute_fingerprint({"probe": "before"})
        identity_hash = command_outcomes.hash_command_identity(identity)
        service._ensure_idempotency_reservation(
            context, key, "fork", fingerprint, identity_hash
        )

        pending = service.get_command_outcome(context, identity)
        self.assertEqual(pending["outcome"], "pending")

        reconciled = service.reconcile_command_outcome(
            context, identity, expected_command_kind="fork"
        )
        self.assertEqual(reconciled["outcome"], "not_applied")
        self.assertIsNotNone(reconciled["reconciled_at"])
        self.assertEqual(
            service.get_command_outcome(context, identity)["outcome"],
            "not_applied",
        )

    def test_terminal_problem_is_authoritatively_recoverable(self) -> None:
        member = self.ctx()
        entry = service.create_entry(
            member,
            entry_type=ENTRY_PRODUCT,
            display_name="Cannot archive",
            content={},
        )
        identity = "command.identity.problem.001"
        fingerprint = service.compute_fingerprint(
            {"entry_id": entry["id"], "_if_match": 1}
        )
        with self.assertRaises(service.LibraryForbidden):
            service.archive_entry(
                member,
                entry["id"],
                expected_revision=1,
                idempotency_key="command-key-problem-001",
                request_fingerprint=fingerprint,
                command_identity=identity,
            )

        outcome = service.get_command_outcome(member, identity)
        self.assertEqual(outcome["outcome"], "problem")
        self.assertEqual(outcome["problem"]["status"], 403)
        self.assertEqual(outcome["problem"]["code"], "library_forbidden")

    def test_same_workspace_wrong_actor_cannot_read_or_reconcile(self) -> None:
        context = self.ctx(actor="actor_a")
        identity = "command.identity.actor.bound.001"
        self._create_with_outcome(context, identity=identity)
        attacker = self.ctx(
            workspace=context.workspace_id,
            actor="actor_b",
            provision=False,
        )
        with self.assertRaises(service.LibraryNotFound):
            service.get_command_outcome(attacker, identity)
        with self.assertRaises(service.LibraryNotFound):
            service.reconcile_command_outcome(attacker, identity)

    def test_wrong_workspace_does_not_disclose_an_existing_outcome(self) -> None:
        context = self.ctx()
        identity = "command.identity.workspace.bound.001"
        self._create_with_outcome(context, identity=identity)
        other = self.ctx()
        outcome = service.get_command_outcome(other, identity)
        self.assertEqual(outcome["outcome"], "reconciliation_required")
        self.assertNotIn("result", outcome)

    def test_wrong_command_kind_is_rejected_on_lookup(self) -> None:
        context = self.ctx()
        identity = "command.identity.kind.bound.001"
        self._create_with_outcome(context, identity=identity)
        with self.assertRaises(service.IdempotencyConflict):
            service.get_command_outcome(
                context, identity, expected_command_kind="clone"
            )

    def test_same_identity_with_different_fingerprint_is_rejected(self) -> None:
        context = self.ctx()
        identity = "command.identity.fingerprint.001"
        self._create_with_outcome(context, identity=identity)
        different_content = {"name": "Different", "description": "d", "audience": "a"}
        fingerprint = service.compute_fingerprint(
            {
                "entry_type": ENTRY_PRODUCT,
                "display_name": "Different",
                "content": different_content,
            }
        )
        with self.assertRaises(service.IdempotencyConflict):
            service.create_entry(
                context,
                entry_type=ENTRY_PRODUCT,
                display_name="Different",
                content=different_content,
                idempotency_key="command-key-create-001",
                request_fingerprint=fingerprint,
                command_identity=identity,
            )

    def test_malformed_identity_is_rejected_before_reservation(self) -> None:
        context = self.ctx()
        content = {"name": "Malformed", "description": "d", "audience": "a"}
        fingerprint = service.compute_fingerprint(
            {"entry_type": ENTRY_PRODUCT, "display_name": "Malformed", "content": content}
        )
        with self.assertRaisesRegex(service.LibraryError, "command identity"):
            service.create_entry(
                context,
                entry_type=ENTRY_PRODUCT,
                display_name="Malformed",
                content=content,
                idempotency_key="malformed-identity-key",
                request_fingerprint=fingerprint,
                command_identity="contains whitespace",
            )
        with tenant_session(context) as session:
            count = session.scalar(
                select(func.count()).select_from(LibraryIdempotencyRecord).where(
                    LibraryIdempotencyRecord.workspace_id == context.workspace_id
                )
            )
        self.assertEqual(count, 0)

    def test_server_outcome_survives_an_expired_local_retention_window(self) -> None:
        context = self.ctx()
        identity = "command.identity.expired.local.001"
        self._create_with_outcome(context, identity=identity)
        old = datetime.now(timezone.utc) - timedelta(days=365)
        with tenant_session(context) as session:
            record = session.scalars(
                select(LibraryIdempotencyRecord).where(
                    LibraryIdempotencyRecord.workspace_id == context.workspace_id,
                    LibraryIdempotencyRecord.command_identity_hash
                    == command_outcomes.hash_command_identity(identity),
                )
            ).one()
            record.created_at = old
            record.updated_at = old

        outcome = service.get_command_outcome(context, identity)
        self.assertEqual(outcome["outcome"], "succeeded")
        self.assertEqual(outcome["created_at"].replace(tzinfo=timezone.utc), old)

    def test_concurrent_fork_and_clone_with_one_identity_commit_only_one_entry(self) -> None:
        context = self.ctx()
        version_id = self._source_product_version(context)
        identity = "command.identity.concurrent.fork.clone.001"
        barrier = threading.Barrier(2)
        results: list[dict] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def fork() -> None:
            try:
                barrier.wait(timeout=10)
                fingerprint = service.compute_fingerprint(
                    {"version_id": version_id, "display_name": "Forked"}
                )
                result = service.fork_version(
                    context,
                    version_id,
                    display_name="Forked",
                    idempotency_key="concurrent-fork-key",
                    request_fingerprint=fingerprint,
                    command_identity=identity,
                )
                with lock:
                    results.append(result)
            except BaseException as exc:  # pragma: no cover - asserted below
                with lock:
                    errors.append(exc)

        def clone() -> None:
            try:
                barrier.wait(timeout=10)
                fingerprint = service.compute_fingerprint(
                    {
                        "version_id": version_id,
                        "display_name": "Cloned",
                        "include_optional": [],
                    }
                )
                result = service.clone_product(
                    context,
                    version_id,
                    display_name="Cloned",
                    include_optional=[],
                    idempotency_key="concurrent-clone-key",
                    request_fingerprint=fingerprint,
                    command_identity=identity,
                )
                with lock:
                    results.append(result)
            except BaseException as exc:  # pragma: no cover - asserted below
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=fork), threading.Thread(target=clone)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive(), "concurrent Library command hung")

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], service.IdempotencyConflict)
        with tenant_session(context) as session:
            entry_count = session.scalar(
                select(func.count()).select_from(LibraryEntryRecord).where(
                    LibraryEntryRecord.workspace_id == context.workspace_id
                )
            )
        self.assertEqual(entry_count, 2)  # source + exactly one Fork/Clone result
