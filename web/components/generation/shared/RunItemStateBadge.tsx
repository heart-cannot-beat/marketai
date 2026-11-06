import { useTranslation } from 'next-i18next';

/**
 * Shared between `RunItemGrid`/`RunItemDetail` (a `RunItemRecord.state`,
 * `app/adapters/persistence/models.py`: `ck_run_item_state`) and
 * `RunList`/`RunSummary` (a `GenerationRunRecord.status`, derived by
 * `derive_run_state` in `app/generation/run.py`) -- the two vocabularies
 * overlap but aren't identical, so this covers the union of both:
 * RunItem states (`pending`, `queued`, `running`, `qc_pending`, `succeeded`,
 * `reused`, `failed`, `cancelled`, `paused`, `uncertain`) plus the
 * Run-only `partially_succeeded`.
 */
const STATE_CLASS: Record<string, string> = {
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

interface RunItemStateBadgeProps {
  state: string;
}

export function RunItemStateBadge({ state }: RunItemStateBadgeProps) {
  const { t } = useTranslation('common');
  const className = STATE_CLASS[state] ?? 'badge-ghost';
  // Falls back to the raw state string for any value the domain adds later
  // that this badge hasn't been taught a label for yet.
  const label = t(`generation-run-state-${state}`, { defaultValue: state });

  return (
    <span className={`badge badge-sm ${className}`} aria-label={label}>
      {label}
    </span>
  );
}
