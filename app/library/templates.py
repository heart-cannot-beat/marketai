"""Dimension template commands: constraint solver, apply, reapply.

Spec §4.1, §4.6, §9.1, §9.2.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from itertools import product as cartesian
from typing import Any

from ..adapters.persistence.models import (
    LibraryDraftRecord,
    LibraryEntryRecord,
    new_id,
)
from ..platform.tenancy import TenantContext
from .contracts import (
    COMMAND_APPLY,
    DRAFT_EDITING,
    ENTRY_DIMENSION_SCHEMA,
    ENTRY_DIMENSION_TEMPLATE,
    ENTRY_PRODUCT,
    Finding,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ARCHIVED,
    SEVERITY_BLOCKING,
    canonical_hash,
)
from .service import (
    LibraryError,
    LibraryNotFound,
    _audit,
    _emit,
    _entry,
    _entry_dict,
    _require_workspace,
    _run_with_idempotency,
    _version,
    derived_display_name,
)

COMBINATION_LIMIT = 10_000


@dataclass(frozen=True)
class SkipReason:
    combination: dict[str, str]
    primary_reason: str
    violations: list[str]


@dataclass
class ConstraintResult:
    valid_items: list[dict[str, str]]
    skip_reasons: list[SkipReason]
    is_satisfiable: bool
    limit_findings: list[Finding] = field(default_factory=list)


def detect_cycles(constraints: list[dict], dim_ids: set[str]) -> list[Finding]:
    """Detect authoring cycles in requires chains (§9.3, spec §4.1)."""
    graph: dict[str, list[str]] = {}
    for c in constraints:
        if c.get("type") != "requires":
            continue
        src = f"{c.get('from_dimension_id')}:{c.get('from_value_id')}"
        dst = f"{c.get('to_dimension_id')}:{c.get('to_value_id')}"
        graph.setdefault(src, []).append(dst)

    visited: set[str] = set()
    in_stack: set[str] = set()
    findings: list[Finding] = []

    def dfs(node: str) -> bool:
        if node in in_stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        in_stack.add(node)
        for neighbor in graph.get(node, []):
            if dfs(neighbor):
                findings.append(Finding(
                    "constraint_cycle",
                    f"requires 链存在循环: {node} → {neighbor}",
                    "constraints",
                ))
                return True
        in_stack.discard(node)
        return False

    for node in graph:
        if node not in visited:
            dfs(node)
    return findings


def check_satisfiability(
    dimensions: list[dict],
    constraints: list[dict],
    cardinality: str,
) -> list[Finding]:
    """Bounded check: at least one valid combination exists (§4.6 call site 1)."""
    if not dimensions:
        return [Finding("unsatisfiable", "没有维度", "dimensions")]
    total = 1
    for dim in dimensions:
        vals = dim.get("values") or []
        total *= max(len(vals), 1)
        if total > COMBINATION_LIMIT:
            return []  # Too large to check here; expansion will catch it
    combos = _expand_combinations(dimensions)
    valid = [c for c in combos if not _collect_violations(c, constraints)]
    if not valid:
        return [Finding("unsatisfiable", "约束导致零个有效组合", "constraints")]
    return []


def evaluate_constraints(
    schema_content: dict,
    planned_items: list[dict[str, str]] | None = None,
) -> ConstraintResult:
    """Dual call-site function (§4.6).

    planned_items=None → satisfiability-only (no expansion if too large).
    planned_items=list → filter planned items and produce SkipReasons.
    """
    dims = schema_content.get("dimensions") or []
    constraints = schema_content.get("constraints") or []
    cardinality = schema_content.get("combination_cardinality", "one_per_dimension")

    if planned_items is not None:
        valid = []
        skips = []
        for combo in planned_items:
            violations = _collect_violations(combo, constraints)
            if violations:
                skips.append(SkipReason(
                    combination=combo,
                    primary_reason=violations[0],
                    violations=violations,
                ))
            else:
                valid.append(combo)
        return ConstraintResult(
            valid_items=valid,
            skip_reasons=skips,
            is_satisfiable=len(valid) > 0,
        )

    total = 1
    for dim in dims:
        total *= max(len(dim.get("values") or []), 1)
    if total > COMBINATION_LIMIT:
        return ConstraintResult(
            valid_items=[],
            skip_reasons=[],
            is_satisfiable=False,
            limit_findings=[Finding(
                "combination_limit_exceeded",
                f"组合数 {total} 超过限制 {COMBINATION_LIMIT}",
                "dimensions",
            )],
        )

    combos = _expand_combinations(dims)
    valid = []
    skips = []
    for combo in combos:
        violations = _collect_violations(combo, constraints)
        if violations:
            skips.append(SkipReason(
                combination=combo,
                primary_reason=violations[0],
                violations=violations,
            ))
        else:
            valid.append(combo)
    return ConstraintResult(
        valid_items=valid,
        skip_reasons=skips,
        is_satisfiable=len(valid) > 0,
    )


def _expand_combinations(dimensions: list[dict]) -> list[dict[str, str]]:
    """Generate all one-value-per-dimension combinations."""
    if not dimensions:
        return [{}]
    dim_values = []
    for dim in dimensions:
        dim_id = dim["id"]
        vals = [(dim_id, v["id"]) for v in (dim.get("values") or [])]
        dim_values.append(vals)
    results = []
    for combo in cartesian(*dim_values):
        results.append({dim_id: val_id for dim_id, val_id in combo})
    return results


def _collect_violations(combo: dict[str, str], constraints: list[dict]) -> list[str]:
    """Return all constraint violations for a single combination."""
    violations = []
    for c in constraints:
        ctype = c.get("type")
        if ctype == "requires":
            from_dim = c.get("from_dimension_id")
            from_val = c.get("from_value_id")
            to_dim = c.get("to_dimension_id")
            to_val = c.get("to_value_id")
            if combo.get(from_dim) == from_val and combo.get(to_dim) != to_val:
                violations.append(
                    f"requires: {from_dim}={from_val} → {to_dim}={to_val}"
                )
        elif ctype == "excludes":
            dim_a = c.get("dimension_id_a")
            val_a = c.get("value_id_a")
            dim_b = c.get("dimension_id_b")
            val_b = c.get("value_id_b")
            if combo.get(dim_a) == val_a and combo.get(dim_b) == val_b:
                violations.append(f"excludes: {dim_a}={val_a} ⊗ {dim_b}={val_b}")
    return violations


# --------------------------------------------------------------------------
# Apply / Reapply template（§9.1、§9.2）
# --------------------------------------------------------------------------

_CONSTRAINT_DIMENSION_KEYS = (
    "from_dimension_id",
    "to_dimension_id",
    "dimension_id_a",
    "dimension_id_b",
    "dimension_id",
)
_CONSTRAINT_VALUE_KEYS = (
    "from_value_id",
    "to_value_id",
    "value_id_a",
    "value_id_b",
)


def _copy_template_dimensions(dimensions: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Deep-copy template dimensions/values under fresh product-local IDs.

    Returns the copied dimensions plus an ``old_id -> new_id`` map (dimension
    and value IDs share one namespace since they never collide) used to remap
    constraint references.
    """
    id_map: dict[str, str] = {}
    new_dims: list[dict] = []
    for dim in dimensions or []:
        old_dim_id = dim.get("id")
        new_dim_id = new_id("ldim")
        if old_dim_id is not None:
            id_map[old_dim_id] = new_dim_id
        new_values = []
        for val in dim.get("values") or []:
            copied_val = copy.deepcopy(val)
            old_val_id = copied_val.get("id")
            new_val_id = new_id("lval")
            if old_val_id is not None:
                id_map[old_val_id] = new_val_id
            copied_val["id"] = new_val_id
            copied_val["source_value_id"] = old_val_id
            new_values.append(copied_val)
        new_dim = copy.deepcopy(dim)
        new_dim["id"] = new_dim_id
        new_dim["source_dimension_id"] = old_dim_id
        new_dim["values"] = new_values
        new_dims.append(new_dim)
    return new_dims, id_map


