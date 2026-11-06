import { useTranslation } from 'next-i18next';

interface ExclusionSummary {
  excluded_count: number;
  reasons: string[];
}

/** `GenerationPreviewDetail.strategy_applied` (Issue #157). */
export interface StrategyApplied {
  kind: string;
  cap?: number | null;
  deprecated_shape?: boolean;
  full_count?: number;
  candidate_count?: number;
  selected_count?: number;
}

interface CombinationSummaryProps {
  combinationCount: number;
  callCount: number;
  exclusionSummary: ExclusionSummary | null;
  strategyApplied?: StrategyApplied | null;
  combinationsDroppedByConstraints?: number;
}

/**
 * `GenerationPreviewDetail.combination_count` / `.call_count` /
 * `.exclusion_summary` plus the planner's own account of what it did --
 * `strategy_applied` and `combinations_dropped_by_constraints`
 * (`app/generation/response_models.py`, Issue #157) -- so the operator can
 * see that the strategy and template constraints took effect.
 */
export function CombinationSummary({
  combinationCount,
  callCount,
  exclusionSummary,
  strategyApplied,
  combinationsDroppedByConstraints = 0,
}: CombinationSummaryProps) {
  const { t } = useTranslation('common');

  return (
    <div className="space-y-2">
      <div className="stats stats-vertical lg:stats-horizontal shadow-sm">
        <div className="stat">
          <div className="stat-title">
            {t('generation-preview-combinations-label')}
          </div>
          <div className="stat-value text-lg">{combinationCount}</div>
          {strategyApplied && (
            <div className="stat-desc" data-testid="strategy-applied">
              {strategyApplied.kind === 'coverage'
                ? t('generation-preview-strategy-coverage', {
                    cap: strategyApplied.cap ?? '?',
                    full: strategyApplied.full_count ?? 0,
                  })
                : t('generation-preview-strategy-full', {
                    full: strategyApplied.full_count ?? 0,
                  })}
            </div>
          )}
        </div>
        <div className="stat">
          <div className="stat-title">
            {t('generation-preview-calls-label')}
          </div>
          <div className="stat-value text-lg">{callCount}</div>
        </div>
        {exclusionSummary && exclusionSummary.excluded_count > 0 && (
          <div className="stat">
            <div className="stat-title">
              {t('generation-preview-excluded-label')}
            </div>
            <div className="stat-value text-lg text-warning">
              {exclusionSummary.excluded_count}
            </div>
            <div className="stat-desc">
              {exclusionSummary.reasons.join(', ')}
            </div>
          </div>
        )}
        {combinationsDroppedByConstraints > 0 && (
          <div className="stat">
            <div className="stat-title">
              {t('generation-preview-constraints-dropped-label')}
            </div>
            <div
              className="stat-value text-lg text-warning"
              data-testid="constraints-dropped"
            >
              {combinationsDroppedByConstraints}
            </div>
          </div>
        )}
      </div>
      {strategyApplied?.deprecated_shape && (
        <p className="text-xs text-warning" role="note">
          {t('generation-preview-strategy-deprecated-shape')}
        </p>
      )}
    </div>
  );
}
