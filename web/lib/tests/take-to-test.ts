import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { DomainOperationMap } from '@/lib/domain-client/generated/operation-map';
import { formatDate, formatNumber } from '@/lib/i18n/format';

/**
 * What the "take to test" screen (ADR 0019, design §5.2, §5.3, §5.6, #256)
 * turns its answers into, and how it reads a started test's round. Every
 * number the screen shows about the test -- what each compared object gets,
 * the orders it may bring, whether that is enough, what a verdict needs, the
 * day of the result, what starting costs at most -- is the quote's; nothing
 * here computes one.
 */

export type TestQuote = components['schemas']['MarketingTestQuote'];
/** The answers, as the quote reads them and the start command sends them. */
export type TestAnswers = components['schemas']['MarketingTestAnswers'];
/** The same answers as the quote's query string. */
export type TestQuoteQuery =
  DomainOperationMap['get_marketing_test_quote']['queryParams'];
export type TestDetail = components['schemas']['MarketingTestDetail'];
export type TestPackage = components['schemas']['MarketingTestPackage'];
export type TestRound = components['schemas']['MarketingRound'];
export type TestRoundAsset = components['schemas']['WorkflowAsset'];
export type TestCurrency = NonNullable<TestAnswers['currency']>;
export type AttributionWindow = NonNullable<TestAnswers['attribution_window']>;
type Finding = components['schemas']['EffectiveFinding'];

export const TEST_CURRENCIES: readonly TestCurrency[] = [
  'USD',
  'EUR',
  'GBP',
  'CAD',
  'AUD',
  'NZD',
  'CHF',
  'SEK',
  'PLN',
  'JPY',
  'CNY',
  'HKD',
  'TWD',
  'KRW',
  'SGD',
  'MYR',
  'THB',
  'IDR',
  'PHP',
  'VND',
  'INR',
  'AED',
  'SAR',
  'MXN',
  'BRL',
  'ZAR',
];

/** The default first (design §5.2: 7-day click plus 1-day view). */
export const ATTRIBUTION_WINDOWS: readonly AttributionWindow[] = [
  '7d_click_1d_view',
  '7d_click',
  '1d_click_1d_view',
  '1d_click',
  '1d_view',
];

/** How long the round runs: the two usual choices; any 7 to 90 days is allowed. */
export const DURATION_CHOICES = [7, 14] as const;
export const MIN_DAYS = 7;
export const MAX_DAYS = 90;

/** What the user typed; every default left empty keeps the server's default. */
export interface TakeToTestForm {
  targetCost: string;
  roundBudget: string;
  durationDays: string;
  landingPage: string;
  audience: string;
  offer: string;
  placement: string;
  currency: TestCurrency | '';
  /** The ad account's time zone; the browser's to begin with (design §5.2). */
  accountTimezone: string;
  startDate: string;
  attributionWindow: AttributionWindow | '';
  maxSmallRounds: string;
  mediaBudget: string;
}

/** The browser's time zone, the ad account's default (design §5.2). */
export function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch {
    return '';
  }
}

export function emptyForm(timezone = browserTimezone()): TakeToTestForm {
  return {
    targetCost: '',
    roundBudget: '',
    durationDays: String(MIN_DAYS),
    landingPage: '',
    audience: '',
    offer: '',
    placement: '',
    currency: '',
    accountTimezone: timezone,
    startDate: '',
    attributionWindow: '',
    maxSmallRounds: '',
    mediaBudget: '',
  };
}

export type FieldProblem =
  | 'required'
  | 'money'
  | 'days'
  | 'url'
  | 'rounds'
  | 'too_long';

export type FormProblems = Partial<Record<keyof TakeToTestForm, FieldProblem>>;

const MONEY = /^\d{1,12}(\.\d{1,2})?$/;
const URL_PATTERN = /^https?:\/\/\S+$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const TEXT_LIMIT = 2000;

