import { useTranslation } from 'next-i18next';
import { formatDateTime } from '@/lib/i18n/format';
import { useScaleEnrollmentEvidence } from '@/hooks/scale/useScaleEvidence';
import { HealthBadge } from '@/components/scale/shared/HealthBadge';
import { MaturityIndicator } from '@/components/scale/shared/MaturityIndicator';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';

interface EvidenceTimelineProps {
  enrollmentId: string;
  metaMode: ScaleMetaMode;
}

/**
 * Evidence condition axis (spec §4.4): the accepted-Evaluation projection's
 * freshness (`cursor_as_of_local_day`/`updated_at`), maturity, health, and
 * streak counters -- independent of the Enrollment's lifecycle and the
 * Episode's phase (spec §3.4 step 3/4). A 404 here (`no_active_episode` or
 * `evidence_not_available`) is a normal "no evidence yet" state, not an
 * error -- a freshly-activated Enrollment has no accepted Evaluation until
 * the scheduler/Worker pipeline seals and projects its first one.
 */
export function EvidenceTimeline({
  enrollmentId,
  metaMode,
}: EvidenceTimelineProps) {
  const { t, i18n } = useTranslation('scale');
  const {
    data: evidence,
    problem,
    isLoading,
    isStale,
    refresh,
  } = useScaleEnrollmentEvidence(enrollmentId, metaMode);

  if (isLoading) {
    return (
      <div className="mt-2 flex justify-center" aria-busy="true">
        <span className="loading loading-spinner loading-sm" />
      </div>
    );
  }

  if (!evidence) {
    return (
      <p className="mt-2 text-sm text-base-content/60">
        {problem?.status === 404
          ? t('evidence.no-evidence-yet')
          : t('evidence.unavailable')}
      </p>
    );
  }

  const streaks = Array.isArray(evidence.streak_counters)
    ? evidence.streak_counters
    : [];

  const windows = [
    evidence.current_window,
    evidence.baseline_window,
    evidence.rolling_window,
  ];

  const formatValue = (value: string | null) => value ?? '—';
  const formatInterval = (lower: string | null, upper: string | null) =>
    lower !== null && upper !== null ? `[${lower}, ${upper}]` : '—';

  return (
    <div className="mt-2 space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <MaturityIndicator maturity={evidence.maturity} />
          <HealthBadge
            health={evidence.health}
            reasonCode={evidence.health_reason}
          />
          <span
            className={`badge badge-sm ${
              evidence.data_quality_state === 'blocked'
                ? 'badge-error'
                : evidence.data_quality_state === 'warning'
                  ? 'badge-warning'
                  : 'badge-success'
            }`}
          >
            {t(`evidence.quality.${evidence.data_quality_state}`, {
              defaultValue: evidence.data_quality_state,
            })}
          </span>
          <span className="text-xs text-base-content/60">
            {t('evidence.as-of', { day: evidence.cursor_as_of_local_day })}
          </span>
        </div>
        <button
          type="button"
          className="btn btn-ghost btn-xs"
          onClick={() => refresh()}
        >
          {isStale ? t('evidence.refresh-stale') : t('evidence.refresh')}
        </button>
      </div>

      {evidence.health_reason && (
        <p className="text-sm text-base-content/70">
          {t(`evidence.health-reason.${evidence.health_reason}`, {
            defaultValue: evidence.health_reason,
            // `episode_window_incomplete` says how many days are in so far.
            days: evidence.complete_local_days_in_episode,
          })}
        </p>
      )}

      <dl className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
        <div>
          <dt className="text-xs text-base-content/60">
            {t('evidence.healthy-evaluations')}
          </dt>
          <dd>{evidence.healthy_evaluations}</dd>
        </div>
        <div>
          <dt className="text-xs text-base-content/60">
            {t('evidence.cursor-revision')}
          </dt>
          <dd>{evidence.cursor_source_revision}</dd>
        </div>
        <div>
          <dt className="text-xs text-base-content/60">
            {t('evidence.watermark')}
          </dt>
          <dd>{evidence.safe_watermark}</dd>
        </div>
        <div>
          <dt className="text-xs text-base-content/60">
            {t('evidence.updated-at')}
          </dt>
          <dd>{formatDateTime(i18n.language, evidence.updated_at)}</dd>
        </div>
      </dl>

      <div>
        <h4 className="text-xs font-semibold text-base-content/60">
          {t('evidence.windows-heading')}
        </h4>
        <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-3">
          {windows.map((window) => (
            <div key={window.role} className="rounded-md bg-base-200 p-3">
              <p className="text-xs font-semibold">
                {t(`evidence.window.${window.role}`, {
                  defaultValue: window.role,
                })}
              </p>
              <p className="mt-1 text-sm tabular-nums">
                {window.start_local_day} → {window.end_local_day}
              </p>
              <p className="text-xs text-base-content/60">
                {t('evidence.end-exclusive')}
              </p>
            </div>
          ))}
        </div>
      </div>

      <dl className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-2 lg:grid-cols-3">
        <div>
          <dt className="text-xs text-base-content/60">
            {t('evidence.policy-version')}
          </dt>
          <dd className="break-all font-mono text-xs">
            {evidence.policy_version_id}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-base-content/60">
            {t('evidence.target-version')}
          </dt>
          <dd className="break-all font-mono text-xs">
            {evidence.target_version_id ?? t('evidence.no-target-version')}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-base-content/60">
            {t('evidence.algorithm-version')}
          </dt>
          <dd className="font-mono text-xs">{evidence.algorithm_version}</dd>
        </div>
        <div>
          <dt className="text-xs text-base-content/60">
            {t('evidence.metric-contract')}
          </dt>
          <dd className="break-all font-mono text-xs">
            {evidence.metric_semantic_contract_set_hash}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-base-content/60">
            {t('evidence.delivery-days')}
          </dt>
          <dd>
            {evidence.active_delivery_days_in_episode} /{' '}
            {evidence.complete_local_days_in_episode}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-base-content/60">
            {t('evidence.last-sync')}
          </dt>
          <dd>{formatDateTime(i18n.language, evidence.last_sync_at)}</dd>
        </div>
      </dl>

      {evidence.quality_issues.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-base-content/60">
            {t('evidence.quality-issues-heading')}
          </h4>
          <ul className="mt-2 space-y-2">
            {evidence.quality_issues.map((issue, index) => (
              <li
                key={`${issue.code}-${index}`}
                className={`rounded-md border p-3 text-sm ${
                  issue.is_blocking ? 'border-error/40' : 'border-warning/40'
                }`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">
                    {t(`evidence.quality-issue.${issue.code}`, {
                      defaultValue: issue.code,
                    })}
                  </span>
                  <span
                    className={`badge badge-xs ${
                      issue.is_blocking ? 'badge-error' : 'badge-warning'
                    }`}
                  >
                    {t(`evidence.quality-severity.${issue.severity}`, {
                      defaultValue: issue.severity,
                    })}
                  </span>
                  {issue.window_role && (
                    <span className="text-xs text-base-content/60">
                      {t(`evidence.window.${issue.window_role}`, {
                        defaultValue: issue.window_role,
                      })}
                    </span>
                  )}
                  {issue.local_day && (
                    <span className="text-xs tabular-nums text-base-content/60">
                      {issue.local_day}
                    </span>
                  )}
                </div>
                {/* The code, the contract and segment it points at, and any
                    detail are for support, not the sentence (#384). */}
                <details className="mt-1 text-xs">
                  <summary className="cursor-pointer text-base-content/60">
                    {t('evidence.technical-details')}
                  </summary>
                  <p className="mt-1 break-all font-mono">
                    {[issue.code, issue.contract_type, issue.segment_key]
                      .filter(Boolean)
                      .join(' · ')}
                  </p>
                  {issue.detail && (
                    <p className="mt-1 break-words">{issue.detail}</p>
                  )}
                  {issue.recovery_action && (
                    <p className="mt-1 text-base-content/70">
                      {t('evidence.recovery-action', {
                        action: issue.recovery_action,
                      })}
                    </p>
                  )}
                </details>
              </li>
            ))}
          </ul>
        </div>
      )}

      {evidence.signals.length > 0 && (
        <div className="overflow-x-auto">
          <h4 className="text-xs font-semibold text-base-content/60">
            {t('evidence.signals-heading')}
          </h4>
          <table className="table table-xs mt-2">
            <thead>
              <tr>
                <th>{t('evidence.signal')}</th>
                <th>{t('evidence.signal-state')}</th>
                <th>{t('evidence.observed')}</th>
                <th>{t('evidence.interval')}</th>
                <th>{t('evidence.threshold')}</th>
                <th>{t('evidence.reason')}</th>
              </tr>
            </thead>
            <tbody>
              {evidence.signals.map((signal) => (
                <tr key={signal.signal_id}>
                  <td className="font-mono text-xs">{signal.signal_id}</td>
                  <td>
                    {t(`evidence.signal-states.${signal.state}`, {
                      defaultValue: signal.state,
                    })}
                  </td>
                  <td className="tabular-nums">
                    {formatValue(signal.observed)}
                  </td>
                  <td className="tabular-nums">
                    {formatInterval(
                      signal.interval_lower,
                      signal.interval_upper
                    )}
                  </td>
                  <td className="tabular-nums">
                    {formatValue(signal.threshold)}
                  </td>
                  <td>
                    {/* `ok`: computed the normal way; the row says the rest. */}
                    {!signal.reason_code || signal.reason_code === 'ok'
                      ? '—'
                      : t(`evidence.signal-reason.${signal.reason_code}`, {
                          defaultValue: signal.reason_code,
                        })}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {streaks.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-base-content/60">
            {t('evidence.streak-counters')}
          </h4>
          <ul className="mt-1 space-y-1 text-sm">
            {streaks.map((entry, index) => (
              <li key={index} className="rounded bg-base-200 px-2 py-1">
                {String(entry.signal_key ?? t('evidence.unknown-signal'))}:{' '}
                {String(entry.consecutive_hits ?? 0)}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
