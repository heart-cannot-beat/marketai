import { useId, useState, type FormEvent } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { BUTTON, PRIMARY } from '@/components/ideas/PaidImageAction';
import { metricsIssueText } from '@/components/workspace/marketing-issues';
import {
  columnMapping,
  readReportFile,
  REPORT_COLUMNS,
} from '@/components/workspace/marketing-report-file';
import type { TestInProgress } from '@/hooks/tests/useTestInProgress';
import type { TestCommandOutcome } from '@/hooks/tests/useTestStart';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { errorMessage } from '@/lib/i18n/domain-errors';
import {
  ATTRIBUTION_WINDOWS,
  dayLabel,
  moneyLabel,
  TEST_CURRENCIES,
  type AttributionWindow,
  type TestCurrency,
} from '@/lib/tests/take-to-test';
import {
  hasConfirmedReport,
  startDateQuestion,
  type BindingChoice,
  type BindingSuggestions,
  type TestReport,
} from '@/lib/tests/test-in-progress';
import { BindingConfirm } from './BindingConfirm';

type Issue = components['schemas']['MetricsIssue'];

const INPUT = 'input input-bordered min-h-12 w-full text-base dark:bg-gray-900';
const SELECT =
  'select select-bordered min-h-12 w-full text-base dark:bg-gray-900';
const ACCOUNT = /^(act_)?[0-9]+$/;
/** The issues that say the report's settings are not the test's. */
const SETTINGS_CODES = new Set([
  'report_timezone_mismatch',
  'report_currency_mismatch',
  'report_attribution_mismatch',
]);

/**
 * Send Meta's report back (design §5.7, #257): the full daily report from
 * the start of the test to today, every time. The upload says what it
 * would count; a time zone, currency or attribution window that is not the
 * test's is said plainly and stops it. Then the Meta ads are matched to the
 * package's ads (prefilled from their names, confirmed once); on the first
 * report, a first day that is not the round's start is asked about; then
 * the report counts. A later full report replaces the days it repeats, the
 * earlier one stays on record -- that is how a report is corrected.
 */
