import type { ReviewStats } from '@/hooks/assets/useReviewMode';
import { useTranslation } from 'next-i18next';

interface ReviewSummaryProps {
  stats: ReviewStats;
  onClose: () => void;
  onBatchRegenerate?: () => void;
  onBatchSupplement?: () => void;
}

export function ReviewSummary({
  stats,
  onClose,
  onBatchRegenerate,
  onBatchSupplement,
}: ReviewSummaryProps) {
  const { t } = useTranslation('assets');
  return (
    <div
      className="flex flex-col items-center justify-center h-full gap-6 p-8"
      data-testid="review-summary"
    >
      <h2 className="text-2xl font-bold" data-testid="review-complete">
        {t('review-complete')}
      </h2>

      <div className="stats shadow">
        <div className="stat" data-testid="review-stat-reviewed">
          <div className="stat-title">{t('reviewed')}</div>
          <div className="stat-value">{stats.reviewed}</div>
          <div className="stat-desc">
            {t('of-total', { count: stats.total })}
          </div>
        </div>
        <div className="stat" data-testid="review-stat-approved">
          <div className="stat-title">{t('approved')}</div>
          <div className="stat-value text-success">{stats.approved}</div>
        </div>
        <div className="stat" data-testid="review-stat-needs-changes">
          <div className="stat-title">{t('needs-changes')}</div>
          <div className="stat-value text-warning">{stats.needsChanges}</div>
        </div>
        <div className="stat" data-testid="review-stat-rejected">
          <div className="stat-title">{t('rejected')}</div>
          <div className="stat-value text-error">{stats.rejected}</div>
        </div>
        <div className="stat" data-testid="review-stat-skipped">
          <div className="stat-title">{t('skipped')}</div>
          <div className="stat-value">{stats.skipped}</div>
        </div>
      </div>

      <div className="flex gap-3">
        {stats.needsChanges >= 2 && onBatchRegenerate && (
          <button
            type="button"
            className="btn btn-warning"
            onClick={onBatchRegenerate}
          >
            {t('regenerate-needs-changes', { count: stats.needsChanges })}
          </button>
        )}
        {stats.approved >= 3 && onBatchSupplement && (
          <button
            type="button"
            className="btn btn-success"
            onClick={onBatchSupplement}
          >
            {t('supplement-approved', { count: stats.approved })}
          </button>
        )}
        <button
          type="button"
          className="btn btn-primary"
          onClick={onClose}
          data-testid="review-done"
        >
          {t('done')}
        </button>
      </div>
    </div>
  );
}
