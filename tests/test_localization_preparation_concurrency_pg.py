"""PostgreSQL serialization evidence for Localization preparation workers."""
from __future__ import annotations

import os
import threading
import unittest
import uuid
from datetime import datetime, timezone


OWNER_URL = os.environ.get("DOMAIN_OWNER_DATABASE_URL")


@unittest.skipUnless(
    OWNER_URL,
    "set DOMAIN_OWNER_DATABASE_URL to a migrated PostgreSQL domain database",
)
class TestPreparationProjectionConcurrency(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from app.adapters.persistence.models import (
            LibraryEntryRecord,
            LibraryVersionRecord,
            LocalizationBatchRecord,
            WorkspaceRecord,
        )

        cls.engine = create_engine(OWNER_URL, future=True)
        cls.workspace_id = f"ws_loc_prep_{uuid.uuid4().hex[:10]}"
        cls.batch_id = f"lb_loc_prep_{uuid.uuid4().hex[:10]}"
        now = datetime.now(timezone.utc)
        with Session(cls.engine, future=True) as session:
            product_entry = LibraryEntryRecord(
                id=f"le_product_{uuid.uuid4().hex[:10]}",
                workspace_id=cls.workspace_id,
                entry_type="product",
                display_name="Concurrency product",
                lifecycle="active",
            )
            offer_entry = LibraryEntryRecord(
                id=f"le_offer_{uuid.uuid4().hex[:10]}",
                workspace_id=cls.workspace_id,
                entry_type="offer",
                display_name="Concurrency offer",
                lifecycle="active",
            )
            session.add(WorkspaceRecord(id=cls.workspace_id, name=cls.workspace_id))
            # These models expose scalar foreign-key columns rather than ORM
            # relationships, so SQLAlchemy has no unit-of-work dependency edge
            # with which to order pending Workspace/LibraryEntry inserts.
            # Flush the FK parent explicitly; PostgreSQL (correctly) rejects
            # the same-flush ordering SQLite happened to tolerate.
            session.flush()
            session.add_all((product_entry, offer_entry))
            session.flush()
            product = LibraryVersionRecord(
                id=f"lv_product_{uuid.uuid4().hex[:10]}",
                workspace_id=cls.workspace_id,
                entry_id=product_entry.id,
                entry_type="product",
                version_no=1,
                content_json={},
                content_hash="1" * 64,
                state="effective",
                eligibility="eligible",
                published_by="test",
            )
            offer = LibraryVersionRecord(
                id=f"lv_offer_{uuid.uuid4().hex[:10]}",
                workspace_id=cls.workspace_id,
                entry_id=offer_entry.id,
                entry_type="offer",
                version_no=1,
                content_json={},
                content_hash="2" * 64,
                state="effective",
                eligibility="eligible",
                published_by="test",
            )
            session.add_all((product, offer))
            session.flush()
            session.add(
                LocalizationBatchRecord(
                    id=cls.batch_id,
                    workspace_id=cls.workspace_id,
                    product_version_id=product.id,
                    offer_version_id=offer.id,
                    source_market_snapshot_json={
                        "market": "US",
                        "placement": "feed",
                        "locale": "en-US",
                        "country": "US",
                        "currency": "USD",
                    },
                    truth_data_mode="real",
                    provider_execution_mode="mock",
                    phase="preparing",
                    preparation_stage="analysis",
                    outcome="none",
                    frozen_recipe_json={},
                    max_attempts=3,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def test_waiting_completion_refreshes_old_identity_map_after_row_lock(self) -> None:
        from sqlalchemy.orm import Session

        from app.adapters.persistence.models import LocalizationBatchRecord
        from app.localization.worker import _lock_preparation_batch
        from app.platform.tenancy import TenantContext

        ctx = TenantContext(
            workspace_id=self.workspace_id,
            role="owner",
            initiator_id="test",
            initiator_role="owner",
        )
        holder_locked = threading.Event()
        release_holder = threading.Event()
        waiter_preloaded = threading.Event()
        observations: list[str] = []
        errors: list[BaseException] = []

        def holder() -> None:
            try:
                with Session(self.engine, future=True) as session:
                    batch = _lock_preparation_batch(
                        session, ctx, self.batch_id
                    )
                    holder_locked.set()
                    self.assertTrue(release_holder.wait(timeout=10))
                    batch.preparation_stage = "copy"
                    session.commit()
            except BaseException as exc:  # pragma: no cover - thread handoff
                errors.append(exc)

        def waiter() -> None:
            try:
                self.assertTrue(holder_locked.wait(timeout=10))
                with Session(self.engine, future=True) as session:
                    stale = session.get(LocalizationBatchRecord, self.batch_id)
                    observations.append(stale.preparation_stage)
                    waiter_preloaded.set()
                    refreshed = _lock_preparation_batch(
                        session, ctx, self.batch_id
                    )
                    observations.append(refreshed.preparation_stage)
                    refreshed.preparation_stage = "complete"
                    session.commit()
            except BaseException as exc:  # pragma: no cover - thread handoff
                errors.append(exc)

        holder_thread = threading.Thread(target=holder)
        waiter_thread = threading.Thread(target=waiter)
        holder_thread.start()
        waiter_thread.start()
        self.assertTrue(waiter_preloaded.wait(timeout=10))
        release_holder.set()
        holder_thread.join(timeout=10)
        waiter_thread.join(timeout=10)

        self.assertFalse(holder_thread.is_alive())
        self.assertFalse(waiter_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(observations, ["analysis", "copy"])
        with Session(self.engine, future=True) as session:
            final = session.get(LocalizationBatchRecord, self.batch_id)
            self.assertEqual(final.preparation_stage, "complete")
