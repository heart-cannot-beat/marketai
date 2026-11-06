/**
 * Display labels for the raw enums the asset, review and QC screens show
 * (Issue #191, front-flow contract §1 principle 2 / §8: the front never
 * shows an internal enum or id, and its copy is Chinese).
 *
 * Every mapping returns an i18n key in the `common` namespace (loaded by
 * every Workspace page, including the advanced Run page that reuses these
 * components) and a DaisyUI badge class. A value the domain adds later
 * falls back to a neutral "unknown" label -- never the raw string.
 */

export interface StatusDisplay {
  /** i18n key in the `common` namespace. */
  labelKey: string;
  /** DaisyUI badge colour class. */
  badgeClass: string;
}

function lookup(
  table: Record<string, string>,
  prefix: string,
  value: string
): StatusDisplay {
  const badgeClass = table[value];
  if (badgeClass === undefined) {
    return { labelKey: `${prefix}-unknown`, badgeClass: 'badge-ghost' };
  }
  return { labelKey: `${prefix}-${value}`, badgeClass };
}

/** `quality_reviews.review_value` (`ck_quality_review_review_value`):
 * approved / needs_changes / rejected / cleared. No value or `cleared`
 * reads 未审核. */
const REVIEW_BADGE: Record<string, string> = {
  approved: 'badge-success',
  needs_changes: 'badge-warning',
  rejected: 'badge-error',
  unreviewed: 'badge-ghost',
};

export function reviewStatus(value: string | null | undefined): StatusDisplay {
  const normalized = !value || value === 'cleared' ? 'unreviewed' : value;
  return lookup(REVIEW_BADGE, 'status-review', normalized);
}

/** QC attempt states (`ck_asset_qc_attempt_state`) plus the per-check
 * `skipped` and the legacy `failed`. */
const QC_BADGE: Record<string, string> = {
  passed: 'badge-success',
  failed_blocking: 'badge-error',
  failed_nonblocking: 'badge-error',
  failed: 'badge-error',
  uncertain: 'badge-warning',
  skipped: 'badge-ghost',
  pending: 'badge-ghost',
  running: 'badge-info',
};

export function qcStatus(state: string | null | undefined): StatusDisplay {
  return lookup(QC_BADGE, 'status-qc', state ?? '');
}

/** Run-level status (`derive_run_state`, `app/generation/run.py`) and the
 * RunItem states it folds. */
const RUN_BADGE: Record<string, string> = {
  pending: 'badge-ghost',
  queued: 'badge-ghost',
  running: 'badge-info',
  qc_pending: 'badge-info',
  succeeded: 'badge-success',
  reused: 'badge-success',
  partially_succeeded: 'badge-warning',
  paused: 'badge-warning',
  uncertain: 'badge-warning',
  failed: 'badge-error',
  cancelled: 'badge-ghost',
};

export function runStatus(status: string | null | undefined): StatusDisplay {
  return lookup(RUN_BADGE, 'status-run', status ?? '');
}

/** Generation execution modes (`byok` / `sandbox` / `mock`, and the
 * persisted alias `controlled_mock`) -> the badge's label key. An unknown
 * mode reads as 真实, matching `ExecutionModeTag`'s historical fallback. */
export type ExecutionModeKind = 'real' | 'sandbox' | 'controlled-mock';

export function executionModeKind(mode: string | null | undefined) {
  if (mode === 'sandbox') return 'sandbox' as const;
  if (mode === 'mock' || mode === 'controlled_mock') {
    return 'controlled-mock' as const;
  }
  return 'real' as const;
}

export function executionModeLabelKey(mode: string | null | undefined) {
  return `execution-mode-${executionModeKind(mode)}`;
}

/** `asset_versions.origin_kind` (`ck_asset_version_origin_kind`). */
const ORIGIN_KINDS = new Set([
  'generation',
  'upload',
  'reuse',
  'legacy',
  'localized',
]);

export function originLabelKey(origin: string | null | undefined): string {
  return origin && ORIGIN_KINDS.has(origin)
    ? `status-origin-${origin}`
    : 'status-origin-unknown';
}

/** Contextual experiment conclusion verdicts (winner / learning / loser). */
const VERDICT_BADGE: Record<string, string> = {
  winner: 'badge-success',
  learning: 'badge-ghost',
  loser: 'badge-error',
};

export function verdictStatus(
  verdict: string | null | undefined
): StatusDisplay {
  return lookup(VERDICT_BADGE, 'status-verdict', verdict ?? '');
}

/** Export package states (`ck_export_package_state`). */
const EXPORT_STATE_BADGE: Record<string, string> = {
  pending: 'badge-ghost',
  building: 'badge-info',
  ready: 'badge-success',
  failed: 'badge-error',
  expired: 'badge-warning',
};

export function exportPackageStatus(
  state: string | null | undefined
): StatusDisplay {
  return lookup(EXPORT_STATE_BADGE, 'status-export', state ?? '');
}

/** Creation time as `MM/DD HH:mm` in the viewer's time zone; `null` for a
 * missing or unparseable timestamp. */
export function formatRunTime(value: string | null | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}`;
}
