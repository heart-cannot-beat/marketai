import { useTranslation } from 'next-i18next';
import { useCommittedText } from './useCommittedText';

/**
 * `combination_strategy` as the server reads it (`app/generation/planner.py`,
 * Issue #157): `{ kind: 'full' }` or `{ kind: 'coverage', cap }`. The legacy
 * `{ mode: 'full' | 'sampled', sample_cap }` shape this page used to write is
 * still accepted by the server (reported back as `strategy_applied.
 * deprecated_shape`), so it is read here for display and rewritten in the
 * canonical shape on the next change. Other keys (`planner_version`,
 * `requested_variant_count`, `batch_draft_ids`) belong to other modules and
 * are carried through untouched.
 */
export interface CombinationStrategyValue {
  kind?: string;
  cap?: number | null;
  /** @deprecated legacy shape, read-only compatibility */
  mode?: string;
  /** @deprecated legacy shape, read-only compatibility */
  sample_cap?: number | null;
  [key: string]: unknown;
}

export type CombinationStrategyKind = 'full' | 'coverage';

export const DEFAULT_COVERAGE_CAP = 12;

interface CombinationStrategyProps {
  strategy: CombinationStrategyValue;
  onUpdate: (strategy: CombinationStrategyValue) => void;
  disabled?: boolean;
}

/** Resolve the effective kind/cap from either the canonical or legacy shape. */
export function readCombinationStrategy(strategy: CombinationStrategyValue): {
  kind: CombinationStrategyKind;
  cap: number | null;
  legacy: boolean;
} {
  if (strategy.kind !== undefined) {
    const kind: CombinationStrategyKind =
      strategy.kind === 'coverage' ? 'coverage' : 'full';
    return {
      kind,
      cap: kind === 'coverage' ? (strategy.cap ?? null) : null,
      legacy: false,
    };
  }
  if (strategy.mode !== undefined) {
    const kind: CombinationStrategyKind =
      strategy.mode === 'sampled' ? 'coverage' : 'full';
    return {
      kind,
      cap: kind === 'coverage' ? (strategy.sample_cap ?? null) : null,
      legacy: true,
    };
  }
  return { kind: 'full', cap: null, legacy: false };
}

/** Build the canonical shape, dropping the legacy keys but keeping the rest. */
export function writeCombinationStrategy(
  strategy: CombinationStrategyValue,
  kind: CombinationStrategyKind,
  cap: number | null
): CombinationStrategyValue {
  const rest: CombinationStrategyValue = { ...strategy };
  delete rest.mode;
  delete rest.sample_cap;
  delete rest.cap;
  if (kind === 'coverage') {
    return { ...rest, kind: 'coverage', cap: cap ?? DEFAULT_COVERAGE_CAP };
  }
  return { ...rest, kind: 'full' };
}

/**
 * Full vs. coverage combination toggle, with a numeric cap when coverage
 * sampling is on. Writes the whole `combination_strategy` dict in one CAS
 * patch (`UpdateDraftInput.combination_strategy`).
 */
export function CombinationStrategy({
  strategy,
  onUpdate,
  disabled,
}: CombinationStrategyProps) {
  const { t } = useTranslation('common');
  const { kind, cap, legacy } = readCombinationStrategy(strategy);
  const capField = useCommittedText(
    cap === null ? '' : String(cap),
    (value) => {
      const parsed = parseInt(value, 10);
      onUpdate(
        writeCombinationStrategy(
          strategy,
          'coverage',
          Number.isFinite(parsed) && parsed >= 1 ? parsed : DEFAULT_COVERAGE_CAP
        )
      );
    }
  );

  return (
    <div className="form-control space-y-2">
      <span className="label-text font-medium">
        {t('generation-draft-combination-label')}
      </span>
      <div className="join">
        <button
          type="button"
          className={`btn btn-sm join-item ${kind === 'full' ? 'btn-active' : ''}`}
          disabled={disabled}
          aria-pressed={kind === 'full'}
          onClick={() =>
            onUpdate(writeCombinationStrategy(strategy, 'full', null))
          }
        >
          {t('generation-draft-combination-full')}
        </button>
        <button
          type="button"
          className={`btn btn-sm join-item ${kind === 'coverage' ? 'btn-active' : ''}`}
          disabled={disabled}
          aria-pressed={kind === 'coverage'}
          onClick={() =>
            onUpdate(
              writeCombinationStrategy(
                strategy,
                'coverage',
                cap ?? DEFAULT_COVERAGE_CAP
              )
            )
          }
        >
          {t('generation-draft-combination-coverage')}
        </button>
      </div>
      <span className="text-xs text-base-content/60">
        {kind === 'coverage'
          ? t('generation-draft-combination-coverage-hint')
          : t('generation-draft-combination-full-hint')}
      </span>
      {kind === 'coverage' && (
        <div className="form-control">
          <label className="label" htmlFor="sample-cap-input">
            <span className="label-text">
              {t('generation-draft-combination-cap-label')}
            </span>
          </label>
          <input
            id="sample-cap-input"
            type="number"
            min={1}
            step={1}
            className="input input-bordered w-32"
            value={capField.value}
            disabled={disabled}
            onFocus={capField.onFocus}
            onChange={(event) => capField.onChange(event.target.value)}
            onBlur={capField.onBlur}
            onKeyDown={capField.onKeyDown}
          />
        </div>
      )}
      {legacy && (
        <span className="text-xs text-warning" role="note">
          {t('generation-draft-combination-legacy-shape')}
        </span>
      )}
    </div>
  );
}
