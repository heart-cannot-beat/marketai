"""Provision a new Workspace's SpendGuardrail at the Beta defaults (#126).

``marketai_apply_workspace_projection_v2`` (last defined in ``zs4f5a6b7c8d``)
created the guardrail with every limit at 0. ``ReservationBook`` refuses any
reservation above a limit, so a newly provisioned Workspace could make no paid
call at all. The function now creates it at the Beta defaults of
docs/architecture/subscription-entitlement-usage-cost-ownership.md §11
(``app.entitlements.guardrail_defaults.SPEND_GUARDRAIL_DEFAULTS_V1``: 5 per
Operation, 20 per rolling 24 hours, 100 per period, 10 outstanding), in the
Workspace's budget currency.

Only the guardrail INSERT changes, and it keeps ``ON CONFLICT DO NOTHING``: a
redelivered or later snapshot never touches an existing row, whether its Owner
edited it or provisioning wrote it at 0 before this migration. Existing rows
are not backfilled (#63); their Owner raises them in Settings through the
guardrail preview and confirm.

The function stays SECURITY DEFINER with ``search_path = ''`` and
schema-qualified names. ``CREATE OR REPLACE`` keeps its owner and ACL, so the
grants in deploy/postgres/domain-tenant-roles.sql do not change.
"""
from alembic import op

from app.entitlements.guardrail_defaults import SPEND_GUARDRAIL_DEFAULTS_V1 as DEFAULTS

revision = "zzg126a1b2c3"
down_revision = "zzf6b7c8d9e0"
branch_labels = None
depends_on = None

#: The guardrail INSERT the previous definition (zs4f5a6b7c8d) ran; the
#: downgrade puts it back.
PREVIOUS_GUARDRAIL_INSERT = """    -- A new tenant starts with paid calls disabled until its Owner sets limits.
    INSERT INTO public.spend_guardrails (workspace_id, revision, budget_currency,
        per_operation_limit, rolling_24h_limit, period_limit, outstanding_exposure_limit,
        platform_policy_version, created_at, updated_at, updated_by)
    VALUES (p_workspace_id, 1, 'USD', 0, 0, 0, 0, 1, now(), now(), 'system:provisioning')
    ON CONFLICT (workspace_id) DO NOTHING;
"""

DEFAULTS_GUARDRAIL_INSERT = """    -- A new tenant starts at the contract's Beta guardrail, in its budget
    -- currency (#126). An existing guardrail is never rewritten: not one its
    -- Owner edited, and not one an earlier version provisioned at 0.
    INSERT INTO public.spend_guardrails (workspace_id, revision, budget_currency,
        per_operation_limit, rolling_24h_limit, period_limit, outstanding_exposure_limit,
        platform_policy_version, created_at, updated_at, updated_by)
    SELECT w.id, 1, w.budget_currency, __PER_OPERATION__, __ROLLING_24H__, __PERIOD__,
        __OUTSTANDING__, __POLICY_VERSION__, now(), now(), 'system:provisioning'
    FROM public.workspaces AS w WHERE w.id = p_workspace_id
    ON CONFLICT (workspace_id) DO NOTHING;
"""


GUARDRAIL_INSERT = (
    DEFAULTS_GUARDRAIL_INSERT
    .replace("__PER_OPERATION__", str(DEFAULTS.per_operation_limit))
    .replace("__ROLLING_24H__", str(DEFAULTS.rolling_24h_limit))
    .replace("__PERIOD__", str(DEFAULTS.period_limit))
    .replace("__OUTSTANDING__", str(DEFAULTS.outstanding_exposure_limit))
    .replace("__POLICY_VERSION__", str(int(DEFAULTS.platform_policy_version)))
)

