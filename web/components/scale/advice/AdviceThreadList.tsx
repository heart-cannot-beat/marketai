import { useTranslation } from 'next-i18next';
import { useScaleAdviceThreads } from '@/hooks/scale/useScaleAdvice';
import { AdviceThreadCard } from '@/components/scale/advice/AdviceThreadCard';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';

interface AdviceThreadListProps {
  metaMode: ScaleMetaMode;
  enrollmentId?: string;
  lifecycle?: string;
}

/**
 * Paginated Advice-thread panel (spec §4.2). Scoped to one Enrollment on the
 * detail page (`enrollmentId` set) or the cross-Enrollment advice inbox when
 * omitted -- same component, same server-driven `allowed_actions` gating
 * either way.
 */
export function AdviceThreadList({
  metaMode,
  enrollmentId,
  lifecycle,
}: AdviceThreadListProps) {
  const { t } = useTranslation('scale');
  const {
    items,
    hasMore,
    isLoading,
    isLoadingMore,
    problem,
    loadMore,
    refresh,
  } = useScaleAdviceThreads(metaMode, { enrollmentId, lifecycle });

  if (isLoading) {
    return (
      <div className="mt-2 flex justify-center" aria-busy="true">
        <span className="loading loading-spinner loading-sm" />
      </div>
    );
  }

  if (problem && items.length === 0) {
    return (
      <div className="alert alert-error mt-2" role="alert">
        <span>{t('advice-list.error')}</span>
        <button type="button" className="btn btn-sm" onClick={() => refresh()}>
          {t('advice-list.retry')}
        </button>
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <p className="mt-2 text-sm text-base-content/60">
        {t('advice-list.empty')}
      </p>
    );
  }

  return (
    <div className="mt-2">
      <ul className="space-y-2">
        {items.map((thread) => (
          <AdviceThreadCard
            key={thread.id}
            thread={thread}
            metaMode={metaMode}
            onChanged={refresh}
          />
        ))}
      </ul>

      <div className="mt-3 text-center">
        {hasMore ? (
          <button
            type="button"
            className="btn btn-sm btn-ghost w-full"
            onClick={loadMore}
            disabled={isLoadingMore}
          >
            {isLoadingMore
              ? t('advice-list.loading')
              : t('advice-list.load-more')}
          </button>
        ) : (
          <p className="text-xs text-base-content/60">{t('advice-list.end')}</p>
        )}
      </div>
    </div>
  );
}
