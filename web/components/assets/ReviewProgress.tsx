import type { ReviewStats } from '@/hooks/assets/useReviewMode';
import { useTranslation } from 'next-i18next';

interface ReviewProgressProps {
  stats: ReviewStats;
  currentIndex: number;
}

export function ReviewProgress({ stats, currentIndex }: ReviewProgressProps) {
  const { t } = useTranslation('assets');
  return (
    <div className="flex items-center gap-3 px-4 py-2 bg-base-200">
      <progress
        className="progress progress-primary flex-1"
        value={currentIndex}
        max={stats.total}
      />
      <div className="flex items-center gap-2 text-sm shrink-0">
        <span data-testid="review-position">
          {currentIndex}/{stats.total}
        </span>
        <span className="text-base-content/40">|</span>
        <span className="text-success">
          {t('review-progress-approved', { count: stats.approved })}
        </span>
        <span className="text-warning">
          {t('review-progress-changes', { count: stats.needsChanges })}
        </span>
        <span className="text-error">
          {t('review-progress-rejected', { count: stats.rejected })}
        </span>
        <span className="text-base-content/40">
          {t('review-skipped', { count: stats.skipped })}
        </span>
      </div>
    </div>
  );
}
