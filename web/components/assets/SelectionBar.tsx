import { useTranslation } from 'next-i18next';

interface SelectionBarProps {
  count: number;
  onClear: () => void;
  onExport: () => void;
  onReviewMode: () => void;
  canExport: boolean;
  canReview: boolean;
  /** How many of the selected images are ads that may not be exported yet
   * (Issue #219). Export is not offered while there is one, and the bar
   * says why; reviewing them stays possible. */
  blockedCount?: number;
}

export function SelectionBar({
  count,
  onClear,
  onExport,
  onReviewMode,
  canExport,
  canReview,
  blockedCount = 0,
}: SelectionBarProps) {
  const { t } = useTranslation('assets');
  if (count === 0) return null;

  return (
    <div
      className="sticky bottom-0 z-20 border-t border-base-300 bg-base-100 px-4 py-2 flex flex-wrap items-center gap-3"
      data-testid="selection-bar"
    >
      <div role="status" aria-live="polite" className="font-medium text-sm">
        {t('selection-count', { count })}
      </div>
      {blockedCount > 0 && (
        <p
          id="selection-export-blocked"
          className="text-xs text-warning-content bg-warning/20 rounded px-2 py-1"
          data-testid="selection-export-blocked"
        >
          {t('export-block-selection', { count: blockedCount })}
        </p>
      )}

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
          disabled={!canExport || blockedCount > 0}
          aria-label={t('export-selected')}
          aria-describedby={
            blockedCount > 0 ? 'selection-export-blocked' : undefined
          }
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
