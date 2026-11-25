import { useEffect, useId, useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { AdThumbnail } from '@/components/ideas/AdCard';
import { PRESSED, BUTTON } from '@/components/ideas/PaidImageAction';
import { useIdeasProject } from '@/hooks/ideas/useIdeasProject';
import { useTestQuote } from '@/hooks/tests/useTestQuote';
import {
  problemParams,
  useProductTestInProgress,
  useTestStart,
} from '@/hooks/tests/useTestStart';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { errorMessage } from '@/lib/i18n/domain-errors';
import { ideasProblem } from '@/lib/ideas/ideas-problems';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';
import {
  productViewHref,
  testHref,
  type WorkspaceProduct,
} from '@/lib/products/workspace-products';
import {
  ATTRIBUTION_WINDOWS,
  DURATION_CHOICES,
  MAX_DAYS,
  MIN_DAYS,
  TEST_CURRENCIES,
  emptyForm,
  formProblems,
  quoteQuery,
  type FieldProblem,
  type TakeToTestForm,
  type TestQuote,
} from '@/lib/tests/take-to-test';
import { TestQuotePanel } from './TestQuotePanel';
import { TestStartProgress } from './TestStartProgress';

/** Refusals that mean the page showed something older than the server has. */
const CHANGED = new Set(['test_quote_changed', 'stale_marketing_revision']);

const SECTION =
  'space-y-3 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900 sm:p-5';
const INPUT = 'input input-bordered min-h-12 w-full text-base dark:bg-gray-900';

/**
 * "Take to test" (ADR 0019, design §5.11, #256): the kept ads of a product's
 * ideas become a test. Three questions (and the landing page when the
 * product has no link), the other defaults folded away, whether the money is
 * enough on the right, and one button "Start the test · at most $X". Once
 * started -- now, or earlier and the page opened again -- the screen follows
 * the checks to the ad package and its download.
 */
export function TakeToTest({ product }: { product: WorkspaceProduct }) {
  const { t } = useTranslation('tests');
  const { current } = useWorkspace();
  const projectId = product.ideas?.projectId;
  const inProgress = useProductTestInProgress(product.productId);
  const [testId, setTestId] = useState<string | null>(null);
  const [restarted, setRestarted] = useState<string | null>(null);
  const ideasHref = productViewHref(current.slug, product.productId, 'ideas');

  if (!projectId)
    return (
      <div className="space-y-3">
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {t('take-no-ideas')}
        </p>
      </div>
    );

  const running =
    testId ?? (inProgress.testId !== restarted ? inProgress.testId : null);
  if (!testId && inProgress.loading)
    return (
      <div className="flex items-center gap-3 py-8" role="status">
        <span className="loading loading-spinner" aria-hidden="true" />
        <span className="text-sm">{t('take-loading')}</span>
      </div>
    );
  if (running)
    return (
      <TestStartProgress
        key={running}
        testId={running}
        ideasHref={ideasHref}
        runHref={testHref(current.slug, product.productId, running)}
        onRestart={() => {
          setRestarted(running);
          setTestId(null);
          void inProgress.refresh();
        }}
      />
    );
  return (
    <TakeToTestForm
      projectId={projectId}
      ideasHref={ideasHref}
      onStarted={(id) => {
        setTestId(id);
        void inProgress.refresh();
      }}
    />
  );
}

function TakeToTestForm({
  projectId,
  ideasHref,
  onStarted,
}: {
  projectId: string;
  ideasHref: string;
  onStarted: (testId: string) => void;
}) {
  const { t } = useTranslation('tests');
  const ideas = useIdeasProject(projectId);
  const { detail } = ideas;
  const [form, setForm] = useState<TakeToTestForm>(() => emptyForm());
  const [touched, setTouched] = useState<Set<keyof TakeToTestForm>>(
    () => new Set()
  );
  const [defaultsOpen, setDefaultsOpen] = useState(false);
  const [starting, setStarting] = useState(false);
  const [startProblem, setStartProblem] = useState<
    ProblemDetails | null | undefined
  >(undefined);
  // Learned from the quotes: whether the product has a link of its own.
  const [landingAsked, setLandingAsked] = useState(false);
  const [productLinked, setProductLinked] = useState(false);

  const kept = useMemo(
    () => (detail?.ads ?? []).filter((item) => item.kept),
    [detail]
  );
  const keptIds = useMemo(
    () => kept.map((item) => item.asset_version_id),
    [kept]
  );
  const query = useMemo(() => quoteQuery(form), [form]);
  const quote = useTestQuote(
    projectId,
    query,
    `${detail?.revision ?? '-'}:${keptIds.join(',')}`
  );
  const { start } = useTestStart(projectId);
  const data = quote.kind === 'ok' ? quote.data : null;

  useEffect(() => {
    if (!data) return;
    if (data.unavailable_reason === 'landing_page_required')
      setLandingAsked(true);
    if (data.landing_page_source === 'product_link') setProductLinked(true);
  }, [data]);

  const landingQuestion = landingAsked && !productLinked;
  const problems = formProblems(form, landingQuestion);
  const ready = Object.keys(problems).length === 0 && query !== null;
  const checkHref = `${ideasHref}?check=1`;

  if (ideas.isLoading)
    return (
      <div className="flex items-center gap-3 py-8" role="status">
        <span className="loading loading-spinner" aria-hidden="true" />
        <span className="text-sm">{t('take-loading')}</span>
      </div>
    );
  if (ideas.failed || !detail)
    return (
      <div className="space-y-3">
        <p className="text-sm" role="alert">
          {t('take-problem')}
        </p>
        <button
          type="button"
          className={BUTTON}
          onClick={() => void ideas.refresh()}
        >
          {t('take-retry')}
        </button>
      </div>
    );
  if (kept.length === 0)
    return (
      <div className="space-y-3" data-testid="take-no-kept">
        <p className="text-sm">{t('take-no-kept')}</p>
        <GuardedLink href={ideasHref} className={BUTTON}>
          {t('take-back-to-ideas')}
        </GuardedLink>
      </div>
    );

  const set = <K extends keyof TakeToTestForm>(
    field: K,
    value: TakeToTestForm[K]
  ) => {
    setForm((previous) => ({ ...previous, [field]: value }));
    setStartProblem(undefined);
  };
  const touch = (field: keyof TakeToTestForm) =>
    setTouched((previous) => new Set(previous).add(field));
  const shown = (field: keyof TakeToTestForm): FieldProblem | undefined =>
    touched.has(field) ? problems[field] : undefined;

  async function onStart() {
    if (
      quote.kind !== 'ok' ||
      !quote.current ||
      !quote.data.available ||
      !query
    )
      return;
    setStarting(true);
    setStartProblem(undefined);
    try {
      const outcome = await start({
        quote: quote.data,
        query,
        assetVersionIds: keptIds,
      });
      if (outcome.kind === 'done') onStarted(outcome.data.id);
      else if (outcome.kind === 'uncertain') setStartProblem(null);
      else {
        const running = problemParams<{ test_id?: string }>(outcome.problem);
        if (outcome.problem.code === 'test_in_progress' && running?.test_id)
          onStarted(running.test_id);
        else setStartProblem(outcome.problem);
      }
    } finally {
      setStarting(false);
    }
  }

  async function refreshAll() {
    setStartProblem(undefined);
    await ideas.refresh();
    await quote.refresh();
  }

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h2 className="text-xl font-bold sm:text-2xl">
          {t('take-title', { count: kept.length })}
        </h2>
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {t('take-intro')}
        </p>
      </div>
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="min-w-0 space-y-4">
          <RoundQuestion quote={data} detail={detail} keptIds={keptIds} />
          <Question
            label={t('take-target-label')}
            help={t('take-target-help')}
            problem={shown('targetCost')}
          >
            {(id, described) => (
              <MoneyInput
                id={id}
                described={described}
                currency={data?.currency ?? (form.currency || 'USD')}
                value={form.targetCost}
                invalid={Boolean(shown('targetCost'))}
                onChange={(value) => set('targetCost', value)}
                onBlur={() => touch('targetCost')}
              />
            )}
          </Question>
          <Question
            label={t('take-budget-label')}
            help={t('take-budget-help')}
            problem={shown('roundBudget')}
          >
            {(id, described) => (
              <MoneyInput
                id={id}
                described={described}
                currency={data?.currency ?? (form.currency || 'USD')}
                value={form.roundBudget}
                invalid={Boolean(shown('roundBudget'))}
                onChange={(value) => set('roundBudget', value)}
                onBlur={() => touch('roundBudget')}
              />
            )}
          </Question>
          <DaysQuestion
            value={form.durationDays}
            problem={shown('durationDays')}
            onChange={(value) => {
              set('durationDays', value);
              touch('durationDays');
            }}
          />
          {landingQuestion && (
            <Question
              label={t('take-landing-label')}
              help={t('take-landing-help')}
              problem={problems.landingPage}
            >
              {(id, described) => (
                <input
                  id={id}
                  type="url"
                  inputMode="url"
                  className={INPUT}
                  aria-describedby={described}
                  aria-invalid={Boolean(problems.landingPage) || undefined}
                  placeholder="https://"
                  value={form.landingPage}
                  onChange={(event) => set('landingPage', event.target.value)}
                />
              )}
            </Question>
          )}
          <Defaults
            open={defaultsOpen}
            onOpenChange={setDefaultsOpen}
            form={form}
            problems={problems}
            quote={data}
            detail={detail}
            landingInside={!landingQuestion}
            set={set}
          />
        </div>
        <TestQuotePanel
          quote={quote}
          ready={ready}
          starting={starting}
          onStart={() => void onStart()}
          checkHref={checkHref}
          ideasHref={ideasHref}
          notice={
            startProblem !== undefined && (
              <StartNotice
                problem={startProblem}
                checkHref={checkHref}
                onRefresh={() => void refreshAll()}
              />
            )
          }
        />
      </div>
    </div>
  );
}