/** `1,200` and ` 30 ` read as the amounts they mean. */
const amount = (value: string) => value.replace(/[,\s]/g, '');

function money(value: string): string | null {
  const plain = amount(value);
  return MONEY.test(plain) && Number(plain) > 0 ? plain : null;
}

function whole(value: string, min: number, max: number): number | null {
  const plain = value.trim();
  if (!/^\d{1,3}$/.test(plain)) return null;
  const number = Number(plain);
  return number >= min && number <= max ? number : null;
}

/**
 * What is wrong with each answer, by field. The three questions are
 * required; a default may be left empty, and is checked only once typed.
 * `landingRequired`: the product has no link, so the ads need an address.
 */
export function formProblems(
  form: TakeToTestForm,
  landingRequired: boolean
): FormProblems {
  const problems: FormProblems = {};
  if (!form.targetCost.trim()) problems.targetCost = 'required';
  else if (!money(form.targetCost)) problems.targetCost = 'money';
  if (!form.roundBudget.trim()) problems.roundBudget = 'required';
  else if (!money(form.roundBudget)) problems.roundBudget = 'money';
  if (whole(form.durationDays, MIN_DAYS, MAX_DAYS) === null)
    problems.durationDays = 'days';
  const landing = form.landingPage.trim();
  if (!landing && landingRequired) problems.landingPage = 'required';
  else if (landing && (!URL_PATTERN.test(landing) || landing.length > 2000))
    problems.landingPage = 'url';
  for (const field of ['audience', 'offer', 'placement'] as const)
    if (form[field].trim().length > TEXT_LIMIT) problems[field] = 'too_long';
  if (form.maxSmallRounds.trim() && whole(form.maxSmallRounds, 1, 20) === null)
    problems.maxSmallRounds = 'rounds';
  if (form.mediaBudget.trim() && !money(form.mediaBudget))
    problems.mediaBudget = 'money';
  if (form.startDate && !DAY.test(form.startDate))
    problems.startDate = 'required';
  return problems;
}

/**
 * The quote's query for these answers (`MarketingTestAnswers`), or `null`
 * while any of them is not a value the server takes. The start command sends
 * the same answers, so the quote and the start agree on what was asked. A
 * missing landing page is the quote's to report (`landing_page_required`):
 * only the server knows whether the product has a link.
 */
export function quoteQuery(form: TakeToTestForm): TestAnswers | null {
  const problems = formProblems(form, false);
  if (Object.keys(problems).length > 0) return null;
  const query: TestAnswers = {
    target_cost: money(form.targetCost)!,
    round_budget: money(form.roundBudget)!,
    duration_days: whole(form.durationDays, MIN_DAYS, MAX_DAYS)!,
  };
  const landing = form.landingPage.trim();
  if (landing) query.landing_page = landing;
  const text = (value: string) => value.trim() || undefined;
  if (text(form.audience)) query.audience = text(form.audience);
  if (text(form.offer)) query.offer = text(form.offer);
  if (text(form.placement)) query.placement = text(form.placement);
  if (form.currency) query.currency = form.currency;
  if (text(form.accountTimezone))
    query.account_timezone = text(form.accountTimezone);
  if (form.startDate) query.start_date = form.startDate;
  if (form.attributionWindow) query.attribution_window = form.attributionWindow;
  if (form.maxSmallRounds.trim())
    query.max_small_rounds = Number(form.maxSmallRounds.trim());
  if (form.mediaBudget.trim()) query.media_budget = money(form.mediaBudget)!;
  return query;
}

/** The answers as the quote's query string: every given value, as text. */
export function queryOf(answers: TestAnswers): TestQuoteQuery {
  return Object.fromEntries(
    Object.entries(answers)
      .filter(([, value]) => value !== undefined && value !== null)
      .map(([key, value]) => [key, String(value)])
  ) as TestQuoteQuery;
}

