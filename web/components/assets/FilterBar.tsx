import { useCallback } from 'react';
import { useTranslation } from 'next-i18next';
import {
  executionModeLabelKey,
  originLabelKey,
  qcStatus,
  reviewStatus,
} from '@/lib/assets/status-labels';

interface FacetValue {
  value: string;
  count: number;
}

export interface AssetFacets {
  providers: FacetValue[];
  reviews: FacetValue[];
  origins: FacetValue[];
  modes: FacetValue[];
  aspects: FacetValue[];
  qc_states: FacetValue[];
  tags: FacetValue[];
}

type FilterKey =
  | 'provider'
  | 'reviewValue'
  | 'originKind'
  | 'executionMode'
  | 'aspectRatio'
  | 'qcState'
  | 'tag';

interface FilterBarProps {
  facets: AssetFacets;
  activeFilters: Partial<Record<FilterKey, string>>;
  onFilterChange: (key: FilterKey, value: string | undefined) => void;
}

/** Facet values that are domain enums get their Chinese label (Issue
 * #191); provider names, aspect ratios and tags are shown as they are. The
 * filter itself always sends the raw value. */
const VALUE_LABEL_KEY: Partial<Record<FilterKey, (value: string) => string>> = {
  reviewValue: (value) => reviewStatus(value).labelKey,
  originKind: originLabelKey,
  executionMode: executionModeLabelKey,
  qcState: (value) => qcStatus(value).labelKey,
};

interface ChipGroupProps {
  label: string;
  filterKey: FilterKey;
  values: FacetValue[];
  active: string | undefined;
  onChange: (key: FilterKey, value: string | undefined) => void;
}

function ChipGroup({
  label,
  filterKey,
  values,
  active,
  onChange,
}: ChipGroupProps) {
  const { t } = useTranslation('assets');
  const handleClick = useCallback(
    (value: string) => {
      onChange(filterKey, active === value ? undefined : value);
    },
    [filterKey, active, onChange]
  );

  if (values.length === 0) return null;
  const labelKeyFor = VALUE_LABEL_KEY[filterKey];

  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <span className="text-xs font-medium text-base-content/50 uppercase tracking-wide">
        {label}
      </span>
      {values.map((v) => {
        const isActive = active === v.value;
        return (
          <button
            key={v.value}
            type="button"
            role="checkbox"
            aria-checked={isActive}
            data-testid={`filter-${filterKey}-${v.value}`}
            className={`badge gap-1 cursor-pointer transition-colors ${
              isActive
                ? 'badge-primary'
                : 'badge-outline badge-ghost hover:badge-primary/20'
            }`}
            onClick={() => handleClick(v.value)}
          >
            {labelKeyFor ? t(labelKeyFor(v.value), { ns: 'common' }) : v.value}
            <span className="text-xs opacity-60">{v.count}</span>
          </button>
        );
      })}
    </div>
  );
}

export function FilterBar({
  facets,
  activeFilters,
  onFilterChange,
}: FilterBarProps) {
  const { t } = useTranslation('assets');
  return (
    <div className="flex flex-col gap-2 px-4 py-2 border-b border-base-300">
      <ChipGroup
        label={t('filter-provider')}
        filterKey="provider"
        values={facets.providers}
        active={activeFilters.provider}
        onChange={onFilterChange}
      />
      <ChipGroup
        label={t('filter-review')}
        filterKey="reviewValue"
        values={facets.reviews}
        active={activeFilters.reviewValue}
        onChange={onFilterChange}
      />
      <ChipGroup
        label={t('filter-origin')}
        filterKey="originKind"
        values={facets.origins}
        active={activeFilters.originKind}
        onChange={onFilterChange}
      />
      <ChipGroup
        label={t('filter-mode')}
        filterKey="executionMode"
        values={facets.modes}
        active={activeFilters.executionMode}
        onChange={onFilterChange}
      />
      <ChipGroup
        label={t('filter-aspect')}
        filterKey="aspectRatio"
        values={facets.aspects}
        active={activeFilters.aspectRatio}
        onChange={onFilterChange}
      />
      <ChipGroup
        label={t('filter-qc')}
        filterKey="qcState"
        values={facets.qc_states}
        active={activeFilters.qcState}
        onChange={onFilterChange}
      />
      <ChipGroup
        label={t('filter-tag')}
        filterKey="tag"
        values={facets.tags}
        active={activeFilters.tag}
        onChange={onFilterChange}
      />
    </div>
  );
}
