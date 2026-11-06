import { useTranslation } from 'next-i18next';

interface SelectionBarProps {
  count: number;
  onClear: () => void;
  onExport: () => void;
  onReviewMode: () => void;
  canExport: boolean;
  canReview: boolean;
}

export function SelectionBar({
  count,
  onClear,
  onExport,
  onReviewMode,
  canExport,
  canReview,
}: SelectionBarProps) {
  const { t } = useTranslation('assets');
  if (count === 0) return null;

  return (
    <div
      className="sticky bottom-0 z-20 border-t border-base-300 bg-base-100 px-4 py-2 flex items-center gap-3"
      data-testid="selection-bar"
    >
      <div role="status" aria-live="polite" className="font-medium text-sm">
        {t('selection-count', { count })}
      </div>

      <div className="flex items-center gap-2 ml-auto">
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          onClick={onReviewMode}
          disabled={!canReview}
          aria-label={t('review-selected')}
          data-testid="selection-review"
        >
          {t('review-heading')}
        </button>
        <button
          type="button"
          className="btn btn-sm btn-primary"
          onClick={onExport}
          disabled={!canExport}
          aria-label={t('export-selected')}
          data-testid="selection-export"
        >
          {t('export-heading')}
        </button>
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          onClick={onClear}
          aria-label={t('clear-selection')}
          data-testid="selection-clear"
        >
          {t('clear-selection')}
        </button>
      </div>
    </div>
  );
}
