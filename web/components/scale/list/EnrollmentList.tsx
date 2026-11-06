import { useState } from 'react';
import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import { formatDateTime } from '@/lib/i18n/format';
import { useScaleEnrollments } from '@/hooks/scale/useScaleEnrollments';
import { useScaleNav } from '@/hooks/scale/useScaleNav';
import { useScaleMetaMode } from '@/lib/scale/meta-mode';
import { FeatureEmptyState } from '@/components/workspace';
import { MetaModeSelect } from '@/components/scale/shared/MetaModeSelect';
import { LifecycleBadge } from '@/components/scale/shared/LifecycleBadge';
import { CandidateSelector } from '@/components/scale/list/CandidateSelector';

const LIFECYCLE_FILTERS = ['', 'draft', 'active', 'paused', 'closed'] as const;

/**
 * Entry point for Scale (spec §4.1). Cursor-paginated Enrollment list --
 * each row shows the Enrollment lifecycle axis directly (spec §4.4);
 * health is a second, independent axis surfaced only once evidence exists,
 * so this list intentionally does not fetch per-row evidence (that's the
 * detail page's job) to keep the list itself a single cheap query.
 */
export function EnrollmentList({
  preferredDecisionId,
}: { preferredDecisionId?: string } = {}) {
  const { t, i18n } = useTranslation('scale');
  const { metaMode, setMetaMode } = useScaleMetaMode();
  const { buildEnrollmentUrl, buildTargetsUrl } = useScaleNav();
  const [lifecycle, setLifecycle] = useState<string>('');
  const [selectorOpen, setSelectorOpen] = useState(!!preferredDecisionId);

  const {
    items,
    hasMore,
    isLoading,
    isLoadingMore,
    isRefreshing,
    problem,
    loadMore,
    refresh,
  } = useScaleEnrollments(metaMode, { lifecycle: lifecycle || undefined });

  const isEmpty = !isLoading && items.length === 0 && !problem;

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold outline-none">{t('title')}</h1>
        <div className="flex items-center gap-3">
          <MetaModeSelect value={metaMode} onChange={setMetaMode} />
          <Link href={buildTargetsUrl()} className="btn btn-sm btn-ghost">
            {t('list.targets-link')}
          </Link>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            onClick={() => setSelectorOpen(true)}
          >
            {t('list.new-enrollment')}
          </button>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <div role="tablist" className="tabs tabs-boxed">
          {LIFECYCLE_FILTERS.map((value) => (
            <button
              key={value || 'all'}
              type="button"
              role="tab"
              className={`tab ${lifecycle === value ? 'tab-active' : ''}`}
              onClick={() => setLifecycle(value)}
            >
              {value
                ? t(`lifecycle.enrollment.${value}`)
                : t('list.filter-all')}
            </button>
          ))}
        </div>
        {isRefreshing && (
          <span className="text-xs text-base-content/60">
            {t('list.refreshing')}
          </span>
        )}
      </div>

      <div className="alert mt-4 border border-info/30 bg-info/10" role="note">
        <span>
          <strong>{t('advisory-boundary.title')}</strong>{' '}
          {t('advisory-boundary.description')}
        </span>
      </div>

      {isLoading && (
        <div className="mt-8 flex justify-center" aria-busy="true">
          <span className="loading loading-spinner" />
        </div>
      )}

      {!isLoading && problem && items.length === 0 && (
        <div className="alert alert-error mt-8" role="alert">
          <span>{t('list.error')}</span>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => refresh()}
          >
            {t('list.retry')}
          </button>
        </div>
      )}

      {isEmpty && (
        <FeatureEmptyState
          title={t('list.empty-title')}
          description={t('list.empty-description')}
          action={
            <button
              type="button"
              className="btn btn-primary btn-sm mt-2"
              onClick={() => setSelectorOpen(true)}
            >
              {t('list.new-enrollment')}
            </button>
          }
        />
      )}

      {!isLoading && items.length > 0 && (
        <div className="mt-6 overflow-x-auto">
          <table className="table">
            <thead>
              <tr>
                <th>{t('list.column-enrollment')}</th>
                <th>{t('list.column-lifecycle')}</th>
                <th>{t('list.column-market')}</th>
                <th>{t('list.column-updated')}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((enrollment) => (
                <tr key={enrollment.id}>
                  <td>
                    <Link
                      href={buildEnrollmentUrl(enrollment.id)}
                      className="font-medium hover:underline"
                    >
                      {enrollment.id}
                    </Link>
                  </td>
                  <td>
                    <LifecycleBadge
                      scope="enrollment"
                      value={enrollment.lifecycle}
                    />
                  </td>
                  <td>{enrollment.market_code}</td>
                  <td>
                    {formatDateTime(i18n.language, enrollment.updated_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="mt-4 text-center">
            {hasMore ? (
              <button
                type="button"
                className="btn btn-sm btn-ghost w-full"
                onClick={loadMore}
                disabled={isLoadingMore}
              >
                {isLoadingMore ? t('list.loading') : t('list.load-more')}
              </button>
            ) : (
              <p className="text-xs text-base-content/60">{t('list.end')}</p>
            )}
          </div>
        </div>
      )}

      <CandidateSelector
        preferredDecisionId={preferredDecisionId}
        open={selectorOpen}
        metaMode={metaMode}
        onClose={() => setSelectorOpen(false)}
        onCreated={() => {
          setSelectorOpen(false);
          refresh();
        }}
      />
    </>
  );
}
