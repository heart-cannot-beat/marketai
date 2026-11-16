import { useId } from 'react';
import { useTranslation } from 'next-i18next';
import { RIGHTS_SCOPES, isKnownRightsScope } from '@/lib/library/rights-scope';

interface RightsScopeSelectProps {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}

/**
 * The rights scope of a reference upload as a choice (#182): each option
 * shows a translated label and stores its code. A value outside the list --
 * a scope saved as free text before #182 -- is offered as it was saved, so
 * it still shows and, left alone, is what gets declared.
 */
export function RightsScopeSelect({
  value,
  onChange,
  disabled,
}: RightsScopeSelectProps) {
  const { t } = useTranslation('library');
  const id = useId();
  const hintId = `${id}-hint`;
  const saved = value.trim();
  const earlier = saved && !isKnownRightsScope(saved) ? saved : null;

  return (
    <div className="form-control w-full">
      <label className="label" htmlFor={id}>
        <span className="label-text">{t('references.rights-scope')}</span>
      </label>
      <select
        id={id}
        className="select select-bordered select-sm w-full"
        value={earlier ?? value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        aria-describedby={hintId}
      >
        {RIGHTS_SCOPES.map((scope) => (
          <option key={scope} value={scope}>
            {t(`references.rights-scope-options.${scope}`)}
          </option>
        ))}
        {earlier && (
          <option value={earlier}>
            {t('references.rights-scope-earlier', { value: earlier })}
          </option>
        )}
      </select>
      <p id={hintId} className="mt-1 text-xs text-gray-500 dark:text-gray-400">
        {t('references.rights-scope-hint')}
      </p>
    </div>
  );
}
