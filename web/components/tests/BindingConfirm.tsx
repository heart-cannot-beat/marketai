import { useId, useMemo, useState } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { AdThumbnail } from '@/components/ideas/AdCard';
import { PRIMARY } from '@/components/ideas/PaidImageAction';
import {
  bindingChanges,
  entryName,
  initialChoices,
  matchedCount,
  type BindingChoice,
  type BindingSuggestion,
  type BindingSuggestions,
} from '@/lib/tests/test-in-progress';

const SELECT =
  'select select-bordered min-h-12 w-full text-base dark:bg-gray-900';

/** What the page says about each Meta ad, by how its name matched. */
const STATUS_TONE: Record<BindingSuggestion['status'], string> = {
  bound: 'text-emerald-700 dark:text-emerald-300',
  suggested: 'text-gray-600 dark:text-gray-400',
  no_match: 'text-amber-800 dark:text-amber-200',
  ambiguous: 'text-amber-800 dark:text-amber-200',
  bound_elsewhere: 'text-amber-800 dark:text-amber-200',
};

/**
 * Which ad each Meta ad of the report shows (design §5.7, #257). The short
 * keys in the Meta ad names fill in the choices (the package's MA key first,
 * then the image's MK code); a Meta ad with no key, a key several ads share,
 * or a match in another test is left for the user to choose or skip. One
 * confirmation writes every choice at once; nothing is stored before it.
 * Once a report of the round is counted, existing matches stay as they are
 * and only new Meta ads can be matched.
 */
export function BindingConfirm({
  suggestions,
  busy,
  onConfirm,
}: {
  suggestions: BindingSuggestions;
  busy: boolean;
  /** Only the choices that change something; an empty list means nothing to write. */
  onConfirm: (changes: BindingChoice[]) => void;
}) {
  const { t } = useTranslation('tests');
  const id = useId();
  const [choices, setChoices] = useState(() => initialChoices(suggestions));
  const entries = useMemo(
    () => new Map(suggestions.entries.map((entry) => [entry.entry_id, entry])),
    [suggestions.entries]
  );
  const changes = bindingChanges(suggestions, choices);
  const matched = matchedCount(suggestions, choices);
  const open = suggestions.ads.filter(
    (ad) => ad.status !== 'bound' && ad.status !== 'suggested'
  ).length;

  return (
    <section
      className="space-y-4"
      aria-labelledby={`${id}-title`}
      data-testid="binding-confirm"
    >
      <div className="space-y-1">
        <h4 id={`${id}-title`} className="font-semibold">
          {t('run-bind-title')}
        </h4>
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {suggestions.ad_name_column
            ? t('run-bind-intro')
            : t('run-bind-no-names')}
        </p>
        {open > 0 && (
          <p className="text-sm font-medium" role="status">
            {t('run-bind-open', { count: open })}
          </p>
        )}
        {suggestions.locked && (
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t('run-bind-locked')}
          </p>
        )}
      </div>

      <ul
        role="list"
        className="divide-y divide-gray-200 border-y border-gray-200 dark:divide-gray-700 dark:border-gray-700"
      >
        {suggestions.ads.map((ad) => {
          const chosen = choices[ad.ad_id] ?? '';
          const entry = chosen ? entries.get(chosen) : undefined;
          const fixed = suggestions.locked && ad.status === 'bound';
          const candidates = new Set(ad.candidate_entry_ids);
          const ordered = [
            ...suggestions.entries.filter((e) => candidates.has(e.entry_id)),
            ...suggestions.entries.filter((e) => !candidates.has(e.entry_id)),
          ];
          const selectId = `${id}-${ad.ad_id}`;
          return (
            <li
              key={ad.ad_id}
              className="flex flex-col gap-3 py-3 sm:flex-row sm:items-center"
              data-testid="binding-row"
            >
              <div className="min-w-0 flex-1 space-y-1 text-sm">
                <p className="break-words font-semibold">
                  {ad.ad_names[0] ?? t('run-bind-unnamed')}
                </p>
                <p className="text-xs text-gray-600 dark:text-gray-400">
                  {t('run-bind-ad-id', { id: ad.ad_id })}
                </p>
                <p className={classNames('text-xs', STATUS_TONE[ad.status])}>
                  {ad.status === 'suggested'
                    ? t(`run-bind-suggested-${ad.key_kind ?? 'ma_key'}`)
                    : t(`run-bind-status-${ad.status}`)}
                </p>
              </div>
              <div className="flex items-center gap-3 sm:w-80">
                {entry ? (
                  <AdThumbnail
                    versionId={entry.asset_version_id}
                    alt={entryName(entry)}
                    size="xs"
                  />
                ) : (
                  <span
                    aria-hidden="true"
                    className="aspect-[4/5] w-10 shrink-0 rounded-md border border-dashed border-gray-300 dark:border-gray-600"
                  />
                )}
                <label htmlFor={selectId} className="sr-only">
                  {t('run-bind-choice', {
                    name: ad.ad_names[0] ?? ad.ad_id,
                  })}
                </label>
                <select
                  id={selectId}
                  className={SELECT}
                  value={chosen}
                  disabled={fixed || busy}
                  onChange={(event) =>
                    setChoices({ ...choices, [ad.ad_id]: event.target.value })
                  }
                >
                  <option value="">{t('run-bind-skip')}</option>
                  {ordered.map((option) => (
                    <option key={option.entry_id} value={option.entry_id}>
                      {entryName(option)}
                    </option>
                  ))}
                </select>
              </div>
            </li>
          );
        })}
      </ul>

      <div className="space-y-2">
        <button
          type="button"
          className={classNames(PRIMARY, 'w-full sm:w-auto')}
          disabled={busy || matched === 0}
          onClick={() => onConfirm(changes)}
          data-testid="binding-confirm-button"
        >
          {changes.length
            ? t('run-bind-confirm', { count: matched })
            : t('run-bind-keep')}
        </button>
        {matched === 0 && (
          <p className="text-xs text-gray-600 dark:text-gray-400">
            {t('run-bind-none')}
          </p>
        )}
      </div>
    </section>
  );
}
