"""Map an observed ad to a Winner's frozen context, for the DeliveryTarget
entry's tests (#129).

The product records which ExperimentVersion an ad ran for through the
sandbox Launch that created it, an Experiment candidate ad binding or a
marketing ad binding. A test that needs an ad behind a given context binds
it, through an Experiment candidate ad binding, to a copy of the Winner's
ExperimentVersion -- and its Experiment, entry and candidate -- with chosen
fields changed. A copy is needed because an entry has at most one ad
binding, and an ad account and ad at most one across the database.
Fictional brand only: Acme Charge.
"""
from __future__ import annotations

from sqlalchemy import inspect as sa_inspect

from app.adapters.persistence.models import (
    ExperimentAdBindingRecord,
    ExperimentEntryRecord,
    ExperimentRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    new_id,
)


def clone_record(session, record, **overrides):
    """A copy of ``record`` with every column taken over, then ``overrides``."""
    mapper = sa_inspect(type(record))
    values = {attribute.key: getattr(record, attribute.key) for attribute in mapper.column_attrs}
    values.update(overrides)
    copy = type(record)(**values)
    session.add(copy)
    session.flush()
    return copy


def copy_winner_version(
    session, *, version_id: str, version_candidate_id: str, suffix: str,
    asset_version_id: str | None = None, **version_fields,
) -> tuple[ExperimentEntryRecord, ExperimentVersionCandidateRecord]:
    """A copy of the Winner's ExperimentVersion, its Experiment, entry and
    candidate, with ``version_fields`` (and the AssetVersion) changed."""
    version = session.get(ExperimentVersionRecord, version_id)
    candidate = session.get(ExperimentVersionCandidateRecord, version_candidate_id)
    experiment = session.get(ExperimentRecord, version.experiment_id)
    entry = session.get(ExperimentEntryRecord, candidate.source_entry_id)
    experiment_copy = clone_record(session, experiment, id=f"exp_{suffix}")
    entry_copy = clone_record(
        session, entry, id=f"ent_{suffix}", experiment_id=experiment_copy.id,
        asset_version_id=asset_version_id or entry.asset_version_id,
    )
    version_copy = clone_record(
        session, version, id=f"ver_{suffix}", experiment_id=experiment_copy.id, **version_fields,
    )
    candidate_copy = clone_record(
        session, candidate, id=f"evc_{suffix}", experiment_version_id=version_copy.id,
        source_entry_id=entry_copy.id, asset_version_id=asset_version_id or candidate.asset_version_id,
    )
    return entry_copy, candidate_copy


def bind_ad_to_winner_copy(
    session, *, workspace_id: str, version_id: str, version_candidate_id: str, suffix: str,
    ad_account_id: str, ad_id: str, with_version: bool = True,
    asset_version_id: str | None = None, **version_fields,
) -> None:
    """An Experiment candidate ad binding of ``ad_id`` to a copy of the
    Winner's ExperimentVersion; ``with_version=False`` binds the entry only,
    the way a binding made before freeze names no version candidate."""
    entry, candidate = copy_winner_version(
        session, version_id=version_id, version_candidate_id=version_candidate_id, suffix=suffix,
        asset_version_id=asset_version_id, **version_fields,
    )
    session.add(ExperimentAdBindingRecord(
        id=new_id("eab"), workspace_id=workspace_id, experiment_entry_id=entry.id,
        experiment_version_candidate_id=candidate.id if with_version else None,
        ad_account_id=ad_account_id, ad_id=ad_id, source="manual_read_only",
    ))
    session.flush()
