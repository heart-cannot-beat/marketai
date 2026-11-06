import { useTranslation } from 'next-i18next';
import { useCommittedText } from './useCommittedText';

interface FormatConfigValue {
  aspect_ratio?: string;
  resolution?: string;
  variants_per_combination?: number;
}

interface FormatConfigProps {
  format: FormatConfigValue;
  onUpdate: (format: FormatConfigValue) => void;
  disabled?: boolean;
  /**
   * The resolved model's `format_limits.max_variants_per_combination` from
   * `get_generation_capabilities`. The server enforces the same ceiling at
   * preview and submission (`variants_exceed_model_limit`, Issue #157); the
   * input clamps to it so the operator sees the limit before the problem
   * detail would. `null`/`undefined` means no limit is known client-side.
   */
  maxVariantsPerCombination?: number | null;
}

const ASPECT_RATIOS = ['1:1', '4:5', '9:16', '16:9'];

/**
 * Aspect ratio, resolution and variant-count fields, written as one CAS
 * patch to `format_config` (`UpdateDraftInput.format_config` -- an
 * unconstrained dict, same as `combination_strategy`). The two typed fields
 * commit on blur/Enter (`useCommittedText`); the select commits on change.
 */
export function FormatConfig({
  format,
  onUpdate,
  disabled,
  maxVariantsPerCombination,
}: FormatConfigProps) {
  const { t } = useTranslation('common');
  const maxVariants =
    typeof maxVariantsPerCombination === 'number' &&
    maxVariantsPerCombination >= 1
      ? maxVariantsPerCombination
      : null;
  const resolution = useCommittedText(format.resolution ?? '', (value) =>
    onUpdate({ ...format, resolution: value.trim() })
  );
  const variantCount = useCommittedText(
    format.variants_per_combination === undefined
      ? ''
      : String(format.variants_per_combination),
    (value) => {
      const parsed = parseInt(value, 10);
      const positive = Number.isFinite(parsed) && parsed > 0 ? parsed : null;
      const clamped =
        positive !== null && maxVariants !== null
          ? Math.min(positive, maxVariants)
          : positive;
      onUpdate({
        ...format,
        variants_per_combination: clamped ?? undefined,
      });
    }
  );

  return (
    <div className="space-y-3">
      <span className="label-text font-medium">
        {t('generation-draft-format-label')}
      </span>

      <div className="form-control">
        <label className="label" htmlFor="format-aspect-ratio">
          <span className="label-text">
            {t('generation-draft-format-aspect-ratio-label')}
          </span>
        </label>
        <select
          id="format-aspect-ratio"
          className="select select-bordered"
          value={format.aspect_ratio ?? ''}
          disabled={disabled}
          onChange={(event) =>
            onUpdate({ ...format, aspect_ratio: event.target.value })
          }
        >
          <option value="">
            {t('generation-draft-format-aspect-ratio-placeholder')}
          </option>
          {ASPECT_RATIOS.map((ratio) => (
            <option key={ratio} value={ratio}>
              {ratio}
            </option>
          ))}
        </select>
      </div>

      <div className="form-control">
        <label className="label" htmlFor="format-resolution">
          <span className="label-text">
            {t('generation-draft-format-resolution-label')}
          </span>
        </label>
        <input
          id="format-resolution"
          type="text"
          className="input input-bordered"
          placeholder={t('generation-draft-format-resolution-placeholder')}
          value={resolution.value}
          disabled={disabled}
          onFocus={resolution.onFocus}
          onChange={(event) => resolution.onChange(event.target.value)}
          onBlur={resolution.onBlur}
          onKeyDown={resolution.onKeyDown}
        />
      </div>

      <div className="form-control">
        <label className="label" htmlFor="format-variant-count">
          <span className="label-text">
            {t('generation-draft-format-variant-count-label')}
          </span>
        </label>
        <div className="flex items-center gap-2">
          <input
            id="format-variant-count"
            type="number"
            min={1}
            max={maxVariants ?? undefined}
            className="input input-bordered w-32"
            value={variantCount.value}
            disabled={disabled}
            onFocus={variantCount.onFocus}
            onChange={(event) => variantCount.onChange(event.target.value)}
            onBlur={variantCount.onBlur}
            onKeyDown={variantCount.onKeyDown}
          />
          {maxVariants !== null && (
            <span className="text-xs text-base-content/60">
              {t('generation-draft-format-variant-count-max', {
                max: maxVariants,
              })}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
