import { useMemo, useState } from 'react';
import { useTranslation } from 'next-i18next';
import type { TemplateDimension } from '@/hooks/generation/useTemplateDimensions';

/**
 * One manual exclusion as the server matches it (`app/generation/planner.py`
 * `apply_exclusions`): every `dimensions` pair must hold for a combination
 * to be excluded, so two pairs mean "this *pairing* is excluded", one pair
 * means "every combination with this value".
 */
export interface ExclusionRule {
  dimensions: Record<string, string>;
  reason?: string;
}

/** A rule the last preview could not apply (`exclusions.invalid`, server-written). */
export interface InvalidExclusion {
  rule: unknown;
  code: string;
  detail?: string;
}

export interface ExclusionConfigValue {
  rules?: ExclusionRule[];
  invalid?: InvalidExclusion[];
  /** @deprecated free-text list the old page wrote; never matched by the server. */
  manual?: string[];
  [key: string]: unknown;
}

interface ExclusionConfigProps {
  exclusions: ExclusionConfigValue;
  /** Dimension/value pairs of the draft's selected templates. */
  dimensions: TemplateDimension[];
  dimensionsLoading?: boolean;
  onUpdate: (exclusions: ExclusionConfigValue) => void;
  disabled?: boolean;
}

interface PendingPair {
  dimensionKey: string;
  valueId: string;
}

export const DEFAULT_EXCLUSION_REASON = 'manual';

/** Strip the keys the server owns or no longer reads before writing. */
export function writeExclusionRules(
  exclusions: ExclusionConfigValue,
  rules: ExclusionRule[]
): ExclusionConfigValue {
  const rest: ExclusionConfigValue = { ...exclusions };
  delete rest.manual;
  delete rest.invalid;
  return { ...rest, rules };
}

function describeRule(rule: unknown, dimensions: TemplateDimension[]): string {
  if (!rule || typeof rule !== 'object') return String(rule);
  const candidate = rule as { dimensions?: unknown };
  if (!candidate.dimensions || typeof candidate.dimensions !== 'object') {
    return JSON.stringify(rule);
  }
  return Object.entries(candidate.dimensions as Record<string, unknown>)
    .map(([key, value]) => {
      const dimension = dimensions.find((d) => d.key === key);
      const dimensionLabel = dimension?.name ?? key;
      const valueLabel =
        dimension?.values.find((v) => v.id === String(value))?.name ??
        String(value);
      return `${dimensionLabel} = ${valueLabel}`;
    })
    .join(' + ');
}

/**
 * Manual exclusions the operator builds from the selected dimension
 * templates (`exclusions.rules`, Issue #157), plus a read-only view of the
 * rules the server flagged on the last preview (`exclusions.invalid`). Both
 * live inside `UpdateDraftInput.exclusions`; `invalid` is written by the
 * server, so it is rendered without an editing affordance and dropped from
 * every write (the next preview recomputes it).
 */
