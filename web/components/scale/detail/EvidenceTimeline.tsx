import type { ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import { formatDateTime } from '@/lib/i18n/format';
import { useScaleEnrollmentEvidence } from '@/hooks/scale/useScaleEvidence';
import type { EnrollmentEvidence } from '@/hooks/scale/useScaleEvidence';
import { HealthBadge } from '@/components/scale/shared/HealthBadge';
import { MaturityIndicator } from '@/components/scale/shared/MaturityIndicator';
import { evidenceView } from '@/lib/scale/evidence-view';
import type { EvidenceView } from '@/lib/scale/evidence-view';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';

interface EvidenceTimelineProps {
  enrollmentId: string;
  metaMode: ScaleMetaMode;
}

type Readiness = NonNullable<EnrollmentEvidence['readiness']>;
type QualityIssue = Readiness['issues'][number];

/** The accepted Evaluation's fields are null together until the first
 * Evaluation projects, and set together after (#114); `health` and
 * `target_version_id` stay nullable on their own. */
type EvaluatedEvidence = {
  [K in keyof EnrollmentEvidence]: K extends
    | 'readiness'
    | 'health'
    | 'target_version_id'
    ? EnrollmentEvidence[K]
    : NonNullable<EnrollmentEvidence[K]>;
};

const evaluatedOf = (evidence: EnrollmentEvidence) =>
  evidence.evaluation_id !== null ? (evidence as EvaluatedEvidence) : null;

function QualityBadge({ state }: { state: string }) {
  const { t } = useTranslation('scale');
  return (
    <span
      className={`badge badge-sm ${
        state === 'blocked'
          ? 'badge-error'
          : state === 'warning'
            ? 'badge-warning'
            : 'badge-success'
      }`}
    >
      {t(`evidence.quality.${state}`, { defaultValue: state })}
    </span>
  );
}

/** A source manifest's typed issues (`evidence.source-issue.<code>`, the
 * collector's codes), worded like the snapshot's findings below. */
function SourceIssues({ issues }: { issues: QualityIssue[] }) {
  const { t } = useTranslation('scale');
  if (issues.length === 0) return null;
  return (
    <ul className="mt-2 space-y-2">
      {issues.map((issue, index) => (
        <li
          key={`${issue.code}-${index}`}
          className={`rounded-md border bg-base-100 p-3 text-sm ${
            issue.is_blocking ? 'border-error/40' : 'border-warning/40'
          }`}
        >
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">
              {t(`evidence.source-issue.${issue.code}`, {
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
          <details className="mt-1 text-xs">
            <summary className="cursor-pointer text-base-content/60">
              {t('evidence.technical-details')}
            </summary>
            <p className="mt-1 break-all font-mono">
              {[issue.code, issue.contract_type, issue.segment_key]
                .filter(Boolean)
                .join(' · ')}
            </p>
            {issue.detail && <p className="mt-1 break-words">{issue.detail}</p>}
          </details>
        </li>
      ))}
    </ul>
  );
}

/**
 * The state the Episode's evidence is in (contract §22): waiting for the
 * first complete day, first day collected, blocked (with its reasons and
 * recovery action; stale when an older Evaluation is shown below), or a
 * newer day collected while its evaluation runs. Nothing for an Evaluation
 * that is current -- its own badges and reasons say the rest.
 */
function StateNotice({
  view,
  readiness,
  evaluatedDay,
}: {
  view: EvidenceView;
  readiness: Readiness | null;
  evaluatedDay: string | null;
}) {
  const { t } = useTranslation('scale');
  let tone = 'border-base-300 bg-base-200';
  let title: string;
  let body: ReactNode = null;
  switch (view.kind) {
    case 'waiting':
      title = t('evidence.state.waiting-title');
      body = <p>{t('evidence.state.waiting-body')}</p>;
      break;
    case 'collected':
      title = t('evidence.state.collected-title');
      body = <p>{t('evidence.state.collected-body', { day: view.day })}</p>;
      break;
    case 'blocked':
      tone = 'border-error/40 bg-error/5';
      title = t('evidence.state.blocked-title');
      body = (
        <>
          <p>
            {t('evidence.state.blocked-body', {
              day: readiness?.target_local_day,
            })}
          </p>
          {view.stale && evaluatedDay && (
            <p>{t('evidence.state.stale-body', { day: evaluatedDay })}</p>
          )}
          {readiness?.last_complete_local_day && (
            <p className="text-base-content/70">
              {t('evidence.state.last-complete-day', {
                day: readiness.last_complete_local_day,
              })}
            </p>
          )}
          {readiness?.recovery_action && (
            <p className="font-medium">
              {t('evidence.state.what-to-do')}{' '}
              {t(`evidence.recovery.${readiness.recovery_action}`, {
                defaultValue: readiness.recovery_action,
              })}
            </p>
          )}
        </>
      );
      break;
    case 'evaluated':
      if (!view.pending) return null;
      title = t('evidence.state.pending-title');
      body = (
        <p>
          {t('evidence.state.pending-body', {
            day:
              readiness?.last_complete_local_day ?? readiness?.target_local_day,
            evaluated: evaluatedDay,
          })}
        </p>
      );
      break;
  }
  const showIssues = readiness && (view.kind !== 'evaluated' || view.pending);
  return (
    <div
      role={view.kind === 'blocked' ? 'alert' : 'status'}
      className={`space-y-1 rounded-md border p-3 text-sm ${tone}`}
    >
      <p className="font-semibold">{title}</p>
      {body}
      {showIssues && <SourceIssues issues={readiness.issues} />}
    </div>
  );
}

/**
 * Evidence condition axis (spec §4.4, contract §22, #114): the Episode's
 * evidence readiness -- what the source collector last made of Meta's data
 * -- and the accepted Evaluation's maturity, health, windows, findings and
 * signals. An activated Enrollment answers 200 before anything is collected
 * (waiting for the first complete day); a blocked manifest shows its typed
 * reasons and recovery action, and an older Evaluation below it is shown as
 * out of date with no current health (contract §5, §8). Only an Enrollment
 * without an active Episode answers 404 `no_active_episode` (the empty
 * state).
 */
export function EvidenceTimeline({
  enrollmentId,
  metaMode,
}: EvidenceTimelineProps) {
  const { t } = useTranslation('scale');
  const { data, problem, isLoading, isStale, refresh } =
    useScaleEnrollmentEvidence(enrollmentId, metaMode);

  if (isLoading) {
    return (
      <div className="mt-2 flex justify-center" aria-busy="true">
        <span className="loading loading-spinner loading-sm" />
      </div>
    );
  }

  if (!data) {
    return (
      <p className="mt-2 text-sm text-base-content/60">
        {problem?.code === 'no_active_episode'
          ? t('evidence.no-active-episode')
          : t('evidence.unavailable')}
      </p>
    );
  }

  const view = evidenceView(data);
  const readiness = data.readiness;
  const evidence = evaluatedOf(data);
  const blocked = view.kind === 'blocked';

  return (
    <div className="mt-2 space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          {evidence && !blocked ? (
            <>
              <MaturityIndicator maturity={evidence.maturity} />
              <HealthBadge
                health={evidence.health}
                reasonCode={evidence.health_reason}
              />
              <QualityBadge state={evidence.data_quality_state} />
              <span className="text-xs text-base-content/60">
                {t('evidence.as-of', { day: evidence.cursor_as_of_local_day })}
              </span>
            </>
          ) : (
            readiness && (
              <>
                <MaturityIndicator maturity={readiness.maturity} />
                <QualityBadge state={readiness.quality_state} />
                {readiness.target_local_day && (
                  <span className="text-xs text-base-content/60">
                    {t('evidence.as-of', { day: readiness.target_local_day })}
                  </span>
                )}
              </>
            )
          )}
        </div>
        <button
          type="button"
          className="btn btn-ghost btn-xs"
          onClick={() => refresh()}
        >
          {isStale ? t('evidence.refresh-stale') : t('evidence.refresh')}
        </button>
      </div>

      <StateNotice
        view={view}
        readiness={readiness}
        evaluatedDay={evidence?.cursor_as_of_local_day ?? null}
      />

      {evidence && (
        <EvaluationDetails
          evidence={evidence}
          historical={blocked || (view.kind === 'evaluated' && view.pending)}
        />
      )}
    </div>
  );
}

/** The accepted Evaluation; `historical` when a newer manifest -- blocked,
 * or collected and still being evaluated -- has moved past it. */
function EvaluationDetails({
  evidence,
  historical,
}: {
  evidence: EvaluatedEvidence;
  historical: boolean;
}) {
  const { t, i18n } = useTranslation('scale');
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
    <div className="space-y-4">
      {historical && (
        <h4 className="text-xs font-semibold text-base-content/60">
          {t('evidence.last-evaluation', {
            day: evidence.cursor_as_of_local_day,
          })}
        </h4>
      )}

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
