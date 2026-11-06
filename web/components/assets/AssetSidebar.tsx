import { useMemo } from 'react';
import { useTranslation } from 'next-i18next';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import {
  originLabelKey,
  qcStatus,
  reviewStatus,
  verdictStatus,
} from '@/lib/assets/status-labels';
import { InternalView } from './InternalView';
import { VersionHistory } from './VersionHistory';

type GeneratedAssetVersionDetail = components['schemas']['AssetVersionDetail'];

interface ContextualConclusion {
  decision_id?: string;
  verdict?: string;
  context?: {
    context_id?: string;
    experiment_id?: string;
  };
}

export type AssetVersionDetail = Omit<
  GeneratedAssetVersionDetail,
  'contextual_conclusions'
> & {
  contextual_conclusions?: ContextualConclusion[];
};

interface AssetSidebarProps {
  detail: AssetVersionDetail;
  onNavigateVersion: (versionId: string) => void;
}

const HEADING =
  'text-xs font-semibold uppercase tracking-wide text-base-content/50 mb-1';

export function AssetSidebar({ detail, onNavigateVersion }: AssetSidebarProps) {
  const { t } = useTranslation('assets');
  const { t: tc } = useTranslation('common');
  const tags = useMemo(() => Object.entries(detail.tags ?? {}), [detail.tags]);
  const reviews = detail.reviews ?? [];
  const qcAttempts = detail.qc_attempts ?? [];
  const conclusions = detail.contextual_conclusions ?? [];
  const effectiveReview = reviewStatus(detail.effective_review_value);
  const seedReason = detail.seed.unavailable_reason;

  return (
    <div className="flex flex-col gap-4 p-4 overflow-y-auto">
      <section>
        <h3 className={HEADING}>{t('provenance-heading')}</h3>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          <span className="text-base-content/60">{t('provider-label')}</span>
          <span>{detail.provider}</span>
          <span className="text-base-content/60">{t('model-label')}</span>
          <span>{detail.model}</span>
          <span className="text-base-content/60">{t('mode-label')}</span>
          <ExecutionModeTag mode={detail.execution_mode ?? 'unknown'} />
          <span className="text-base-content/60">{t('origin-label')}</span>
          <span>{tc(originLabelKey(detail.origin_kind))}</span>
          <span className="text-base-content/60">{t('aspect-label')}</span>
          <span>{detail.aspect_ratio}</span>
          <span className="text-base-content/60">{t('size-label')}</span>
          <span>{(detail.byte_size / 1024).toFixed(1)} KB</span>
        </div>
      </section>

      <section>
        <h3 className={HEADING}>{t('seed-heading')}</h3>
        <div className="text-sm">
          {detail.seed.effective !== null &&
          detail.seed.effective !== undefined ? (
            <span>{t('effective-seed', { seed: detail.seed.effective })}</span>
          ) : seedReason ? (
            <span className="text-base-content/60" title={seedReason}>
              {seedReason === 'external_creative'
                ? t('seed-unavailable-external')
                : t('seed-unavailable')}
            </span>
          ) : (
            <span className="text-base-content/40">{t('not-available')}</span>
          )}
        </div>
      </section>

      <section>
        {/* The prompt is what the model received, kept verbatim (English);
            only its heading is translated (Issue #191 §4). */}
        <h3 className={HEADING}>{t('prompt-heading')}</h3>
        <p
          className="text-sm whitespace-pre-wrap break-words max-h-40 overflow-y-auto bg-base-200 rounded-lg p-2"
          lang="en"
        >
          {detail.prompt}
        </p>
      </section>

      {tags.length > 0 && (
        <section>
          <h3 className={HEADING}>{t('tags-heading')}</h3>
          <div className="flex flex-wrap gap-1">
            {tags.map(([key, value]) => (
              <span key={key} className="badge badge-sm badge-outline">
                {key}: {String(value)}
              </span>
            ))}
          </div>
        </section>
      )}

      <section>
        <h3 className={HEADING}>{t('review-heading')}</h3>
        <span
          className={`badge ${effectiveReview.badgeClass}`}
          data-testid="asset-detail-review"
          data-review={detail.effective_review_value ?? ''}
        >
          {tc(effectiveReview.labelKey)}
        </span>
        {reviews.length > 0 && (
          <div className="mt-2 flex flex-col gap-1">
            {reviews.map((review) => {
              const status = reviewStatus(review.review_value);
              return (
                <div
                  key={review.id}
                  className="text-xs flex items-center gap-1"
                >
                  <span className={`badge badge-xs ${status.badgeClass}`}>
                    {tc(status.labelKey)}
                  </span>
                  {review.reason && (
                    <span className="text-base-content/60 truncate">
                      {review.reason}
                    </span>
                  )}
                  {review.supersedes_review_id && (
                    <span className="text-base-content/40">
                      {t('correction')}
                    </span>
                  )}
                  {review.source === 'machine_qc' && (
                    <span className="text-base-content/40">
                      {t('qc-machine-review')}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>

      {qcAttempts.length > 0 && (
        <section>
          <h3 className={HEADING}>{t('quality-checks-heading')}</h3>
          <div className="flex flex-col gap-1">
            {qcAttempts.map((attempt) => {
              const status = qcStatus(attempt.state);
              const findings = (attempt.checks ?? []).filter(
                (check) =>
                  check.verdict === 'fail' || check.verdict === 'uncertain'
              );
              return (
                <div key={attempt.id} className="text-xs flex flex-col gap-1">
                  <div className="flex items-center gap-1">
                    <span
                      className={`badge badge-xs ${status.badgeClass}`}
                      data-testid="qc-state"
                      data-state={attempt.state}
                    >
                      {tc(status.labelKey)}
                    </span>
                    {attempt.summary && (
                      <span className="text-base-content/60">
                        {attempt.summary}
                      </span>
                    )}
                  </div>
                  {(attempt.consecutive_failures ?? 0) > 1 && (
                    <span className="text-base-content/60">
                      {t('qc-consecutive-failures', {
                        count: attempt.consecutive_failures ?? 0,
                      })}
                    </span>
                  )}
                  {findings.length > 0 && (
                    <ul className="flex flex-col gap-0.5 pl-2">
                      {findings.map((check) => (
                        <li
                          key={`${attempt.id}-${check.rule_id}`}
                          data-testid="qc-finding"
                          className={
                            check.verdict === 'fail'
                              ? 'text-error'
                              : 'text-warning'
                          }
                        >
                          {check.description ? `${check.description}: ` : ''}
                          {check.reason}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}

      {conclusions.length > 0 && (
        <section>
          <h3 className={HEADING}>{t('experiment-conclusions-heading')}</h3>
          <div className="flex flex-wrap gap-1">
            {conclusions.map((conclusion, index) => {
              const status = verdictStatus(conclusion.verdict);
              return (
                <span
                  key={conclusion.decision_id ?? index}
                  className={`badge badge-xs ${status.badgeClass}`}
                  title={conclusion.context?.experiment_id}
                >
                  {tc(status.labelKey)}
                </span>
              );
            })}
          </div>
        </section>
      )}

      {detail.mock_evidence && (
        <section>
          <h3 className={HEADING}>{t('controlled-mock-evidence-heading')}</h3>
          <InternalView testId="asset-mock-evidence">
            {() => (
              <pre className="text-xs bg-base-200 rounded-lg p-2 overflow-x-auto max-h-32">
                {JSON.stringify(detail.mock_evidence, null, 2)}
              </pre>
            )}
          </InternalView>
        </section>
      )}

      <VersionHistory
        parent={detail.parent ?? null}
        childVersions={detail.children ?? []}
        onNavigate={onNavigateVersion}
      />
    </div>
  );
}
