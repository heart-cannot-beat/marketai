import { useId, useState, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { BUTTON, PRIMARY } from '@/components/ideas/PaidImageAction';
import type { TestInProgress } from '@/hooks/tests/useTestInProgress';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { errorMessage } from '@/lib/i18n/domain-errors';
import { formatDateTime } from '@/lib/i18n/format';
import { dayLabel } from '@/lib/tests/take-to-test';

const INPUT = 'input input-bordered min-h-12 w-full text-base dark:bg-gray-900';
const REASON_LIMIT = 500;

/**
 * The round's schedule (design §5.7, #257): the days it runs and the day
 * its result can be read, all as the server has them. Before the first
 * report is counted the start can move to the day the ads went live (the
 * end moves with it, the number of days stays); the round can end early,
 * and the result follows once the attribution window after that day has
 * passed. Each change is added to the record; the planned days stay.
 */
export function TestSchedule({ run }: { run: TestInProgress }) {
  const { t, i18n } = useTranslation('tests');
  const id = useId();
  const schedule = run.schedule!;
  const day = (value: string) => dayLabel(i18n.language, value);
  const [editing, setEditing] = useState<'start' | 'end' | null>(null);
  const [date, setDate] = useState('');
  const [reason, setReason] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [problem, setProblem] = useState<ProblemDetails | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const closed = run.round?.state === 'closed';

  function open(kind: 'start' | 'end') {
    setEditing(kind);
    setDate('');
    setReason('');
    setConfirming(false);
    setProblem(null);
    setUncertain(false);
  }

  async function save() {
    if (!editing || !date) return;
    const outcome =
      editing === 'start'
        ? await run.moveStart(date, reason)
        : await run.endEarly(date, reason);
    setUncertain(outcome.kind === 'uncertain');
    if (outcome.kind === 'refused') setProblem(outcome.problem);
    if (outcome.kind === 'done') setEditing(null);
  }

  return (
    <section
      className="space-y-4"
      aria-labelledby={`${id}-title`}
      data-testid="test-schedule"
    >
      <h3 id={`${id}-title`} className="text-lg font-semibold">
        {t('run-schedule-title')}
      </h3>
      <dl className="grid gap-3 text-sm sm:grid-cols-3">
        <Fact label={t('run-schedule-start')}>
          {day(schedule.effective_start_date)}
          {schedule.effective_start_date !== schedule.start_date && (
            <span className="block text-xs text-gray-600 dark:text-gray-400">
              {t('run-schedule-planned', { day: day(schedule.start_date) })}
            </span>
          )}
        </Fact>
        <Fact label={t('run-schedule-end')}>
          {day(schedule.effective_end_date)}
          {schedule.ended_early ? (
            <span className="block text-xs font-semibold text-amber-800 dark:text-amber-200">
              {t('run-schedule-ended-early')}
            </span>
          ) : (
            schedule.effective_end_date !== schedule.end_date && (
              <span className="block text-xs text-gray-600 dark:text-gray-400">
                {t('run-schedule-planned', { day: day(schedule.end_date) })}
              </span>
            )
          )}
        </Fact>
        <Fact label={t('run-schedule-result')}>
          {day(schedule.result_date)}
        </Fact>
      </dl>

      {!closed && editing === null && (
        <div className="flex flex-wrap gap-3">
          {schedule.start_movable && (
            <button
              type="button"
              className={BUTTON}
              onClick={() => open('start')}
            >
              {t('run-schedule-move')}
            </button>
          )}
          <button type="button" className={BUTTON} onClick={() => open('end')}>
            {schedule.ended_early
              ? t('run-schedule-end-correct')
              : t('run-schedule-end-early')}
          </button>
        </div>
      )}
      {!closed && !schedule.start_movable && editing === null && (
        <p className="text-xs text-gray-600 dark:text-gray-400">
          {schedule.ended_early
            ? t('run-schedule-start-fixed-ended')
            : t('run-schedule-start-fixed')}
        </p>
      )}

      {editing && (
        <form
          className="space-y-3 rounded-lg border border-gray-200 p-3 dark:border-gray-700 sm:p-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (editing === 'end' && !confirming) setConfirming(true);
            else void save();
          }}
          data-testid={`schedule-${editing}-form`}
        >
          <p className="text-sm">
            {editing === 'start'
              ? t('run-schedule-move-help')
              : t('run-schedule-end-help')}
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block space-y-1 text-sm">
              <span className="font-semibold">
                {editing === 'start'
                  ? t('run-schedule-move-date')
                  : t('run-schedule-end-date')}
              </span>
              <input
                className={INPUT}
                type="date"
                value={date}
                onChange={(event) => {
                  setDate(event.target.value);
                  setConfirming(false);
                }}
              />
            </label>
            <label className="block space-y-1 text-sm">
              <span className="font-semibold">{t('run-schedule-reason')}</span>
              <input
                className={INPUT}
                maxLength={REASON_LIMIT}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
          </div>
          {confirming && (
            <p
              className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
              role="alert"
            >
              {t('run-schedule-end-warning', { day: day(date) })}
            </p>
          )}
          <div className="flex flex-wrap gap-3">
            <button
              className={classNames(PRIMARY, 'w-full sm:w-auto')}
              disabled={run.busy || !date}
            >
              {editing === 'start'
                ? t('run-schedule-move-save')
                : confirming
                  ? t('run-schedule-end-confirm')
                  : t('run-schedule-end-save')}
            </button>
            <button
              type="button"
              className={BUTTON}
              onClick={() => setEditing(null)}
            >
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

      {schedule.changes.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer font-medium">
            {t('run-schedule-changes', { count: schedule.changes.length })}
          </summary>
          <ul role="list" className="mt-2 space-y-1">
            {schedule.changes.map((change) => (
              <li key={change.id}>
                {t(`run-schedule-change-${change.kind}`, {
                  day: day(change.date),
                  at: formatDateTime(i18n.language, change.created_at),
                })}
                {change.reason && (
                  <span className="text-gray-600 dark:text-gray-400">
                    {' '}
                    {t('run-schedule-change-reason', { reason: change.reason })}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900">
      <dt className="text-xs text-gray-600 dark:text-gray-400">{label}</dt>
      <dd className="font-semibold">{children}</dd>
    </div>
  );
}
