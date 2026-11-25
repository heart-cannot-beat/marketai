import { useId, useState } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { AdThumbnail } from '@/components/ideas/AdCard';
import { BUTTON, PRIMARY } from '@/components/ideas/PaidImageAction';
import type { TestInProgress } from '@/hooks/tests/useTestInProgress';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { errorMessage } from '@/lib/i18n/domain-errors';
import { formatDateTime } from '@/lib/i18n/format';
import { dayLabel, moneyLabel } from '@/lib/tests/take-to-test';
import { affectedComparisons } from '@/lib/tests/test-in-progress';

const NOTE_LIMIT = 2000;

/**
 * The numbers so far (design §5.7, #257): each compared group's spend,
 * orders, cost per order and the orders it is still short of a verdict, as
 * the server counts them -- marked as not a conclusion; reading them
 * evaluates nothing. Below, what ran differently from the plan: it can be
 * added at any time, and the page says first which groups then draw no
 * conclusion this round.
 */
export function TestNumbers({ run }: { run: TestInProgress }) {
  const { t, i18n } = useTranslation('tests');
  const id = useId();
  const progress = run.progress!;
  const money = (value: string) =>
    moneyLabel(i18n.language, value, progress.currency);
  const headlines = new Map(
    (run.round?.handoff?.entries ?? []).map((entry) => [
      entry.asset_version_id,
      entry.headline,
    ])
  );

  return (
    <section
      className="space-y-4"
      aria-labelledby={`${id}-title`}
      data-testid="test-numbers"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h3 id={`${id}-title`} className="text-lg font-semibold">
          {t('run-numbers-title')}
        </h3>
        <span className="rounded-full bg-amber-100 px-2 py-px text-xs font-semibold text-amber-900 dark:bg-amber-900 dark:text-amber-100">
          {t('run-numbers-provisional')}
        </span>
      </div>
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {progress.data_through
          ? t('run-numbers-through', {
              day: dayLabel(i18n.language, progress.data_through),
              result: dayLabel(i18n.language, progress.result_date),
            })
          : t('run-numbers-none', {
              result: dayLabel(i18n.language, progress.result_date),
            })}
      </p>
      <ul role="list" className="grid gap-3 lg:grid-cols-2">
        {progress.comparisons.map((comparison) => (
          <li
            key={comparison.id}
            className="space-y-3 rounded-lg border border-gray-200 p-3 dark:border-gray-700"
            data-testid="numbers-comparison"
          >
            <div className="flex items-center gap-3">
              <div className="flex shrink-0 gap-1">
                {comparison.asset_version_ids.map((version) => (
                  <AdThumbnail
                    key={version}
                    versionId={version}
                    alt={headlines.get(version) ?? comparison.label}
                    size="xs"
                  />
                ))}
              </div>
              <p className="min-w-0 break-words text-sm font-semibold">
                {comparison.label}
              </p>
            </div>
            <dl className="grid grid-cols-3 gap-2 text-sm">
              <div>
                <dt className="text-xs text-gray-600 dark:text-gray-400">
                  {t('run-numbers-spend')}
                </dt>
                <dd className="font-semibold">{money(comparison.spend)}</dd>
              </div>
              <div>
                <dt className="text-xs text-gray-600 dark:text-gray-400">
                  {t('run-numbers-orders')}
                </dt>
                <dd className="font-semibold">{comparison.purchases}</dd>
              </div>
              <div>
                <dt className="text-xs text-gray-600 dark:text-gray-400">
                  {t('run-numbers-cost', {
                    target: money(progress.target_cost),
                  })}
                </dt>
                <dd className="font-semibold">
                  {comparison.cost_per_purchase
                    ? money(comparison.cost_per_purchase)
                    : t('run-numbers-no-orders')}
                </dd>
              </div>
            </dl>
            <p className="text-sm">
              {comparison.remaining_purchases > 0
                ? t('run-numbers-short', {
                    count: comparison.remaining_purchases,
                    min: progress.min_purchases,
                  })
                : t('run-numbers-enough', { min: progress.min_purchases })}
            </p>
            {comparison.conditions_changed && (
              <p className="text-sm font-semibold text-amber-800 dark:text-amber-200">
                {t('run-numbers-deviated')}
              </p>
            )}
          </li>
        ))}
      </ul>
      <Deviations run={run} headlines={headlines} />
    </section>
  );
}

