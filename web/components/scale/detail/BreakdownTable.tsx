import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import {
  useScaleEnrollmentBreakdowns,
  useScaleEnrollmentEvidence,
  type BreakdownSegment,
  type BreakdownType,
} from '@/hooks/scale/useScaleEvidence';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface BreakdownTableProps {
  enrollmentId: string;
  metaMode: ScaleMetaMode;
}

const BREAKDOWN_TYPES: BreakdownType[] = ['placement', 'country', 'age_gender'];

function SegmentRow({ segment }: { segment: BreakdownSegment }) {
  const { t } = useTranslation('scale');

  if (segment.state === 'privacy_suppressed') {
    return (
      <tr>
        <td>{segment.segment_key}</td>
        <td colSpan={6} className="text-base-content/50">
          <span className="badge badge-ghost badge-sm">
            {t('breakdowns.suppressed')}
          </span>{' '}
          {segment.suppression_reason
            ? t(`breakdowns.suppression-reason.${segment.suppression_reason}`, {
                defaultValue: segment.suppression_reason,
              })
            : null}
        </td>
      </tr>
    );
  }

  return (
    <tr>
      <td>{segment.segment_key}</td>
      <td>{segment.spend_share ?? '—'}</td>
      <td>{segment.impressions ?? '—'}</td>
      <td>{segment.link_clicks ?? '—'}</td>
      <td>{segment.purchases ?? '—'}</td>
      <td>
        {segment.cpa ?? (
          <span
            className="text-base-content/50"
            title={
              segment.insufficient_reason
                ? t(
                    `breakdowns.insufficient-reason.${segment.insufficient_reason}`,
                    { defaultValue: segment.insufficient_reason }
                  )
                : undefined
            }
          >
            {t('breakdowns.no-metric')}
          </span>
        )}
      </td>
      <td>{segment.roas ?? t('breakdowns.no-metric')}</td>
    </tr>
  );
}

/**
 * Snapshot-pinned breakdown view (spec §4.2: placement/country/age-gender
 * breakdowns, privacy-suppressed segments distinguishable without leaking
 * values). Pinned to the Enrollment's current accepted-evidence snapshot
 * (`useScaleEnrollmentEvidence`'s `snapshot_id`) -- switching the
 * placement/country/age_gender tab never moves the snapshot underneath the
 * caller mid-review.
 */
export function BreakdownTable({
  enrollmentId,
  metaMode,
}: BreakdownTableProps) {
  const { t } = useTranslation('scale');
  const [breakdownType, setBreakdownType] =
    useState<BreakdownType>('placement');

  const { data: evidence, isLoading: evidenceLoading } =
    useScaleEnrollmentEvidence(enrollmentId, metaMode);
  const snapshotId = evidence?.snapshot_id;

  const {
    page,
    segments,
    problem,
    isLoading,
    isLoadingMore,
    hasMore,
    loadMore,
  } = useScaleEnrollmentBreakdowns(
    enrollmentId,
    metaMode,
    snapshotId,
    breakdownType
  );

  return (
    <div className="rounded-lg border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">{t('breakdowns.heading')}</h3>
        <div role="tablist" className="tabs tabs-boxed tabs-xs">
          {BREAKDOWN_TYPES.map((type) => (
            <button
              key={type}
              type="button"
              role="tab"
              className={`tab ${breakdownType === type ? 'tab-active' : ''}`}
              aria-selected={breakdownType === type}
              onClick={() => setBreakdownType(type)}
            >
              {t(`breakdowns.type.${type}`)}
            </button>
          ))}
        </div>
      </div>

      {evidenceLoading || (isLoading && segments.length === 0) ? (
        <div className="mt-3 flex justify-center" aria-busy="true">
          <span className="loading loading-spinner loading-sm" />
        </div>
      ) : !snapshotId ? (
        <p className="mt-2 text-sm text-base-content/60">
          {t('breakdowns.no-snapshot')}
        </p>
      ) : problem && segments.length === 0 ? (
        <div className="alert alert-error mt-2" role="alert">
          <span>{errorMessage(t, problem)}</span>
        </div>
      ) : segments.length === 0 ? (
        <p className="mt-2 text-sm text-base-content/60">
          {t('breakdowns.empty')}
        </p>
      ) : (
        <>
          {page && (
            <p className="mt-1 text-xs text-base-content/60">
              {t('breakdowns.window', {
                start: page.window_start,
                end: page.window_end,
              })}
            </p>
          )}
          <div className="mt-2 overflow-x-auto">
            <table className="table table-xs">
              <thead>
                <tr>
                  <th>{t('breakdowns.column.segment')}</th>
                  <th>{t('breakdowns.column.spend-share')}</th>
                  <th>{t('breakdowns.column.impressions')}</th>
                  <th>{t('breakdowns.column.link-clicks')}</th>
                  <th>{t('breakdowns.column.purchases')}</th>
                  <th>{t('breakdowns.column.cpa')}</th>
                  <th>{t('breakdowns.column.roas')}</th>
                </tr>
              </thead>
              <tbody>
                {segments.map((segment) => (
                  <SegmentRow
                    key={`${segment.breakdown_type}:${segment.segment_key}`}
                    segment={segment}
                  />
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-2 text-center">
            {hasMore && (
              <button
                type="button"
                className="btn btn-xs btn-ghost w-full"
                onClick={loadMore}
                disabled={isLoadingMore}
              >
                {isLoadingMore
                  ? t('breakdowns.loading')
                  : t('breakdowns.load-more')}
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