export function ExclusionConfig({
  exclusions,
  dimensions,
  dimensionsLoading,
  onUpdate,
  disabled,
}: ExclusionConfigProps) {
  const { t } = useTranslation('common');
  const rules = useMemo(
    () => (Array.isArray(exclusions.rules) ? exclusions.rules : []),
    [exclusions.rules]
  );
  const invalid = Array.isArray(exclusions.invalid) ? exclusions.invalid : [];
  const [pairs, setPairs] = useState<PendingPair[]>([]);
  const [dimensionKey, setDimensionKey] = useState('');
  const [valueId, setValueId] = useState('');
  const [reason, setReason] = useState('');

  const selectedDimension = dimensions.find((d) => d.key === dimensionKey);
  const canAddPair = !!selectedDimension && valueId !== '';
  const canAddRule = pairs.length > 0 && !disabled;

  function addPair() {
    if (!canAddPair) return;
    setPairs((current) => [
      ...current.filter((pair) => pair.dimensionKey !== dimensionKey),
      { dimensionKey, valueId },
    ]);
    setDimensionKey('');
    setValueId('');
  }

  function removePair(key: string) {
    setPairs((current) => current.filter((pair) => pair.dimensionKey !== key));
  }

  function addRule() {
    if (!canAddRule) return;
    const rule: ExclusionRule = {
      dimensions: Object.fromEntries(
        pairs.map((pair) => [pair.dimensionKey, pair.valueId])
      ),
      reason: reason.trim() || DEFAULT_EXCLUSION_REASON,
    };
    onUpdate(writeExclusionRules(exclusions, [...rules, rule]));
    setPairs([]);
    setReason('');
  }

  function removeRule(index: number) {
    onUpdate(
      writeExclusionRules(
        exclusions,
        rules.filter((_rule, position) => position !== index)
      )
    );
  }

  return (
    <div className="form-control space-y-2">
      <span className="label-text font-medium">
        {t('generation-draft-exclusions-label')}
      </span>
      <span className="text-xs text-base-content/60">
        {t('generation-draft-exclusions-hint')}
      </span>

      {dimensionsLoading && (
        <span className="text-xs text-base-content/60" role="status">
          {t('generation-draft-exclusions-dimensions-loading')}
        </span>
      )}
      {!dimensionsLoading && dimensions.length === 0 && (
        <span className="text-xs text-base-content/60">
          {t('generation-draft-exclusions-no-dimensions')}
        </span>
      )}

      {dimensions.length > 0 && (
        <div className="space-y-2 rounded-lg border p-3">
          <div className="flex flex-wrap items-end gap-2">
            <div className="form-control">
              <label className="label" htmlFor="exclusion-dimension">
                <span className="label-text">
                  {t('generation-draft-exclusions-dimension-label')}
                </span>
              </label>
              <select
                id="exclusion-dimension"
                className="select select-bordered select-sm"
                value={dimensionKey}
                disabled={disabled}
                onChange={(event) => {
                  setDimensionKey(event.target.value);
                  setValueId('');
                }}
              >
                <option value="">
                  {t('generation-draft-exclusions-dimension-placeholder')}
                </option>
                {dimensions.map((dimension) => (
                  <option key={dimension.key} value={dimension.key}>
                    {dimension.templateName} · {dimension.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="form-control">
              <label className="label" htmlFor="exclusion-value">
                <span className="label-text">
                  {t('generation-draft-exclusions-value-label')}
                </span>
              </label>
              <select
                id="exclusion-value"
                className="select select-bordered select-sm"
                value={valueId}
                disabled={disabled || !selectedDimension}
                onChange={(event) => setValueId(event.target.value)}
              >
                <option value="">
                  {t('generation-draft-exclusions-value-placeholder')}
                </option>
                {(selectedDimension?.values ?? []).map((value) => (
                  <option key={value.id} value={value.id}>
                    {value.name}
                  </option>
                ))}
              </select>
            </div>
            <button
              type="button"
              className="btn btn-sm"
              disabled={disabled || !canAddPair}
              onClick={addPair}
            >
              {t('generation-draft-exclusions-add-pair')}
            </button>
          </div>

          {pairs.length > 0 && (
            <ul
              className="flex flex-wrap gap-2"
              aria-label={t('generation-draft-exclusions-pending-label')}
            >
              {pairs.map((pair) => {
                const label = describeRule(
                  { dimensions: { [pair.dimensionKey]: pair.valueId } },
                  dimensions
                );
                return (
                  <li
                    key={pair.dimensionKey}
                    className="badge badge-outline gap-1"
                  >
                    {label}
                    <button
                      type="button"
                      aria-label={t('generation-draft-exclusions-remove', {
                        value: label,
                      })}
                      disabled={disabled}
                      onClick={() => removePair(pair.dimensionKey)}
                    >
                      ×
                    </button>
                  </li>
                );
              })}
            </ul>
          )}

          <div className="flex flex-wrap items-end gap-2">
            <div className="form-control flex-1">
              <label className="label" htmlFor="exclusion-reason">
                <span className="label-text">
                  {t('generation-draft-exclusions-reason-label')}
                </span>
              </label>
              <input
                id="exclusion-reason"
                type="text"
                className="input input-bordered input-sm"
                placeholder={t(
                  'generation-draft-exclusions-reason-placeholder'
                )}
                value={reason}
                disabled={disabled}
                onChange={(event) => setReason(event.target.value)}
              />
            </div>
            <button
              type="button"
              className="btn btn-sm btn-primary"
              disabled={!canAddRule}
              onClick={addRule}
            >
              {t('generation-draft-exclusions-add')}
            </button>
          </div>
        </div>
      )}

      {rules.length > 0 && (
        <ul
          className="space-y-1"
          aria-label={t('generation-draft-exclusions-rules-label')}
        >
          {rules.map((rule, index) => {
            const label = describeRule(rule, dimensions);
            return (
              <li
                key={`${index}:${label}`}
                className="flex items-center justify-between gap-2 rounded border px-2 py-1 text-sm"
              >
                <span>
                  {label}
                  {rule.reason && (
                    <span className="ml-2 text-xs text-base-content/60">
                      {rule.reason}
                    </span>
                  )}
                </span>
                <button
                  type="button"
                  className="btn btn-ghost btn-xs"
                  aria-label={t('generation-draft-exclusions-remove', {
                    value: label,
                  })}
                  disabled={disabled}
                  onClick={() => removeRule(index)}
                >
                  ×
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {invalid.length > 0 && (
        <div role="note">
          <span className="text-xs text-warning">
            {t('generation-draft-exclusions-invalid-label')}
          </span>
          <ul className="mt-1 flex flex-wrap gap-2">
            {invalid.map((entry, index) => (
              <li
                key={`${index}:${entry.code}`}
                className="badge badge-ghost gap-1"
                title={entry.detail}
              >
                {describeRule(entry.rule, dimensions)}
                <span className="text-xs opacity-70">({entry.code})</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
