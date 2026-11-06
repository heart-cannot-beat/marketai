import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';

interface MetaModeSelectProps {
  value: ScaleMetaMode;
  onChange: (mode: ScaleMetaMode) => void;
}

/** `list_scale_available_modes`'s response has no declared `response_model`
 * server-side (`app.scale.v1_router.list_available_meta_modes_route`), so
 * the generated `DomainOperationMap` types its body as `null`; the actual
 * JSON shape is `{modes: string[]}`. */
interface AvailableModesResponse {
  modes: string[];
}

/**
 * Server-driven mode selector (spec §3.11, Issue #86 Task 15).
 * `list_scale_available_modes` returns exactly the Meta modes this
 * request's identity may select (`app.platform.meta_mode_auth.
 * list_available_meta_modes`) -- a delegated user never even sees
 * `internal_validation` as an option (ADR 0009), rather than being allowed
 * to pick it client-side and rejected only by the server. This never
 * performs a Meta write regardless of which mode is selected (Scale never
 * performs Meta writes, spec constraint); it only selects which mode's
 * already-synced evidence the reads below scope to.
 */
export function MetaModeSelect({ value, onChange }: MetaModeSelectProps) {
  const { t } = useTranslation('scale');
  const { data } = useDomainQuery('list_scale_available_modes');
  const modes = (data as AvailableModesResponse | undefined)?.modes;
  // While loading (or if the server ever returns an empty list), keep the
  // current value selectable rather than rendering an empty `<select>`.
  const options = modes && modes.length > 0 ? modes : [value];

  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-base-content/70">{t('meta-mode.label')}</span>
      <select
        className="select select-sm select-bordered"
        value={value}
        onChange={(e) => onChange(e.target.value as ScaleMetaMode)}
        aria-label={t('meta-mode.label')}
      >
        {options.map((mode) => (
          <option key={mode} value={mode}>
            {t(`meta-mode.${mode}`, { defaultValue: mode })}
          </option>
        ))}
      </select>
    </label>
  );
}