#: ``marketai_apply_workspace_projection_v2`` as zs4f5a6b7c8d left it, with
#: the guardrail INSERT in a placeholder.
FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION marketai_apply_workspace_projection_v2(
    p_snapshot_id TEXT, p_workspace_id TEXT, p_workspace_name TEXT,
    p_authorization_epoch INT, p_entitlement_version INT, p_lease_sequence INT,
    p_projection_version INT, p_lifecycle TEXT, p_suspended BOOLEAN,
    p_schema_version INT, p_delivery_kind TEXT, p_canonical_envelope BYTEA,
    p_envelope_sha256 TEXT, p_semantic_hash TEXT, p_signature TEXT,
    p_signature_key_id TEXT, p_signature_algorithm TEXT, p_plan_version_id TEXT,
    p_source_refs_json JSONB, p_period_start TIMESTAMPTZ, p_period_end TIMESTAMPTZ,
    p_period_anchor TIMESTAMPTZ, p_resolved_entitlements_json JSONB,
    p_restriction_summary_json JSONB, p_entitlement_registry_version INT,
    p_resolution_policy_version INT, p_platform_limits_version INT,
    p_issued_at TIMESTAMPTZ, p_valid_until TIMESTAMPTZ,
    p_control_plane_audit_event_id TEXT
) RETURNS TEXT SECURITY DEFINER SET search_path = '' LANGUAGE plpgsql AS $$
DECLARE
    v_current_ev INT; v_current_ls INT; v_current_pv INT;
    v_existing_sha TEXT; v_result TEXT;
BEGIN
    -- Bootstrap only from an authenticated, verified control-plane envelope.
    -- Serialize first delivery even before a projection row exists.
    PERFORM pg_advisory_xact_lock(hashtextextended(p_workspace_id, 7251));
    INSERT INTO public.workspaces (id, name, default_currency, authorization_epoch,
        entitlement_version, suspended, projection_version, lifecycle, budget_currency, created_at)
    VALUES (p_workspace_id, p_workspace_name, 'USD', p_authorization_epoch,
        p_entitlement_version, p_suspended, p_projection_version, p_lifecycle, 'USD', now())
    ON CONFLICT (id) DO NOTHING;