def _remap_constraints(constraints: list[dict], id_map: dict[str, str]) -> list[dict]:
    """Rewrite constraint dimension/value references to the new product-local IDs."""
    remapped_constraints = []
    for constraint in constraints or []:
        remapped = copy.deepcopy(constraint)
        for key in _CONSTRAINT_DIMENSION_KEYS:
            if key in remapped and remapped[key] in id_map:
                remapped[key] = id_map[remapped[key]]
        for key in _CONSTRAINT_VALUE_KEYS:
            if key in remapped and remapped[key] in id_map:
                remapped[key] = id_map[remapped[key]]
        remapped_constraints.append(remapped)
    return remapped_constraints


def apply_template(
    context: TenantContext,
    template_version_id: str,
    product_entry_id: str,
    *,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
) -> dict:
    """Deep-copy a DimensionTemplateVersion into a product-local DimensionSchema draft (§9.1).

    Product-local dimension/value IDs are freshly minted (never the template's
    own IDs) so later template edits can never reach into an already-applied
    schema; provenance is preserved via ``source_dimension_id``/``source_value_id``.
    """

    def body(session, key_hash):
        _require_workspace(session, context.workspace_id)
        tmpl_ver = _version(session, context, template_version_id)
        if tmpl_ver.entry_type != ENTRY_DIMENSION_TEMPLATE:
            raise LibraryError("引用的版本不是维度模板", code="library_template_invalid")
        product = _entry(session, context, product_entry_id)
        if product.entry_type != ENTRY_PRODUCT:
            raise LibraryError("引用的条目不是产品", code="entry_not_product")
        if product.lifecycle == LIFECYCLE_ARCHIVED:
            raise LibraryError("产品已归档，不能应用维度模板", code="entry_archived")

        tmpl_content = dict(tmpl_ver.content_json or {})
        new_dims, id_map = _copy_template_dimensions(tmpl_content.get("dimensions") or [])
        new_constraints = _remap_constraints(tmpl_content.get("constraints") or [], id_map)

        schema_content = {
            "product_entry_id": product_entry_id,
            "derived_from_template_version_id": template_version_id,
            "dimensions": new_dims,
            "constraints": new_constraints,
            "combination_cardinality": tmpl_content.get("combination_cardinality", "one_per_dimension"),
        }

        entry = LibraryEntryRecord(
            id=new_id("lib"),
            workspace_id=context.workspace_id,
            entry_type=ENTRY_DIMENSION_SCHEMA,
            display_name=derived_display_name(f"Schema for {product.display_name}"),
            lifecycle=LIFECYCLE_ACTIVE,
        )
        session.add(entry)
        session.flush()
        session.add(
            LibraryDraftRecord(
                id=new_id("libdraft"),
                workspace_id=context.workspace_id,
                entry_id=entry.id,
                content_json=schema_content,
                revision=1,
                state=DRAFT_EDITING,
                origin_command=COMMAND_APPLY,
                source_version_id=template_version_id,
            )
        )
        _emit(
            session, context, entry.id, "draft_created", to_state=DRAFT_EDITING,
            payload={"origin": "apply_template", "template_version_id": template_version_id},
        )
        result = _entry_dict(session, context, entry)
        audit_detail = {
            "template_version_id": template_version_id,
            "product_entry_id": product_entry_id,
        }
        if key_hash:
            audit_detail["idempotency_key_sha256"] = key_hash
        _audit(session, context, "apply_template", entry_id=entry.id, detail=audit_detail)
        return result

    return _run_with_idempotency(
        context, idempotency_key, "apply_template", request_fingerprint, body
    )


