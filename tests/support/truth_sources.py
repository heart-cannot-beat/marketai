"""Publish a DataSource and the TruthResolutionPolicy that lists it (#70).

A policy's ``rules[].source_priority`` lists DataSource *entry* ids in
priority order. Library publish resolves each one to the entry's current
version (the stored dependency edge stays version-to-version), and Truth
resolution reads the entry's current effective version, whose
``adapter_kind`` picks the adapter.

Tests and the E2E seeds publish both through ``app.library.service`` so the
policy has passed publish validation, the same way an Owner's policy does,
instead of inserting LibraryVersion rows directly. Call these outside any
open ``tenant_session``: the service opens its own transaction, and SQLite
allows one writer at a time.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.library import service
from app.library.contracts import ENTRY_DATA_SOURCE, ENTRY_TRUTH_RESOLUTION_POLICY
from app.platform.tenancy import TenantContext


@dataclass(frozen=True)
class PublishedTruthSources:
    data_source_entry_id: str
    data_source_version_id: str
    policy_entry_id: str
    policy_version_id: str


def _publish(ctx: TenantContext, entry_type: str, display_name: str, content: dict) -> tuple[str, str]:
    entry = service.create_entry(ctx, entry_type=entry_type, display_name=display_name, content=content)
    version = service.publish(ctx, entry["id"], entry["draft"]["revision"])
    return entry["id"], version["id"]


def publish_data_source(ctx: TenantContext, display_name: str, content: dict) -> tuple[str, str]:
    """Create and publish a DataSource: ``(entry_id, version_id)``."""
    return _publish(ctx, ENTRY_DATA_SOURCE, display_name, content)


def republish_data_source(ctx: TenantContext, entry_id: str, content: dict) -> str:
    """Save new DataSource content and publish it; the old version is superseded."""
    draft = service.save_draft(ctx, entry_id, content, expected_revision=_draft_revision(ctx, entry_id))
    return service.publish(ctx, entry_id, draft["revision"])["id"]


def _draft_revision(ctx: TenantContext, entry_id: str) -> int:
    return int(service.get_entry(ctx, entry_id)["draft"]["revision"])


def truth_policy_content(
    source_priority: Sequence[str],
    fact_rules: Mapping[str, str],
    *,
    max_staleness_seconds: int = 86400,
) -> dict:
    """One rule per fact type (``fact_type -> on_failure``), all sharing ``source_priority``."""
    return {
        "rules": [
            {
                "fact_type": fact_type,
                "source_priority": list(source_priority),
                "on_failure": on_failure,
                "max_staleness_seconds": max_staleness_seconds,
            }
            for fact_type, on_failure in fact_rules.items()
        ]
    }


def publish_truth_policy(ctx: TenantContext, display_name: str, content: dict) -> tuple[str, str]:
    """Create and publish a TruthResolutionPolicy: ``(entry_id, version_id)``."""
    return _publish(ctx, ENTRY_TRUTH_RESOLUTION_POLICY, display_name, content)


def publish_truth_sources(
    ctx: TenantContext,
    name: str,
    *,
    data_source_content: dict,
    fact_rules: Mapping[str, str],
    max_staleness_seconds: int = 86400,
) -> PublishedTruthSources:
    """Publish one DataSource and a policy whose every rule lists it by entry id."""
    data_source_entry_id, data_source_version_id = publish_data_source(
        ctx, f"Data source {name}", data_source_content
    )
    policy_entry_id, policy_version_id = publish_truth_policy(
        ctx,
        f"Truth policy {name}",
        truth_policy_content(
            [data_source_entry_id], fact_rules, max_staleness_seconds=max_staleness_seconds
        ),
    )
    return PublishedTruthSources(
        data_source_entry_id=data_source_entry_id,
        data_source_version_id=data_source_version_id,
        policy_entry_id=policy_entry_id,
        policy_version_id=policy_version_id,
    )