__GUARDRAIL_INSERT__
    SELECT entitlement_version, lease_sequence, projection_version
    INTO v_current_ev, v_current_ls, v_current_pv
    FROM public.entitlement_projections WHERE workspace_id = p_workspace_id FOR UPDATE;

    IF NOT FOUND THEN v_result := 'applied';
    ELSIF p_entitlement_version < v_current_ev THEN v_result := 'stale';
    ELSIF p_entitlement_version = v_current_ev AND p_lease_sequence <= v_current_ls THEN
        SELECT envelope_sha256 INTO v_existing_sha FROM public.entitlement_snapshot_inbox
        WHERE workspace_id = p_workspace_id AND entitlement_version = p_entitlement_version
          AND lease_sequence = p_lease_sequence;
        IF FOUND AND v_existing_sha != p_envelope_sha256 THEN v_result := 'conflict';
        ELSE v_result := 'duplicate'; END IF;
    ELSIF p_entitlement_version = v_current_ev AND p_lease_sequence > v_current_ls THEN
        IF p_semantic_hash != (SELECT semantic_hash FROM public.entitlement_projections WHERE workspace_id = p_workspace_id) THEN
            v_result := 'conflict';
        ELSE v_result := 'applied'; END IF;
    ELSIF p_entitlement_version = v_current_ev + 1 THEN v_result := 'applied';
    ELSIF p_delivery_kind = 'full_repair' THEN v_result := 'applied';
    ELSE v_result := 'version_jump_paused'; END IF;

    INSERT INTO public.entitlement_snapshot_inbox (
        snapshot_id, workspace_id, entitlement_version, lease_sequence,
        projection_version, schema_version, delivery_kind,
        canonical_envelope, envelope_sha256, semantic_hash,
        signature, signature_key_id, signature_algorithm,
        outcome, received_at, processed_at, projection_applied_at
    ) VALUES (
        p_snapshot_id, p_workspace_id, p_entitlement_version, p_lease_sequence,
        p_projection_version, p_schema_version, p_delivery_kind,
        p_canonical_envelope, p_envelope_sha256, p_semantic_hash,
        p_signature, p_signature_key_id, p_signature_algorithm,
        v_result, now(), now(),
        CASE WHEN v_result = 'applied' THEN now() ELSE NULL END
    ) ON CONFLICT (workspace_id, entitlement_version, lease_sequence) DO NOTHING;

    IF v_result = 'applied' THEN
        INSERT INTO public.entitlement_projections (
            workspace_id, current_snapshot_id, entitlement_version, lease_sequence,
            projection_version, schema_version, plan_version_id, source_refs_json,
            period_start, period_end, period_anchor, resolved_entitlements_json,
            restriction_summary_json, semantic_hash, entitlement_registry_version,
            resolution_policy_version, platform_limits_version, issued_at, valid_until,
            control_plane_audit_event_id, signature, signature_key_id,
            signature_algorithm, applied_at
        ) VALUES (
            p_workspace_id, p_snapshot_id, p_entitlement_version, p_lease_sequence,
            p_projection_version, p_schema_version, p_plan_version_id, p_source_refs_json,
            p_period_start, p_period_end, p_period_anchor, p_resolved_entitlements_json,
            p_restriction_summary_json, p_semantic_hash, p_entitlement_registry_version,
            p_resolution_policy_version, p_platform_limits_version, p_issued_at, p_valid_until,
            p_control_plane_audit_event_id, p_signature, p_signature_key_id,
            p_signature_algorithm, now()
        ) ON CONFLICT (workspace_id) DO UPDATE SET
            current_snapshot_id = EXCLUDED.current_snapshot_id,
            entitlement_version = EXCLUDED.entitlement_version,
            lease_sequence = EXCLUDED.lease_sequence,
            projection_version = EXCLUDED.projection_version,
            schema_version = EXCLUDED.schema_version,
            plan_version_id = EXCLUDED.plan_version_id,
            source_refs_json = EXCLUDED.source_refs_json,
            period_start = EXCLUDED.period_start, period_end = EXCLUDED.period_end,
            period_anchor = EXCLUDED.period_anchor,
            resolved_entitlements_json = EXCLUDED.resolved_entitlements_json,
            restriction_summary_json = EXCLUDED.restriction_summary_json,
            semantic_hash = EXCLUDED.semantic_hash,
            entitlement_registry_version = EXCLUDED.entitlement_registry_version,
            resolution_policy_version = EXCLUDED.resolution_policy_version,
            platform_limits_version = EXCLUDED.platform_limits_version,
            issued_at = EXCLUDED.issued_at, valid_until = EXCLUDED.valid_until,
            control_plane_audit_event_id = EXCLUDED.control_plane_audit_event_id,
            signature = EXCLUDED.signature, signature_key_id = EXCLUDED.signature_key_id,
            signature_algorithm = EXCLUDED.signature_algorithm, applied_at = now();

        UPDATE public.workspaces SET
            name = p_workspace_name,
            authorization_epoch = GREATEST(authorization_epoch, p_authorization_epoch),
            entitlement_version = p_entitlement_version,
            suspended = p_suspended, lifecycle = p_lifecycle,
            projection_version = p_projection_version
        WHERE id = p_workspace_id;
    END IF;
    RETURN v_result;
END;
$$;
"""


def _function(guardrail_insert: str) -> str:
    assert FUNCTION_SQL.count("__GUARDRAIL_INSERT__\n") == 1
    return FUNCTION_SQL.replace("__GUARDRAIL_INSERT__\n", guardrail_insert, 1)


SQL = _function(GUARDRAIL_INSERT)


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute(SQL)


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_function(PREVIOUS_GUARDRAIL_INSERT))