/** What ran differently from the plan: the list, and adding one after a warning. */
function Deviations({
  run,
  headlines,
}: {
  run: TestInProgress;
  headlines: Map<string, string>;
}) {
  const { t, i18n } = useTranslation('tests');
  const { t: tc } = useTranslation('common');
  const id = useId();
  const progress = run.progress!;
  const entries = run.round?.handoff?.entries ?? [];
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [note, setNote] = useState('');
  const [warned, setWarned] = useState(false);
  const [problem, setProblem] = useState<ProblemDetails | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const affected = affectedComparisons(progress.comparisons, picked);
  const labels = new Map(progress.comparisons.map((c) => [c.id, c.label]));
  // Ads of one package can share a headline: their group tells them apart.
  const groups = new Map(
    progress.comparisons.flatMap((c) =>
      c.asset_version_ids.map((version) => [version, c.label] as const)
    )
  );

  function reset() {
    setOpen(false);
    setPicked([]);
    setNote('');
    setWarned(false);
    setProblem(null);
    setUncertain(false);
  }

  async function record() {
    const outcome = await run.recordDeviation(picked, note);
    setUncertain(outcome.kind === 'uncertain');
    if (outcome.kind === 'refused') setProblem(outcome.problem);
    if (outcome.kind === 'done') reset();
  }

  return (
    <div className="space-y-3 border-t border-gray-200 pt-4 dark:border-gray-700">
      <h4 id={`${id}-title`} className="font-semibold">
        {t('run-deviation-title')}
      </h4>
      {progress.deviations.length > 0 ? (
        <ul role="list" className="space-y-2 text-sm" data-testid="deviations">
          {progress.deviations.map((deviation) => (
            <li
              key={deviation.id}
              className="rounded-lg bg-gray-50 p-3 dark:bg-gray-900"
            >
              <p className="break-words">{deviation.note}</p>
              <p className="text-xs text-gray-600 dark:text-gray-400">
                {t('run-deviation-record', {
                  groups: deviation.comparison_ids
                    .map((comparison) => labels.get(comparison) ?? comparison)
                    .join(tc('list-comma')),
                  at: formatDateTime(i18n.language, deviation.created_at),
                })}
              </p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {t('run-deviation-none')}
        </p>
      )}

      {!open ? (
        <button type="button" className={BUTTON} onClick={() => setOpen(true)}>
          {t('run-deviation-add')}
        </button>
      ) : (
        <form
          className="space-y-3 rounded-lg border border-gray-200 p-3 dark:border-gray-700 sm:p-4"
          aria-labelledby={`${id}-title`}
          onSubmit={(event) => {
            event.preventDefault();
            if (!warned) setWarned(true);
            else void record();
          }}
          data-testid="deviation-form"
        >
          <fieldset className="space-y-2">
            <legend className="text-sm font-semibold">
              {t('run-deviation-which')}
            </legend>
            <ul role="list" className="grid gap-2 sm:grid-cols-2">
              {entries.map((entry) => (
                <li key={entry.entry_id}>
                  <label className="flex min-h-11 cursor-pointer items-center gap-3 rounded-lg border border-gray-200 p-2 text-sm dark:border-gray-700">
                    <input
                      type="checkbox"
                      className="checkbox"
                      checked={picked.includes(entry.asset_version_id)}
                      onChange={(event) => {
                        setWarned(false);
                        setPicked(
                          event.target.checked
                            ? [...picked, entry.asset_version_id]
                            : picked.filter(
                                (version) => version !== entry.asset_version_id
                              )
                        );
                      }}
                    />
                    <AdThumbnail
                      versionId={entry.asset_version_id}
                      alt={headlines.get(entry.asset_version_id) ?? ''}
                      size="xs"
                    />
                    <span className="min-w-0 break-words">
                      {entry.headline}
                      {groups.get(entry.asset_version_id) && (
                        <span className="block text-xs text-gray-600 dark:text-gray-400">
                          {groups.get(entry.asset_version_id)}
                        </span>
                      )}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </fieldset>
          <label className="block space-y-1 text-sm">
            <span className="font-semibold">{t('run-deviation-note')}</span>
            <textarea
              className="textarea textarea-bordered min-h-20 w-full text-base"
              maxLength={NOTE_LIMIT}
              value={note}
              placeholder={t('run-deviation-note-placeholder')}
              onChange={(event) => {
                setWarned(false);
                setNote(event.target.value);
              }}
            />
          </label>
          {warned && (
            <div
              className="space-y-1 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
              role="alert"
              data-testid="deviation-warning"
            >
              <p className="font-semibold">
                {t('run-deviation-warning', {
                  count: affected.length,
                  groups: affected
                    .map((comparison) => comparison.label)
                    .join(tc('list-comma')),
                })}
              </p>
              <p>{t('run-deviation-warning-kept')}</p>
            </div>
          )}
          <div className="flex flex-wrap gap-3">
            <button
              className={classNames(PRIMARY, 'w-full sm:w-auto')}
              disabled={run.busy || !picked.length || !note.trim()}
            >
              {warned ? t('run-deviation-confirm') : t('run-deviation-save')}
            </button>
            <button type="button" className={BUTTON} onClick={reset}>
              {t('run-cancel-edit')}
            </button>
          </div>
          {uncertain && (
            <p className="text-sm" role="status">
              {t('run-no-answer')}
            </p>
          )}
          {problem && (
            <p className="text-sm text-red-700 dark:text-red-300" role="alert">
              {errorMessage(t, problem)}
            </p>
          )}
        </form>
      )}
    </div>
  );
}