/** What the start command said: a change to look at, a refusal, or no answer. */
function StartNotice({
  problem,
  checkHref,
  onRefresh,
}: {
  problem: ProblemDetails | null;
  checkHref: string;
  onRefresh: () => void;
}) {
  const { t } = useTranslation('tests');
  if (problem === null)
    return (
      <p className="text-sm text-gray-700 dark:text-gray-300" role="status">
        {t('take-start-uncertain')}
      </p>
    );
  if (CHANGED.has(problem.code))
    return (
      <div
        className="space-y-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
        role="alert"
        data-testid="take-changed"
      >
        <p>{t('take-changed')}</p>
        <button
          type="button"
          className="btn btn-sm min-h-11 bg-white"
          onClick={onRefresh}
        >
          {t('take-refresh')}
        </button>
      </div>
    );
  return (
    <div className="space-y-2" role="alert">
      <p className="text-sm text-red-700 dark:text-red-300">
        {problem.code === 'ads_not_exportable' ||
        problem.code === 'checks_unavailable'
          ? ideasProblem(t, problem)
          : errorMessage(t, problem)}
      </p>
      {problem.code === 'ads_not_exportable' && (
        <GuardedLink href={checkHref} className="btn btn-sm min-h-11">
          {t('take-go-check')}
        </GuardedLink>
      )}
    </div>
  );
}

