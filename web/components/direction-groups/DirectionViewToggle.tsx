import classNames from 'classnames';
import { useTranslation } from 'next-i18next';

export type ResultsView = 'directions' | 'all';

interface DirectionViewToggleProps {
  value: ResultsView;
  onChange: (value: ResultsView) => void;
  /** Label of the ungrouped view: 全部条目 (Run) or 全部素材 (Assets). */
  allLabel: string;
}

/** 按方向 / 全部 switch shown only for a Run that came from a test plan. */
export function DirectionViewToggle({
  value,
  onChange,
  allLabel,
}: DirectionViewToggleProps) {
  const { t } = useTranslation('directions');
  const options: { key: ResultsView; label: string }[] = [
    { key: 'directions', label: t('view-directions') },
    { key: 'all', label: allLabel },
  ];
  return (
    <div
      className="join"
      role="radiogroup"
      aria-label={t('view-label')}
      data-testid="direction-view-toggle"
    >
      {options.map((option) => {
        const selected = value === option.key;
        return (
          <button
            key={option.key}
            type="button"
            role="radio"
            aria-checked={selected}
            className={classNames(
              'btn join-item btn-sm font-normal',
              selected ? 'btn-neutral' : 'btn-outline'
            )}
            onClick={() => {
              if (!selected) onChange(option.key);
            }}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