@dataclass(frozen=True)
class DiffItem:
    """One dimension's three-way diff outcome for :func:`reapply_template` (§9.2)."""

    dimension_id: str
    status: str  # "template_only" | "template_removed" | "local_only" | "locally_removed" | "conflict" | "unchanged"
    template_value: dict | None = None
    local_value: dict | None = None

    def as_dict(self) -> dict:
        return {
            "dimension_id": self.dimension_id,
            "status": self.status,
            "template_value": self.template_value,
            "local_value": self.local_value,
        }


@dataclass
class TemplateDiffReport:
    """Three-way diff between the template version a schema was applied from, the
    template's newest version, and the schema's own (possibly locally-edited)
    content — so a reapply can be reviewed instead of silently overwriting
    local edits (§9.2)."""

    base_template_version_id: str
    new_template_version_id: str
    schema_version_id: str
    items: list[DiffItem] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "base_template_version_id": self.base_template_version_id,
            "new_template_version_id": self.new_template_version_id,
            "schema_version_id": self.schema_version_id,
            "items": [item.as_dict() for item in self.items],
        }


def _normalize_for_diff(dim: dict) -> dict:
    """Strip ID/provenance keys for cross-space hash comparison.

    ``apply_template`` mints fresh product-local IDs (``ldim_*``/``lval_*``)
    and stamps ``source_dimension_id``/``source_value_id`` provenance onto the
    schema copy, so a template-space dimension dict and its schema-space copy
    never hash equal even with zero local edits. Strip those structural keys
    before hashing so the comparison reflects content, not ID space.
    """
    d = {k: v for k, v in dim.items() if k not in ("id", "source_dimension_id")}
    if "values" in d:
        d["values"] = [
            {k: v for k, v in val.items() if k not in ("id", "source_value_id")}
            for val in d["values"]
        ]
    return d