/** One amount in the test's currency, as the quote wrote it. */
export function moneyLabel(
  locale: string,
  value: string | number,
  currency: string
): string {
  const number = Number(value);
  try {
    return formatNumber(locale, number, {
      style: 'currency',
      currency,
      maximumFractionDigits: Number.isInteger(number) ? 0 : 2,
      minimumFractionDigits: Number.isInteger(number) ? 0 : 2,
    });
  } catch {
    return `${value} ${currency}`;
  }
}

/** A calendar day (`2026-12-02`) as the reader writes dates; never shifted by the browser's zone. */
export function dayLabel(locale: string, day: string): string {
  return formatDate(locale, `${day}T00:00:00Z`, {
    timeZone: 'UTC',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  });
}

/** A paired check is still on its way: sent, being made, or uncertain and being reconciled. */
const WAITING = new Set(['queued', 'running', 'uncertain']);

export interface CheckProgress {
  /** Every kept ad of the round. */
  total: number;
  /** Ads whose "look together" check has its result (passed or not). */
  answered: number;
  /** Every check has its result: the package can be asked for. */
  allAnswered: boolean;
}

/** How far the round's "look together" checks are (design §5.3). */
export function checkProgress(round: TestRound): CheckProgress {
  const total = round.assets.length;
  const answered = round.assets.filter(
    (asset) => asset.paired_ai_qc && !WAITING.has(asset.paired_ai_qc.state)
  ).length;
  return { total, answered, allAnswered: total > 0 && answered === total };
}

/** Whether the page should keep asking for the round: something on its way. */
export function roundBusy(round: TestRound | undefined): boolean {
  if (!round || round.state === 'closed') return false;
  if (round.handoff)
    return (
      round.handoff.state === 'queued' || round.handoff.state === 'running'
    );
  return !checkProgress(round).allAnswered;
}

/** One ad's findings the user has to look at, as `check_findings_open` lists them. */
export interface OpenAd {
  assetVersionId: string;
  checkId: string;
  headline: string;
  findings: Finding[];
}

type OpenParam = {
  asset_version_id: string;
  check_id: string;
  finding_ids: string[];
};

/**
 * The ads `check_findings_open` names, each with the findings of its newest
 * "look together" check that did not pass (design §5.3: only the ads with a
 * problem open). The finding's words come from the round the page reads.
 */
export function openAds(
  params: { findings?: OpenParam[] } | null | undefined,
  round: TestRound | undefined
): OpenAd[] {
  return (params?.findings ?? []).map((item) => {
    const asset = round?.assets.find(
      (a) => a.asset_version_id === item.asset_version_id
    );
    const findings = (asset?.paired_ai_qc?.result?.findings ?? []).filter(
      (finding) => item.finding_ids.includes(finding.check_id)
    );
    return {
      assetVersionId: item.asset_version_id,
      checkId: item.check_id,
      headline: asset?.headline ?? '',
      findings: findings.length
        ? findings
        : item.finding_ids.map(
            (id) =>
              ({
                check_id: id,
                status: 'unknown',
                confidence: 'low',
                visual_evidence: '',
                rationale: '',
                suggested_fix: '',
                fact_ids: [],
                model_status: 'unknown',
              }) as Finding
          ),
    };
  });
}

/** The ads `check_evidence_missing` names, with why (`check_missing`, `check_failed`, ...). */
export interface UnprovenAd {
  assetVersionId: string;
  headline: string;
  reason: string;
}

export function unprovenAds(
  params:
    | {
        ads?: {
          asset_version_id: string;
          reason: string;
        }[];
      }
    | null
    | undefined,
  round: TestRound | undefined
): UnprovenAd[] {
  return (params?.ads ?? []).map((item) => ({
    assetVersionId: item.asset_version_id,
    headline:
      round?.assets.find((a) => a.asset_version_id === item.asset_version_id)
        ?.headline ?? '',
    reason: item.reason,
  }));
}
