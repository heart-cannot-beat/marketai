import { useId, type ReactNode } from 'react';
import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import type {
  DirectionGroup,
  ReviewCounts,
  StatusCounts,
} from '@/lib/test-plans/direction-groups';

type TFunction = ReturnType<typeof useTranslation>['t'];

/** The title a group is shown by: its direction's title, or "未匹配到方向". */
export function groupTitle(t: TFunction, group: DirectionGroup): string {
  return group.direction?.title?.trim() || t('group-unmatched-title');
}

const STATUS_ORDER: (keyof Omit<StatusCounts, 'total'>)[] = [
  'done',
  'running',
  'queued',
  'failed',
];

const STATUS_TONE: Record<keyof Omit<StatusCounts, 'total'>, string> = {
  done: 'text-success',
  running: 'text-info',
  queued: 'text-base-content/70',
  failed: 'text-error',
};

const REVIEW_ORDER: (keyof Omit<ReviewCounts, 'total'>)[] = [
  'approved',
  'needs_changes',
  'rejected',
  'unreviewed',
];

const REVIEW_FILL: Record<keyof Omit<ReviewCounts, 'total'>, string> = {
  approved: 'bg-success',
  needs_changes: 'bg-warning',
  rejected: 'bg-error',
  unreviewed: 'bg-base-300',
};

function StatusLine({ status }: { status: StatusCounts }) {
  const { t } = useTranslation('directions');
  if (status.total === 0) return null;
  return (
    <p
      className="flex flex-wrap gap-x-3 gap-y-1 text-sm"
      data-testid="group-status"
    >
      <span className="sr-only">{t('group-status-label')}</span>
      {STATUS_ORDER.filter((key) => status[key] > 0).map((key) => (
        <span key={key} className={STATUS_TONE[key]}>
          {t(`status-${key}`, { count: status[key] })}
        </span>
      ))}
    </p>
  );
}

/** One bar split by review outcome, with the counts beside it. */
function ReviewLine({ review }: { review: ReviewCounts }) {
  const { t } = useTranslation('directions');
  if (review.total === 0) {
    return (
      <p className="text-sm text-base-content/60" data-testid="group-review">
        {t('review-nothing-yet')}
      </p>
    );
  }
  const reviewed = review.total - review.unreviewed;
  const summary = t('review-progress', { reviewed, total: review.total });
  return (
    <div className="space-y-1.5" data-testid="group-review">
      <div
        className="flex h-1.5 w-full max-w-xs overflow-hidden rounded-full bg-base-300"
        role="img"
        aria-label={t('group-review-label', { summary })}
      >
        {REVIEW_ORDER.filter(
          (key) => key !== 'unreviewed' && review[key] > 0
        ).map((key) => (
          <span
            key={key}
            className={REVIEW_FILL[key]}
            style={{ width: `${(review[key] / review.total) * 100}%` }}
          />
        ))}
      </div>
      <p className="flex flex-wrap gap-x-3 gap-y-1 text-sm">
        <span className="font-medium">{summary}</span>
        {REVIEW_ORDER.filter((key) => review[key] > 0).map((key) => (
          <span
            key={key}
            className="inline-flex items-center gap-1 text-base-content/70"
          >
            <span
              className={`inline-block h-2 w-2 rounded-full ${REVIEW_FILL[key]}`}
              aria-hidden="true"
            />
            {t(`review-${key}`, { count: review[key] })}
          </span>
        ))}
      </p>
    </div>
  );
}

export interface DirectionGroupSectionProps {
  group: DirectionGroup;
  /** The group's thumbnail grid. */
  children: ReactNode;
  onReview: () => void;
  onExport: () => void;
  /** Review / export are allowed for this Workspace member at all. */
  canReview?: boolean;
  canExport?: boolean;
  experimentsHref: string;
}

/**
 * One direction of a test-plan Run (front-flow contract §3.3): its title,
 * what it is testing, generation status, review progress, thumbnails, and
 * the three exits -- 审核本组, 导出本组, 去投放实验.
 */
export function DirectionGroupSection({
  group,
  children,
  onReview,
  onExport,
  canReview = true,
  canExport = true,
  experimentsHref,
}: DirectionGroupSectionProps) {
  const { t } = useTranslation('directions');
  const titleId = useId();
  const hintId = useId();
  const title = groupTitle(t, group);
  const unmatched = group.direction === null;
  const whatItTests = unmatched
    ? t('group-unmatched-tests')
    : group.direction?.what_it_tests?.trim();
  const hasImages = group.versions.length > 0;
  const hasApproved = group.review.approved > 0;

  return (
    <section
      className={`space-y-3 rounded-box border bg-base-100 p-4 sm:p-5 ${
        unmatched ? 'border-dashed border-base-300' : 'border-base-300'
      }`}
      aria-labelledby={titleId}
      data-testid="direction-group"
      data-unmatched={unmatched ? 'true' : undefined}
    >
      <div className="space-y-1">
        <h3 id={titleId} className="text-lg font-semibold leading-snug">
          {title}
          {group.direction?.removed && (
            <span className="badge badge-ghost badge-sm ml-2 align-middle font-normal">
              {t('group-removed')}
            </span>
          )}
        </h3>
        {whatItTests && (
          <p className="max-w-prose text-sm text-base-content/80">
            <span className="sr-only">{t('group-tests-label')}</span>
            {whatItTests}
          </p>
        )}
      </div>

      <div className="space-y-2">
        <StatusLine status={group.status} />
        <ReviewLine review={group.review} />
      </div>

      {children}

      <div className="flex flex-wrap items-center gap-2 pt-1">
        <button
          type="button"
          className="btn btn-primary btn-sm"
          onClick={onReview}
          disabled={!hasImages || !canReview}
          aria-label={t('action-review-label', { title })}
          data-testid="group-review-action"
        >
          {t('action-review')}
        </button>
        <button
          type="button"
          className="btn btn-outline btn-sm"
          onClick={onExport}
          disabled={!hasImages || !canExport}
          aria-label={t('action-export-label', { title })}
          data-testid="group-export-action"
        >
          {t('action-export')}
        </button>
        {hasApproved ? (
          <Link
            href={experimentsHref}
            className="btn btn-ghost btn-sm"
            data-testid="group-experiment-action"
          >
            {t('action-experiment')}
          </Link>
        ) : (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled
            aria-describedby={hintId}
            data-testid="group-experiment-action"
          >
            {t('action-experiment')}
          </button>
        )}
        {!hasApproved && (
          <span id={hintId} className="text-xs text-base-content/60">
            {hasImages ? t('action-experiment-hint') : t('action-no-images')}
          </span>
        )}
      </div>
    </section>
  );
}