def reapply_template(
    context: TenantContext,
    new_template_version_id: str,
    existing_schema_version_id: str,
    *,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
) -> dict:
    """Three-way diff: base template version, new template version, local schema (§9.2).

    Read-only: produces a :class:`TemplateDiffReport` (as a dict, so the result
    replays identically through the idempotency protocol) for the caller to
    review. It never mutates the schema itself — a follow-up ``save_draft``
    call applies whatever the caller decides to keep.
    """

    def body(session, key_hash):
        _require_workspace(session, context.workspace_id)
        schema_ver = _version(session, context, existing_schema_version_id)
        if schema_ver.entry_type != ENTRY_DIMENSION_SCHEMA:
            raise LibraryError("版本不是维度 Schema", code="library_schema_invalid")
        schema_content = dict(schema_ver.content_json or {})

        base_tmpl_id = schema_content.get("derived_from_template_version_id")
        if not base_tmpl_id:
            raise LibraryError("该 Schema 没有模板来源，无法 Reapply", code="library_template_source_missing")
        base_tmpl = _version(session, context, base_tmpl_id)
        if base_tmpl.entry_type != ENTRY_DIMENSION_TEMPLATE:
            raise LibraryError("Schema 的模板来源已损坏", code="library_template_source_invalid")

        new_tmpl = _version(session, context, new_template_version_id)
        if new_tmpl.entry_type != ENTRY_DIMENSION_TEMPLATE:
            raise LibraryError("新版本不是维度模板", code="library_template_invalid")

        base_content = dict(base_tmpl.content_json or {})
        new_content = dict(new_tmpl.content_json or {})

        base_dims = {d["id"]: d for d in base_content.get("dimensions") or [] if isinstance(d, dict) and "id" in d}
        new_dims = {d["id"]: d for d in new_content.get("dimensions") or [] if isinstance(d, dict) and "id" in d}
        # Local dims are keyed by product-local ID; index them by their template
        # provenance (source_dimension_id) so they line up with base/new template IDs.
        local_by_source: dict[str, dict] = {}
        for d in schema_content.get("dimensions") or []:
            if not isinstance(d, dict):
                continue
            src = d.get("source_dimension_id")
            if src:
                local_by_source[src] = d

        items: list[DiffItem] = []
        for dim_id in sorted(set(base_dims) | set(new_dims)):
            in_base = dim_id in base_dims
            in_new = dim_id in new_dims
            in_local = dim_id in local_by_source

            if in_new and not in_base:
                items.append(DiffItem(dim_id, "template_only", template_value=new_dims[dim_id]))
            elif in_base and not in_new:
                if in_local:
                    items.append(DiffItem(dim_id, "template_removed", local_value=local_by_source[dim_id]))
                # Removed upstream and never locally applied: nothing to report.
            elif in_base and in_new:
                if not in_local:
                    # Removed locally while the template kept it in both base and
                    # new versions — not the same thing as a genuine local-only
                    # addition (those are picked up in the pass below).
                    items.append(DiffItem(dim_id, "locally_removed"))
                    continue
                base_hash = canonical_hash(_normalize_for_diff(base_dims[dim_id]))
                new_hash = canonical_hash(_normalize_for_diff(new_dims[dim_id]))
                local_hash = canonical_hash(_normalize_for_diff(local_by_source[dim_id]))
                if new_hash == local_hash or base_hash == new_hash:
                    # Template did not change, or the local copy already matches
                    # the new template — nothing for the user to reconcile.
                    items.append(DiffItem(dim_id, "unchanged"))
                elif base_hash == local_hash:
                    # Local copy still matches the old base and the template moved:
                    # a clean, non-conflicting update is available.
                    items.append(DiffItem(dim_id, "template_only", template_value=new_dims[dim_id]))
                else:
                    # Both the template and the local copy diverged from the base.
                    items.append(DiffItem(
                        dim_id, "conflict",
                        template_value=new_dims[dim_id], local_value=local_by_source[dim_id],
                    ))

        # Genuine local-only dimensions: added directly on the schema without a
        # template source, so they never appear in base_dims/new_dims and the
        # loop above never visits them.
        template_dim_ids = set(base_dims) | set(new_dims)
        for local_dim in schema_content.get("dimensions") or []:
            if not isinstance(local_dim, dict):
                continue
            src = local_dim.get("source_dimension_id")
            if not src or src not in template_dim_ids:
                items.append(DiffItem(local_dim["id"], "local_only", local_value=local_dim))

        report = TemplateDiffReport(
            base_template_version_id=base_tmpl_id,
            new_template_version_id=new_template_version_id,
            schema_version_id=existing_schema_version_id,
            items=items,
        )
        _audit(
            session, context, "reapply_template",
            entry_id=schema_ver.entry_id,
            detail={
                "new_template_version_id": new_template_version_id,
                "schema_version_id": existing_schema_version_id,
                "diff_summary": {status: sum(1 for it in items if it.status == status)
                                  for status in {it.status for it in items}},
            },
        )
        return report.as_dict()

    return _run_with_idempotency(
        context, idempotency_key, "reapply_template", request_fingerprint, body
    )
