import { useRef, useState } from 'react';
import { useTranslation } from 'next-i18next';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { CsrfTokenFetchError } from '@/lib/domain-client/csrf';
import {
  DomainProblemError,
  errorCodeMessage,
  errorMessage,
  errorMessageOf,
  type DomainProblem,
} from '@/lib/i18n/domain-errors';
import { formatNumber } from '@/lib/i18n/format';

export type Competitor = components['schemas']['Competitor'];
export type CompetitorCandidate = components['schemas']['CompetitorCandidate'];
export type CompetitorResolution =
  components['schemas']['CompetitorResolution'];
export type CompetitorResolveQuote =
  components['schemas']['CompetitorResolveQuote'];
export type CompetitorScan = components['schemas']['CompetitorScan'];
export type CompetitorScanSummary =
  components['schemas']['CompetitorScanSummary'];
export type CompetitorScanQuote = components['schemas']['CompetitorScanQuote'];
export type CompetitorScanRequest =
  components['schemas']['CompetitorScanRequest'];
export type CompetitorDiscovery = components['schemas']['CompetitorDiscovery'];
export type ExecutionMode = 'byok' | 'mock';
export type CompetitorAd = components['schemas']['CompetitorAd'];
export type CompetitorAdAssessment =
  components['schemas']['CompetitorAdAssessment'];
export type CompetitorAdTagAssessment =
  components['schemas']['CompetitorAdTagAssessment'];
export type CompetitorTagVocabulary =
  components['schemas']['CompetitorTagVocabulary'];
export type CompetitorSnapshotSummary =
  components['schemas']['CompetitorScanSnapshotSummary'];
export type CompetitorSnapshotAd =
  components['schemas']['CompetitorSnapshotAd'];
export type CompetitorReclusterQuote =
  components['schemas']['CompetitorScanReclusterQuote'];
export type Relevance = 'relevant' | 'adjacent' | 'unrelated';
/** Who made a judgment: only `ai` is AI inference (`rule` and `user` are not). */
export type JudgmentSource = 'ai' | 'rule' | 'user';

export const RELEVANCE_VALUES: Relevance[] = [
  'relevant',
  'adjacent',
  'unrelated',
];

/** Settings renders the provider keys here (`GenerationDefaultsBanner`). */
export const PROVIDER_CREDENTIALS_ANCHOR = 'provider-credentials';

/**
 * A confirmed page's name: the confirmation's own, else the advertiser name
 * the product's scans observed for that page (a link confirms a page without
 * its name, #332). `null` until a scan has seen the page.
 */
export function confirmedPageName(competitor: Competitor) {
  return (
    competitor.confirmation?.page_name || competitor.observed_page_name || null
  );
}

/**
 * A page's active ads in the Meta Ad Library for a market: the link the
 * backend gives discovered advertisers (`discovery.library_url`).
 */
export function pageLibraryUrl(pageId: string, country?: string | null) {
  const query = new URLSearchParams({
    active_status: 'active',
    ad_type: 'all',
    country: country || 'ALL',
    view_all_page_id: pageId,
  });
  return `https://www.facebook.com/ads/library/?${query}`;
}

/** Scans and resolves still working; the panel polls them. */
export const ACTIVE_STATUSES = ['queued', 'running'];

export const POLL_MS = 1500;

const SIGN_IN_REQUIRED = 'competitors-sign-in-required';
const RECONCILE = 'competitors-previous-request-needs-reconciliation';
const NO_RESULT = 'competitors-no-result';

/** The data of a gateway result, or the reason it has none, as an error. */
export function dataOf<T>(result: {
  kind: string;
  data?: T;
  problem?: DomainProblem;
  status?: number;
}): T {
  if (result.kind === 'problem' && result.status === 401)
    throw new Error(SIGN_IN_REQUIRED);
  if (result.kind === 'problem') throw new DomainProblemError(result.problem);
  if (result.kind === 'reconciliation_required') throw new Error(RECONCILE);
  if (result.data === undefined) throw new Error(NO_RESULT);
  return result.data;
}

/** A list in a code's params (`operations`) reads as "page, probe". */
function readableParams(params?: Record<string, unknown> | null) {
  if (!params) return params;
  return Object.fromEntries(
    Object.entries(params).map(([key, value]) => [
      key,
      Array.isArray(value) ? value.join(', ') : value,
    ])
  );
}

type Translate = ReturnType<typeof useTranslation>['t'];

/** The interface's sentence for a code a resolve, quote or scan carries. */
export function codeMessage(
  t: Translate,
  code: string | null | undefined,
  params?: Record<string, unknown> | null
) {
  return errorCodeMessage(t, code, readableParams(params));
}

export function problemMessage(t: Translate, error: unknown) {
  if (error instanceof CsrfTokenFetchError && error.status === 401)
    return t(SIGN_IN_REQUIRED);
  if (error instanceof DomainProblemError)
    return error.problem.code === 'validation_error'
      ? errorMessage(t, error.problem)
      : codeMessage(t, error.problem.code, error.problem.params);
  return errorMessageOf(t, error);
}

/**
 * Runs one command at a time. A retry of the same `identity` reuses its
 * action id, so the BFF derives the same `Idempotency-Key` and a request
 * whose answer was lost replays instead of reserving twice.
 */
export function useCommand() {
  const { t } = useTranslation('competitors');
  const pending = useRef<{ identity: string; action: string } | null>(null);
  const running = useRef(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{
    identity: string;
    message: string;
  } | null>(null);

  async function run<T>(
    identity: string,
    work: (action: string) => Promise<T>
  ): Promise<T | undefined> {
    if (running.current) return undefined;
    running.current = true;
    setBusy(identity);
    setError(null);
    if (pending.current?.identity !== identity)
      pending.current = { identity, action: crypto.randomUUID() };
    try {
      const result = await work(pending.current.action);
      pending.current = null;
      return result;
    } catch (e) {
      setError({ identity, message: problemMessage(t, e) });
      return undefined;
    } finally {
      running.current = false;
      setBusy(null);
    }
  }

  return {
    run,
    busy,
    /** The last failure, if it came from a command whose identity starts with `prefix`. */
    errorFor: (prefix: string) =>
      error && error.identity.startsWith(prefix) ? error.message : null,
    clearError: () => setError(null),
  };
}

/** `"0.016920"` -> `$0.0169`: credit prices are fractions of a cent. */
export function formatUsd(locale: string, value: string | null | undefined) {
  const amount = Number(value ?? 0);
  return formatNumber(locale, Number.isFinite(amount) ? amount : 0, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
}

export function formatCount(locale: string, value: number | null | undefined) {
  return formatNumber(locale, value ?? 0);
}

/**
 * A quote line's calls and credits as words: "3 calls", "6 credits". The
 * line's `credits` is its total (each call's price × calls), so the two read
 * side by side, never as a product (#346).
 */
export function quoteLineCounts(
  t: Translate,
  locale: string,
  line: { calls: number; credits: number }
) {
  return {
    calls: t('quote-unit-calls', {
      count: line.calls,
      formatted: formatCount(locale, line.calls),
    }),
    credits: t('quote-unit-credits', {
      count: line.credits,
      formatted: formatCount(locale, line.credits),
    }),
  };
}