export function ReportUpload({ run }: { run: TestInProgress }) {
  const { t, i18n } = useTranslation('tests');
  const { t: tc } = useTranslation('common');
  const id = useId();
  const round = run.round!;
  const frozen = round.snapshot;
  const counted = hasConfirmedReport(round);

  const [text, setText] = useState('');
  const [filename, setFilename] = useState('');
  const [fileError, setFileError] = useState('');
  const [account, setAccount] = useState(
    round.bindings[0]?.ad_account_id ??
      round.batches[round.batches.length - 1]?.metadata.ad_account_id ??
      ''
  );
  const [timezone, setTimezone] = useState(round.timezone);
  const [currency, setCurrency] = useState<TestCurrency>(
    frozen.currency as TestCurrency
  );
  const [attribution, setAttribution] = useState<AttributionWindow>(
    frozen.attribution_window as AttributionWindow
  );
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [fileKey, setFileKey] = useState(0);

  const [report, setReport] = useState<TestReport | null>(null);
  const [suggestions, setSuggestions] = useState<BindingSuggestions | null>(
    null
  );
  const [matched, setMatched] = useState(false);
  const [keptStart, setKeptStart] = useState(false);
  const [problem, setProblem] = useState<ProblemDetails | null>(null);
  const [uncertain, setUncertain] = useState(false);

  const question = matched ? startDateQuestion(report, run.schedule) : null;
  const accountValid = ACCOUNT.test(account.trim());

  function settle<T>(outcome: TestCommandOutcome<T>): T | null {
    setUncertain(outcome.kind === 'uncertain');
    if (outcome.kind === 'done') {
      setProblem(null);
      return outcome.data;
    }
    if (outcome.kind === 'refused') setProblem(outcome.problem);
    return null;
  }

  function startOver() {
    setReport(null);
    setSuggestions(null);
    setMatched(false);
    setKeptStart(false);
    setProblem(null);
    setUncertain(false);
    setText('');
    setFilename('');
    setFileKey((key) => key + 1);
  }

  async function upload(event: FormEvent) {
    event.preventDefault();
    const uploaded = settle(
      await run.preview({
        csv_text: text,
        filename,
        ad_account_id: account.trim(),
        account_timezone: timezone.trim(),
        currency,
        attribution_window: attribution,
        source_mode: 'customer_file',
        field_mapping: columnMapping(mapping),
      })
    );
    if (!uploaded) return;
    setReport(uploaded);
    setSuggestions(null);
    setMatched(false);
    setKeptStart(false);
    if (!uploaded.issues.length) await readNames(uploaded.id);
  }

  async function readNames(batchId: string) {
    const names = settle(await run.suggest(batchId));
    if (names) setSuggestions(names);
  }

  async function confirmMatches(changes: BindingChoice[]) {
    if (!suggestions) return;
    if (!changes.length) {
      setMatched(true);
      return;
    }
    if (settle(await run.confirmBindings(suggestions.ad_account_id, changes)))
      setMatched(true);
  }

  async function moveStart(day: string) {
    settle(await run.moveStart(day));
  }

  async function count() {
    if (!report) return;
    const confirmed = settle(await run.confirmReport(report.id));
    if (confirmed) setReport(confirmed);
  }

  const issueText = (issue: Issue) => {
    if (issue.code !== 'report_attribution_mismatch')
      return metricsIssueText(tc, issue);
    const params = (issue.params ?? {}) as Record<string, string>;
    return metricsIssueText(tc, {
      ...issue,
      params: {
        expected: t(`take-attribution-${params.expected}`),
        found: t(`take-attribution-${params.found}`),
      },
    });
  };

  const settings = t('run-report-settings', {
    timezone: round.timezone,
    currency: frozen.currency,
    attribution: t(`take-attribution-${frozen.attribution_window}`),
  });

  return (
    <div className="space-y-5" data-testid="report-upload">
      {!report && (
        <form className="space-y-4" onSubmit={(event) => void upload(event)}>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {counted ? t('run-report-again') : t('run-report-first')}
          </p>
          <label className="block space-y-1 text-sm">
            <span className="font-semibold">{t('run-report-file')}</span>
            <input
              key={fileKey}
              className="file-input file-input-bordered min-h-12 w-full text-base"
              type="file"
              accept=".csv,text/csv"
              disabled={run.busy}
              onChange={(event) => {
                setText('');
                setFilename('');
                setReport(null);
                setSuggestions(null);
                const file = event.target.files?.[0];
                if (!file) return;
                void readReportFile(file).then((read) => {
                  if (read.ok) {
                    setText(read.text);
                    setFilename(read.filename);
                    setFileError('');
                  } else
                    setFileError(
                      tc(
                        read.reason === 'too_large'
                          ? 'marketing-csv-size-limit'
                          : 'marketing-file-read-failed'
                      )
                    );
                });
              }}
            />
          </label>
          {fileError && (
            <p className="text-sm text-red-700 dark:text-red-300" role="alert">
              {fileError}
            </p>
          )}
          <div className="space-y-1 text-sm">
            <label htmlFor={`${id}-account`} className="block font-semibold">
              {t('run-report-account')}
            </label>
            <input
              id={`${id}-account`}
              className={INPUT}
              value={account}
              inputMode="numeric"
              autoComplete="off"
              aria-describedby={`${id}-account-help`}
              aria-invalid={Boolean(account.trim()) && !accountValid}
              onChange={(event) => setAccount(event.target.value)}
            />
            <p
              id={`${id}-account-help`}
              className="text-xs text-gray-600 dark:text-gray-400"
            >
              {account.trim() && !accountValid
                ? t('run-report-account-invalid')
                : t('run-report-account-help')}
            </p>
          </div>
          <p className="text-sm">{settings}</p>
          <details className="text-sm">
            <summary className="cursor-pointer font-medium">
              {t('run-report-settings-differ')}
            </summary>
            <div className="mt-3 grid gap-3 sm:grid-cols-3">
              <label className="block space-y-1">
                <span>{t('take-default-timezone')}</span>
                <input
                  className={INPUT}
                  value={timezone}
                  onChange={(event) => setTimezone(event.target.value)}
                />
              </label>
              <label className="block space-y-1">
                <span>{t('take-default-currency')}</span>
                <select
                  className={SELECT}
                  value={currency}
                  onChange={(event) =>
                    setCurrency(event.target.value as TestCurrency)
                  }
                >
                  {TEST_CURRENCIES.map((code) => (
                    <option key={code} value={code}>
                      {code}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block space-y-1">
                <span>{t('take-default-attribution')}</span>
                <select
                  className={SELECT}
                  value={attribution}
                  onChange={(event) =>
                    setAttribution(event.target.value as AttributionWindow)
                  }
                >
                  {ATTRIBUTION_WINDOWS.map((window) => (
                    <option key={window} value={window}>
                      {t(`take-attribution-${window}`)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <p className="mt-2 text-xs text-gray-600 dark:text-gray-400">
              {t('run-report-settings-help')}
            </p>
          </details>
          <details className="text-sm">
            <summary className="cursor-pointer font-medium">
              {tc('marketing-custom-column-mapping')}
            </summary>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              {REPORT_COLUMNS.map((field) => (
                <label key={field} className="block space-y-1">
                  <span>{tc(`marketing-column-${field}`)}</span>
                  <input
                    className={INPUT}
                    value={mapping[field] ?? ''}
                    onChange={(event) =>
                      setMapping({ ...mapping, [field]: event.target.value })
                    }
                  />
                </label>
              ))}
            </div>
            <p className="mt-2 text-xs text-gray-600 dark:text-gray-400">
              {tc('marketing-column-mapping-help')}
            </p>
          </details>
          <button
            className={classNames(PRIMARY, 'w-full sm:w-auto')}
            disabled={
              run.busy ||
              !text ||
              !filename ||
              !accountValid ||
              !timezone.trim()
            }
            data-testid="report-upload-button"
          >
            {t('run-report-upload')}
          </button>
        </form>
      )}

      {report && report.state === 'preview' && (
        <section
          className="space-y-4 rounded-lg border border-gray-200 p-3 dark:border-gray-700 sm:p-4"
          aria-labelledby={`${id}-preview`}
          data-testid="report-preview"
        >
          <div className="space-y-1">
            <h4 id={`${id}-preview`} className="font-semibold">
              {t('run-report-preview-title', { filename: report.filename })}
            </h4>
            <p className="text-sm text-gray-600 dark:text-gray-400">
              {report.first_date && report.last_date
                ? t('run-report-preview-days', {
                    count: report.row_count,
                    first: dayLabel(i18n.language, report.first_date),
                    last: dayLabel(i18n.language, report.last_date),
                  })
                : t('run-report-preview-empty')}
            </p>
          </div>
          {report.issues.length > 0 ? (
            <div className="space-y-2" role="alert">
              <p className="text-sm font-semibold text-red-700 dark:text-red-300">
                {t('run-report-issues', { count: report.issues.length })}
              </p>
              <ul className="max-h-64 space-y-1 overflow-y-auto text-sm">
                {report.issues.map((issue, i) => (
                  <li key={i}>{issueText(issue)}</li>
                ))}
              </ul>
              {report.issues.some((issue) =>
                SETTINGS_CODES.has(issue.code)
              ) && (
                <p className="text-sm">
                  {t('run-report-settings-mismatch', { settings })}
                </p>
              )}
              <button type="button" className={BUTTON} onClick={startOver}>
                {t('run-report-choose-again')}
              </button>
            </div>
          ) : (
            <>
              {!suggestions && (problem || uncertain) && (
                <button
                  type="button"
                  className={BUTTON}
                  disabled={run.busy}
                  onClick={() => void readNames(report.id)}
                >
                  {t('take-retry')}
                </button>
              )}
              {suggestions && !matched && (
                <BindingConfirm
                  suggestions={suggestions}
                  busy={run.busy}
                  onConfirm={(changes) => void confirmMatches(changes)}
                />
              )}
              {matched && (
                <p className="text-sm" role="status">
                  <span aria-hidden="true">{'✓ '}</span>
                  {t('run-bind-done')}
                </p>
              )}
              {question && !keptStart && (
                <div
                  className="space-y-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
                  data-testid="start-date-question"
                >
                  <p className="text-sm font-semibold">
                    {t('run-start-question', {
                      day: dayLabel(i18n.language, question),
                    })}
                  </p>
                  <p className="text-sm">
                    {t('run-start-question-help', {
                      day: dayLabel(
                        i18n.language,
                        run.schedule!.effective_start_date
                      ),
                    })}
                  </p>
                  <div className="flex flex-wrap gap-3">
                    <button
                      type="button"
                      className={PRIMARY}
                      disabled={run.busy}
                      onClick={() => void moveStart(question)}
                    >
                      {t('run-start-question-move', {
                        day: dayLabel(i18n.language, question),
                      })}
                    </button>
                    <button
                      type="button"
                      className={BUTTON}
                      disabled={run.busy}
                      onClick={() => setKeptStart(true)}
                    >
                      {t('run-start-question-keep')}
                    </button>
                  </div>
                </div>
              )}
              {matched && (!question || keptStart) && (
                <div className="space-y-2">
                  <button
                    type="button"
                    className={classNames(PRIMARY, 'w-full sm:w-auto')}
                    disabled={run.busy}
                    onClick={() => void count()}
                    data-testid="report-confirm-button"
                  >
                    {t('run-report-confirm')}
                  </button>
                  <p className="text-xs text-gray-600 dark:text-gray-400">
                    {counted
                      ? t('run-report-confirm-help-again')
                      : t('run-report-confirm-help-first')}
                  </p>
                </div>
              )}
              <button
                type="button"
                className="text-sm font-medium underline"
                disabled={run.busy}
                onClick={startOver}
              >
                {t('run-report-choose-again')}
              </button>
            </>
          )}
        </section>
      )}

      {report && report.state === 'confirmed' && (
        <section
          className="space-y-3 rounded-lg border border-gray-200 p-3 dark:border-gray-700 sm:p-4"
          data-testid="report-counted"
        >
          <p className="text-sm font-semibold" role="status">
            <span aria-hidden="true">{'✓ '}</span>
            {t('run-report-counted', {
              filename: report.filename,
              count: report.counted_rows,
            })}
          </p>
          {report.replaces_rows > 0 && (
            <p className="text-sm">
              {t('run-report-replaces', { count: report.replaces_rows })}
            </p>
          )}
          {report.excluded.length > 0 && (
            <div className="space-y-1 text-sm" data-testid="report-excluded">
              <p className="font-semibold">{t('run-report-excluded-title')}</p>
              <ul role="list" className="space-y-1">
                {report.excluded.map((rows) => (
                  <li key={`${rows.ad_id}:${rows.reason}:${rows.first_date}`}>
                    {t(`run-report-excluded-${rows.reason}`, {
                      id: rows.ad_id,
                      count: rows.rows,
                      first: dayLabel(i18n.language, rows.first_date),
                      last: dayLabel(i18n.language, rows.last_date),
                      spend: moneyLabel(
                        i18n.language,
                        rows.spend,
                        report.currency
                      ),
                    })}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <button type="button" className={BUTTON} onClick={startOver}>
            {t('run-report-another')}
          </button>
        </section>
      )}

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
    </div>
  );
}