/** "What this round finds out": the quote's sentence and what it compares; the kept ads until then. */
function RoundQuestion({
  quote,
  detail,
  keptIds,
}: {
  quote: TestQuote | null;
  detail: IdeasDetail;
  keptIds: string[];
}) {
  const { t } = useTranslation('tests');
  const headline = (id: string) =>
    detail.ideas?.combinations.find(
      (c) =>
        c.id ===
        detail.ads.find((a) => a.asset_version_id === id)?.combination_id
    )?.headline ?? '';
  if (!quote?.question || quote.subjects.length === 0)
    return (
      <section className={SECTION} aria-labelledby="take-kept-label">
        <h3
          id="take-kept-label"
          className="text-sm font-semibold text-gray-600 dark:text-gray-400"
        >
          {t('take-kept-label')}
        </h3>
        <ul role="list" className="flex flex-wrap gap-2">
          {keptIds.map((id) => (
            <li key={id}>
              <AdThumbnail versionId={id} alt={headline(id)} />
            </li>
          ))}
        </ul>
      </section>
    );
  return (
    <section
      className={SECTION}
      aria-labelledby="take-question-label"
      data-testid="test-question"
    >
      <h3
        id="take-question-label"
        className="text-sm font-semibold text-gray-600 dark:text-gray-400"
      >
        {t('take-question-label')}
      </h3>
      <p className="text-lg font-bold leading-snug">{quote.question}</p>
      <ul role="list" className="grid gap-3 sm:grid-cols-2">
        {quote.subjects.map((subject) => (
          <li
            key={`${subject.angle_id}:${subject.expression_id ?? ''}:${subject.asset_version_ids.join(',')}`}
            className="space-y-2 rounded-lg border border-gray-200 p-3 dark:border-gray-700"
          >
            <p className="text-xs font-semibold text-gray-600 dark:text-gray-400">
              {t(`take-subject-${subject.kind}`)}
            </p>
            <p className="text-sm font-semibold">{subject.title}</p>
            <ul role="list" className="flex flex-wrap gap-2">
              {subject.asset_version_ids.map((id) => (
                <li key={id}>
                  <AdThumbnail versionId={id} alt={headline(id)} size="xs" />
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Question({
  label,
  help,
  problem,
  children,
}: {
  label: string;
  help?: string;
  problem?: FieldProblem;
  children: (id: string, described: string) => ReactNode;
}) {
  const { t } = useTranslation('tests');
  const id = useId();
  return (
    <section className={SECTION}>
      <label htmlFor={`${id}-input`} className="block text-base font-semibold">
        {label}
      </label>
      {children(`${id}-input`, `${id}-help ${id}-problem`)}
      {help && (
        <p
          id={`${id}-help`}
          className="text-sm text-gray-600 dark:text-gray-400"
        >
          {help}
        </p>
      )}
      {problem && (
        <p
          id={`${id}-problem`}
          className="text-sm text-red-700 dark:text-red-300"
          role="alert"
        >
          {t(`take-problem-${problem}`)}
        </p>
      )}
    </section>
  );
}

function MoneyInput({
  id,
  described,
  currency,
  value,
  invalid,
  onChange,
  onBlur,
}: {
  id: string;
  described: string;
  currency: string;
  value: string;
  invalid: boolean;
  onChange: (value: string) => void;
  onBlur: () => void;
}) {
  return (
    <div className="flex max-w-xs items-center gap-2">
      <input
        id={id}
        type="text"
        inputMode="decimal"
        autoComplete="off"
        className={INPUT}
        aria-describedby={described}
        aria-invalid={invalid || undefined}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onBlur={onBlur}
      />
      <span className="text-sm font-semibold text-gray-600 dark:text-gray-400">
        {currency}
      </span>
    </div>
  );
}

function DaysQuestion({
  value,
  problem,
  onChange,
}: {
  value: string;
  problem?: FieldProblem;
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation('tests');
  const id = useId();
  const preset = DURATION_CHOICES.map(String).includes(value);
  const [other, setOther] = useState(!preset);
  return (
    <section className={SECTION} aria-labelledby={`${id}-label`}>
      <h3 id={`${id}-label`} className="text-base font-semibold">
        {t('take-days-label')}
      </h3>
      <div
        className="flex flex-wrap gap-2"
        role="group"
        aria-labelledby={`${id}-label`}
      >
        {DURATION_CHOICES.map((days) => {
          const on = !other && value === String(days);
          return (
            <button
              key={days}
              type="button"
              aria-pressed={on}
              className={classNames(
                on ? PRESSED : BUTTON,
                'min-h-12 px-5 text-base'
              )}
              onClick={() => {
                setOther(false);
                onChange(String(days));
              }}
            >
              {t('take-days-option', { count: days })}
            </button>
          );
        })}
        <button
          type="button"
          aria-pressed={other}
          className={classNames(
            other ? PRESSED : BUTTON,
            'min-h-12 px-5 text-base'
          )}
          onClick={() => setOther(true)}
        >
          {t('take-days-other')}
        </button>
      </div>
      {other && (
        <label className="block max-w-[10rem] space-y-1 text-sm">
          <span>{t('take-days-other-label')}</span>
          <input
            type="number"
            inputMode="numeric"
            min={MIN_DAYS}
            max={MAX_DAYS}
            className={INPUT}
            aria-invalid={Boolean(problem) || undefined}
            value={value}
            onChange={(event) => onChange(event.target.value)}
          />
        </label>
      )}
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {t('take-days-help')}
      </p>
      {problem && (
        <p className="text-sm text-red-700 dark:text-red-300" role="alert">
          {t(`take-problem-${problem}`)}
        </p>
      )}
    </section>
  );
}

/**
 * "Everything else follows the defaults" (design §5.2): folded, each one
 * changeable -- audience, landing page, offer, placements, what counts as an
 * order, the ad account's time zone, the start day, the currency and the
 * whole test's limits. Left empty, the server's default stands; what it is
 * shows beside the field, as the quote or the ideas say it.
 */
function Defaults({
  open,
  onOpenChange,
  form,
  problems,
  quote,
  detail,
  landingInside,
  set,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  form: TakeToTestForm;
  problems: ReturnType<typeof formProblems>;
  quote: TestQuote | null;
  detail: IdeasDetail;
  landingInside: boolean;
  set: <K extends keyof TakeToTestForm>(
    field: K,
    value: TakeToTestForm[K]
  ) => void;
}) {
  const { t } = useTranslation('tests');
  const audience = detail.understanding?.audience;
  const row = (
    label: string,
    field: keyof TakeToTestForm,
    control: (id: string) => ReactNode,
    help?: string
  ) => {
    const id = `take-default-${field}`;
    const problem = problems[field];
    return (
      <div className="grid gap-1 sm:grid-cols-[10rem_minmax(0,1fr)] sm:gap-4">
        <label
          htmlFor={id}
          className="pt-2 text-sm font-semibold sm:font-normal sm:text-gray-600 dark:sm:text-gray-400"
        >
          {label}
        </label>
        <div className="min-w-0 space-y-1">
          {control(id)}
          {help && (
            <p className="text-xs text-gray-600 dark:text-gray-400">{help}</p>
          )}
          {problem && (
            <p className="text-xs text-red-700 dark:text-red-300" role="alert">
              {t(`take-problem-${problem}`)}
            </p>
          )}
        </div>
      </div>
    );
  };
  const text = (
    field: 'audience' | 'offer' | 'placement' | 'landingPage',
    placeholder: string,
    multiline = false
  ) =>
    function control(id: string) {
      return multiline ? (
        <textarea
          id={id}
          className="textarea textarea-bordered min-h-20 w-full text-base"
          placeholder={placeholder}
          value={form[field]}
          onChange={(event) => set(field, event.target.value)}
        />
      ) : (
        <input
          id={id}
          type={field === 'landingPage' ? 'url' : 'text'}
          className={INPUT}
          placeholder={placeholder}
          value={form[field]}
          onChange={(event) => set(field, event.target.value)}
        />
      );
    };
  return (
    <details
      className="rounded-xl border border-gray-200 bg-white px-4 dark:border-gray-700 dark:bg-gray-900 sm:px-5"
      open={open}
      onToggle={(event) =>
        onOpenChange((event.target as HTMLDetailsElement).open)
      }
      data-testid="take-defaults"
    >
      <summary className="flex min-h-12 cursor-pointer items-center text-sm font-semibold">
        {t('take-defaults-summary')}
      </summary>
      <div className="space-y-4 pb-5 pt-1">
        {landingInside &&
          row(
            t('take-default-landing'),
            'landingPage',
            text(
              'landingPage',
              quote?.landing_page_source === 'product_link' &&
                quote.landing_page
                ? quote.landing_page
                : t('take-default-landing-placeholder')
            )
          )}
        {row(
          t('take-default-audience'),
          'audience',
          text('audience', audience ?? '', true),
          audience
            ? t('take-default-audience-help', { audience })
            : t('take-default-audience-help-none')
        )}
        {row(
          t('take-default-offer'),
          'offer',
          text('offer', t('take-default-offer-placeholder'))
        )}
        {row(
          t('take-default-placement'),
          'placement',
          text('placement', t('take-default-placement-placeholder'))
        )}
        {row(t('take-default-attribution'), 'attributionWindow', (id) => (
          <select
            id={id}
            className="select select-bordered min-h-12 w-full text-base"
            value={form.attributionWindow}
            onChange={(event) =>
              set(
                'attributionWindow',
                event.target.value as TakeToTestForm['attributionWindow']
              )
            }
          >
            <option value="">
              {t('take-default-choice', {
                value: t(`take-attribution-${ATTRIBUTION_WINDOWS[0]}`),
              })}
            </option>
            {ATTRIBUTION_WINDOWS.slice(1).map((window) => (
              <option key={window} value={window}>
                {t(`take-attribution-${window}`)}
              </option>
            ))}
          </select>
        ))}
        {row(
          t('take-default-timezone'),
          'accountTimezone',
          (id) => (
            <input
              id={id}
              type="text"
              className={INPUT}
              value={form.accountTimezone}
              placeholder="UTC"
              onChange={(event) => set('accountTimezone', event.target.value)}
            />
          ),
          t('take-default-timezone-help')
        )}
        {row(
          t('take-default-start'),
          'startDate',
          (id) => (
            <input
              id={id}
              type="date"
              className={INPUT}
              value={form.startDate}
              onChange={(event) => set('startDate', event.target.value)}
            />
          ),
          t('take-default-start-help')
        )}
        {row(
          t('take-default-currency'),
          'currency',
          (id) => (
            <select
              id={id}
              className="select select-bordered min-h-12 w-full text-base"
              value={form.currency}
              onChange={(event) =>
                set(
                  'currency',
                  event.target.value as TakeToTestForm['currency']
                )
              }
            >
              <option value="">
                {t('take-default-choice', { value: 'USD' })}
              </option>
              {TEST_CURRENCIES.filter((code) => code !== 'USD').map((code) => (
                <option key={code} value={code}>
                  {code}
                </option>
              ))}
            </select>
          ),
          t('take-default-currency-help')
        )}
        {row(t('take-default-rounds'), 'maxSmallRounds', (id) => (
          <input
            id={id}
            type="number"
            inputMode="numeric"
            min={1}
            max={20}
            className={INPUT}
            placeholder="3"
            value={form.maxSmallRounds}
            onChange={(event) => set('maxSmallRounds', event.target.value)}
          />
        ))}
        {row(t('take-default-total'), 'mediaBudget', (id) => (
          <input
            id={id}
            type="text"
            inputMode="decimal"
            className={INPUT}
            placeholder={t('take-default-total-placeholder')}
            value={form.mediaBudget}
            onChange={(event) => set('mediaBudget', event.target.value)}
          />
        ))}
        <dl className="grid gap-1 text-sm sm:grid-cols-[10rem_minmax(0,1fr)] sm:gap-x-4 sm:gap-y-2">
          <dt className="text-gray-600 dark:text-gray-400">
            {t('take-default-split')}
          </dt>
          <dd>{t('take-default-split-value')}</dd>
          {quote && (
            <>
              <dt className="text-gray-600 dark:text-gray-400">
                {t('take-default-threshold')}
              </dt>
              <dd>
                {t('take-default-threshold-value', {
                  count: quote.min_purchases,
                })}
              </dd>
            </>
          )}
        </dl>
      </div>
    </details>
  );
}
