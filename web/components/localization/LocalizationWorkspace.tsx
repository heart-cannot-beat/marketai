import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/router';
import { Trans, useTranslation } from 'next-i18next';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useOperation } from '@/hooks/domain/useOperation';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { errorCodeMessage } from '@/lib/i18n/domain-errors';
import { formatDateTime } from '@/lib/i18n/format';
import { newActionId } from '@/components/generation/shared/newActionId';
import { OperationStatus } from '@/components/workspace/OperationStatus';
import { LocalizationPreparation } from './LocalizationPreparation';
import {
  actionReasonMessage,
  codeWords,
  LocalizationProblem as ProblemBanner,
  LocalizationSection as Section,
} from './LocalizationUi';

type Workspace = components['schemas']['LocalizationWorkspace'];
type BatchPage = components['schemas']['LocalizationBatchPage'];
type BatchSummary = components['schemas']['LocalizationBatchSummary'];
type BatchDetail = components['schemas']['LocalizationBatchDetail'];
type ItemDetail = components['schemas']['LocalizationItemDetail'];
type Source = components['schemas']['EligibleLocalizationSource'];

type UnknownRecord = Record<string, unknown>;

type TFunction = ReturnType<typeof useTranslation>['t'];

interface BatchSourceRow extends UnknownRecord {
  asset_version_id: string;
  decision_id: string;
  experiment_id: string;
  experiment_version_id: string;
  source_generation_mode: string;
  experiment_evidence_mode: string;
}

interface TruthFactRow extends UnknownRecord {
  id: string;
  fact_type: string;
  scope_key: string;
  value: unknown;
  external_object_ref?: string | null;
  valid_until?: string | null;
}

interface TruthAttemptRow extends UnknownRecord {
  id: string;
  fact_type: string;
  adapter_kind: string;
  outcome: string;
  failure_code?: string | null;
  skip_code?: string | null;
}

/**
 * The outcomes a TruthSourceAttempt can persist are `accepted`, `failed` and
 * `skipped` (`app/localization/truth.py`). Only the last two are something
 * went wrong; `accepted` is the fact actually landing.
 *
 * This used to be written as `outcome !== 'resolved'` — a value the backend
 * never emits — so every accepted attempt was reported under "Source
 * failures / skips", and a fully ready market looked like it had failed on
 * every single fact it successfully resolved.
 */
const TRUTH_ATTEMPT_FAILURE_OUTCOMES = new Set(['failed', 'skipped']);

export function isFailedTruthAttempt(attempt: { outcome: string }): boolean {
  return TRUTH_ATTEMPT_FAILURE_OUTCOMES.has(attempt.outcome);
}

interface TruthMarketRow extends UnknownRecord {
  market: string;
  locale?: string;
  currency?: string;
  readiness: string;
  readiness_detail?: unknown;
  facts?: TruthFactRow[];
  source_attempts?: TruthAttemptRow[];
}

interface BatchItemRow extends UnknownRecord {
  id: string;
  state: string;
  target_market: string;
  source_asset_version_id: string;
  attempt_count: number;
  reason_code?: string | null;
  recoverable?: boolean | null;
}

interface CopyRow extends UnknownRecord {
  id: string;
  source_asset_version_id: string;
  target_market: string;
  state: string;
  copy_origin: string;
  content?: UnknownRecord;
  evidence_hash?: string;
}

interface QcRow extends UnknownRecord {
  id: string;
  check_kind: string;
  check_slot_key?: string | null;
  severity: string;
  result: string;
  expected?: unknown;
  observed?: unknown;
  rule_version?: string;
}

interface ProviderCallRow extends UnknownRecord {
  step: string;
  dispatch_state: string;
  chargeability: string;
  provider: string;
  model?: string | null;
  error_code?: string | null;
  maximum_cost?: { amount: string; currency: string; state: string } | null;
}

interface AttemptRow extends UnknownRecord {
  id: string;
  attempt_number: number;
  state: string;
  provider: string;
  model: string;
  created_at?: string;
  output?: UnknownRecord | null;
  publication?: UnknownRecord | null;
  qc?: QcRow[];
  provider_calls?: ProviderCallRow[];
}

interface CostRow extends UnknownRecord {
  entry_type: string;
  direction: string;
  amount: string;
  currency: string;
  provider: string;
  model?: string | null;
}

interface ItemCost extends UnknownRecord {
  totals?: Array<{ amount: string; currency: string }>;
  ledger?: CostRow[];
}

const badgeTone: Record<string, string> = {
  complete: 'badge-success',
  completed: 'badge-success',
  ready: 'badge-success',
  pass: 'badge-success',
  passed: 'badge-success',
  full: 'badge-success',
  running: 'badge-info',
  preparing: 'badge-info',
  retry_wait: 'badge-warning',
  paused: 'badge-warning',
  degraded: 'badge-warning',
  failed: 'badge-error',
  cancelled: 'badge-ghost',
};

/** A persisted server state worded as `status.<code>`; others show as words. */
function statusLabel(t: TFunction, value: string | null | undefined): string {
  const status = value || 'unknown';
  return t(`status.${status}`, { defaultValue: codeWords(status) });
}

/** A persisted server state; codes without copy show as words. */
function StatusBadge({ value }: { value: string | null | undefined }) {
  const { t } = useTranslation('localization');
  const status = value || 'unknown';
  return (
    <span className={`badge badge-sm ${badgeTone[status] ?? 'badge-ghost'}`}>
      {statusLabel(t, status)}
    </span>
  );
}

/**
 * The reasons the localization domain itself gives an Item (`worker.py`,
 * `batch.py`), worded as `item.reason.<code>` (#369). Any other reason is a
 * dispatch outcome the call coordinator reported (`dispatch_uncertain`, a
 * provider's code) and reads as its `error-<code>` copy.
 */
export const ITEM_REASONS: ReadonlySet<string> = new Set([
  'skipped_market_unavailable',
  'truth_price_unresolved',
  'truth_market_failed',
  'truth_fact_missing_blocked',
  'ineligible_before_dispatch',
  'ineligible_after_dispatch',
  'ineligible_before_publish',
  'ineligible_on_resume',
  'deterministic_qc_failed',
  'qc_exhausted',
  'adopted_isolated_output',
  'batch_cancelled',
]);

export function itemReasonMessage(t: TFunction, code: string): string {
  return ITEM_REASONS.has(code)
    ? t(`item.reason.${code}`)
    : errorCodeMessage(t, code);
}

/** A cost ledger entry's direction, shown as the sign of its amount (#384). */
export const LEDGER_SIGN: Readonly<Record<string, string>> = {
  increase: '+',
  decrease: '−',
};

/** The QC check kinds of `qc.py` and the Worker, worded (#384). */
function qcKindLabel(t: TFunction, kind: string): string {
  return t(`item.qc-kind.${kind}`, { defaultValue: codeWords(kind) });
}

/** A batch command (`_record_command` in `batch.py`) as an event (#400). */
function commandKindLabel(t: TFunction, kind: string): string {
  return t(`workspace.command-kind.${kind}`, { defaultValue: codeWords(kind) });
}

/**
 * Why a market's Truth is not ready (#400). `_derive_market_readiness` in
 * `truth.py` gives a `reason` and the `missing` fact types, plus the
 * `blocked` ones when an `on_failure=block` fact stops the market; a
 * batch whose Truth is not resolved yet reads `pending` with an empty
 * detail (`queries.py`). Any other shape keeps its JSON.
 */
function readinessDetailMessage(t: TFunction, market: TruthMarketRow): string {
  const detail = market.readiness_detail;
  const fallback = t('truth.missing-detail', {
    detail: displayValue(t, detail),
  });
  if (!detail || typeof detail !== 'object')
    return market.readiness === 'pending' ? t('truth.not-resolved') : fallback;
  const { reason, missing, blocked } = detail as UnknownRecord;
  const factList = (value: unknown) =>
    Array.isArray(value) ? value.join(t('list-separator')) : '';
  if (typeof reason === 'string')
    return t(`truth.readiness-reason.${reason}`, {
      facts: factList(missing),
      blocked: factList(blocked),
      defaultValue: fallback,
    });
  return market.readiness === 'pending' && Object.keys(detail).length === 0
    ? t('truth.not-resolved')
    : fallback;
}

function Identifier({ children }: { children: string | null | undefined }) {
  const { t } = useTranslation('localization');
  return (
    <code className="break-all rounded bg-base-300 px-1.5 py-0.5 text-[11px]">
      {children || t('not-recorded')}
    </code>
  );
}

function actionAllowed(
  actions: components['schemas']['LocalizationAction'][] | undefined,
  name: string
) {
  return actions?.find((action) => action.action === name);
}

function displayValue(t: TFunction, value: unknown): string {
  if (value === null || value === undefined) return t('missing');
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
}

function hasCompleteSourceSnapshot(
  value: unknown
): value is Record<
  'market' | 'placement' | 'locale' | 'country' | 'currency',
  string
> {
  if (!value || typeof value !== 'object') return false;
  const snapshot = value as UnknownRecord;
  return ['market', 'placement', 'locale', 'country', 'currency'].every(
    (field) =>
      typeof snapshot[field] === 'string' &&
      (snapshot[field] as string).trim().length > 0
  );
}

export function TruthPanel({ batch }: { batch: BatchDetail }) {
  const { t, i18n } = useTranslation('localization');
  const markets = (batch.markets ?? []) as TruthMarketRow[];
  const degraded =
    batch.truth_status === 'degraded' ||
    markets.some((market) => market.readiness !== 'ready');

  return (
    <Section title={t('truth.title')} subtitle={t('truth.subtitle')}>
      <div
        className={`mb-4 rounded-lg border p-3 ${
          degraded
            ? 'border-warning/50 bg-warning/10'
            : 'border-success/40 bg-success/10'
        }`}
      >
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge value={batch.truth_status} />
          <StatusBadge value={batch.truth_freshness} />
          <Identifier>{batch.truth_package_id}</Identifier>
        </div>
        <p className="mt-2 text-xs">
          {t('truth.summary', {
            readiness: degraded ? t('truth.degraded') : t('truth.ready'),
            validity: batch.truth_valid_until
              ? t('truth.valid-until', {
                  time: formatDateTime(i18n.language, batch.truth_valid_until),
                })
              : t('truth.no-deadline'),
          })}
        </p>
      </div>

      {batch.truth_freshness && batch.truth_freshness !== 'current' ? (
        <div className="alert alert-error mb-4 items-start" role="alert">
          <div>
            <div className="font-semibold">{t('truth.stale-title')}</div>
            <div className="text-xs">
              {t('truth.stale-detail', {
                freshness: t(`status.${batch.truth_freshness}`, {
                  defaultValue: batch.truth_freshness,
                }),
              })}
            </div>
          </div>
        </div>
      ) : null}

      {markets.length === 0 ? (
        <p className="text-sm opacity-60">{t('truth.no-markets')}</p>
      ) : (
        <div className="space-y-3">
          {markets.map((market) => {
            const failures = (market.source_attempts ?? []).filter(
              isFailedTruthAttempt
            );
            return (
              <div
                key={market.market}
                className="rounded-lg border border-base-300 p-3"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="font-medium">
                    {market.market}{' '}
                    <span className="text-xs opacity-60">
                      {market.locale} · {market.currency}
                    </span>
                  </div>
                  <StatusBadge value={market.readiness} />
                </div>
                {market.readiness !== 'ready' ? (
                  <div className="mt-2 rounded bg-warning/10 p-2 text-xs text-warning-content">
                    {readinessDetailMessage(t, market)}
                  </div>
                ) : null}
                <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-2">
                  {(market.facts ?? []).map((fact) => (
                    <div
                      key={fact.id}
                      className="rounded bg-base-200 p-2 text-xs"
                    >
                      <div className="font-semibold">
                        {fact.fact_type} · {fact.scope_key}
                      </div>
                      <div className="mt-1 break-words">
                        {displayValue(t, fact.value)}
                      </div>
                      <div className="mt-1 opacity-60">
                        {t('truth.fact-source', {
                          source:
                            fact.external_object_ref ||
                            t('truth.fact-source-embedded'),
                        })}
                        {fact.valid_until
                          ? ` · ${t('truth.fact-valid-until', { date: formatDateTime(i18n.language, fact.valid_until) })}`
                          : ''}
                      </div>
                    </div>
                  ))}
                  {(market.facts ?? []).length === 0 ? (
                    <div className="rounded bg-warning/10 p-2 text-xs">
                      {t('truth.no-facts')}
                    </div>
                  ) : null}
                </div>
                {failures.length > 0 ? (
                  <div className="mt-3 text-xs">
                    <div className="font-semibold">{t('truth.failures')}</div>
                    {failures.map((attempt) => (
                      <div key={attempt.id} className="mt-1">
                        {t('truth.failure-line', {
                          fact: attempt.fact_type,
                          // The source the attempt asked; `policy` is the
                          // fact's `on_failure` rule after them all (#400).
                          adapter: t(`truth.adapter.${attempt.adapter_kind}`, {
                            defaultValue: codeWords(attempt.adapter_kind),
                          }),
                          // An adapter's failure reads as its error copy; a
                          // skip names the fact's `on_failure` policy (#384).
                          reason: attempt.failure_code
                            ? errorCodeMessage(t, attempt.failure_code)
                            : attempt.skip_code
                              ? t(`truth.skip.${attempt.skip_code}`, {
                                  defaultValue: codeWords(attempt.skip_code),
                                })
                              : t(`status.${attempt.outcome}`, {
                                  defaultValue: codeWords(attempt.outcome),
                                }),
                        })}
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </Section>
  );
}

export function BatchMatrix({
  batch,
  selectedItemId,
  onSelectItem,
}: {
  batch: BatchDetail;
  selectedItemId?: string;
  onSelectItem: (itemId: string) => void;
}) {
  const { t } = useTranslation('localization');
  const sources = (batch.sources ?? []) as BatchSourceRow[];
  const markets = (batch.markets ?? []) as TruthMarketRow[];
  const items = (batch.items ?? []) as BatchItemRow[];

  return (
    <Section title={t('matrix.title')} subtitle={t('matrix.subtitle')}>
      {items.length === 0 ? (
        <p className="text-sm opacity-60">{t('matrix.empty')}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="table table-sm">
            <thead>
              <tr>
                <th>{t('matrix.source-asset')}</th>
                {markets.map((market) => (
                  <th key={market.market}>{market.market}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sources.map((source) => (
                <tr key={source.asset_version_id}>
                  <td>
                    <Identifier>{source.asset_version_id}</Identifier>
                  </td>
                  {markets.map((market) => {
                    const item = items.find(
                      (candidate) =>
                        candidate.source_asset_version_id ===
                          source.asset_version_id &&
                        candidate.target_market === market.market
                    );
                    return (
                      <td key={market.market}>
                        {item ? (
                          <button
                            className={`btn btn-sm ${selectedItemId === item.id ? 'btn-primary' : 'btn-ghost'}`}
                            onClick={() => onSelectItem(item.id)}
                          >
                            <StatusBadge value={item.state} />
                            <span>
                              {t('matrix.attempts', {
                                count: item.attempt_count,
                              })}
                            </span>
                          </button>
                        ) : (
                          <span className="text-xs opacity-40">
                            {t('matrix.not-created')}
                          </span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

/**
 * The two explicit commands that recover one preserved isolated output.
 *
 * The order is not a preference, it is the contract: a blocking QC check
 * that is *missing* has to be authorized and run before adoption can be
 * attempted at all, and the domain refuses a premature adopt with
 * `qc_plan_incomplete`. Surfacing the adopt button as disabled (rather than
 * hiding it) is deliberate -- the user needs to see that adoption is the
 * destination and what is still standing between them and it.
 *
 * A blocking check that ran and *failed* is different again: no command
 * publishes it, so neither button is offered.
 */
function IsolatedOutputRecovery({
  attempt,
  isPending,
  onAuthorizeIsolatedQc,
  onAdoptIsolatedOutput,
}: {
  attempt: AttemptRow;
  isPending: boolean;
  onAuthorizeIsolatedQc?: (attemptId: string) => void;
  onAdoptIsolatedOutput?: (attemptId: string) => void;
}) {
  const { t } = useTranslation('localization');
  const checks = attempt.qc ?? [];
  const blockingFailed = checks.some(
    (qc) => qc.severity === 'blocking' && qc.result === 'fail'
  );
  const pendingChecks = checks.filter(
    (qc) => qc.result !== 'pass' && qc.result !== 'fail'
  );

  if (blockingFailed) {
    return (
      <div className="mt-2 rounded bg-error/10 p-2 text-xs">
        {t('isolated.blocking-failed')}
      </div>
    );
  }

  return (
    <div className="mt-2 flex flex-wrap items-center gap-2">
      <button
        className="btn btn-xs"
        data-testid={`authorize-isolated-qc-${attempt.id}`}
        disabled={isPending || !onAuthorizeIsolatedQc}
        onClick={() => onAuthorizeIsolatedQc?.(attempt.id)}
      >
        {t('isolated.authorize')}
      </button>
      <button
        className="btn btn-xs btn-primary"
        data-testid={`adopt-isolated-output-${attempt.id}`}
        disabled={
          isPending || !onAdoptIsolatedOutput || pendingChecks.length > 0
        }
        onClick={() => onAdoptIsolatedOutput?.(attempt.id)}
      >
        {t('isolated.adopt')}
      </button>
      {pendingChecks.length > 0 ? (
        <span className="text-xs opacity-60">
          {t('isolated.blocked', { count: pendingChecks.length })}
        </span>
      ) : null}
    </div>
  );
}

export function LocalizationItemInspector({
  item,
  onRetry,
  onResume,
  onAuthorizeIsolatedQc,
  onAdoptIsolatedOutput,
  isPending,
}: {
  item: ItemDetail;
  onRetry: () => void;
  onResume: () => void;
  /**
   * Isolated-output recovery (spec §2E/§2H). An Attempt whose external call
   * completed but whose Item never published leaves its output preserved
   * and unpublished. Authorizing admits only the frozen QC steps that
   * attempt still owes; adopting publishes it once they have all passed.
   * Both are optional so the existing render-only tests keep working.
   */
  onAuthorizeIsolatedQc?: (attemptId: string) => void;
  onAdoptIsolatedOutput?: (attemptId: string) => void;
  isPending: boolean;
}) {
  const { t } = useTranslation('localization');
  const attempts = (item.attempts ?? []) as AttemptRow[];
  const cost = (item.cost ?? {}) as ItemCost;
  const lineage = (item.lineage ?? {}) as UnknownRecord;
  const binding = item.binding;
  const retry = actionAllowed(item.allowed_actions, 'retry');
  const resume = actionAllowed(item.allowed_actions, 'resume');
  const blockingFailures = attempts.flatMap((attempt) =>
    (attempt.qc ?? []).filter(
      (qc) => qc.severity === 'blocking' && qc.result !== 'pass'
    )
  );

  return (
    <Section
      title={t('item.title')}
      subtitle={`${item.target_market} · ${item.id}`}
    >
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div>
          <div className="text-xs opacity-60">{t('item.state')}</div>
          <StatusBadge value={item.state} />
        </div>
        <div>
          <div className="text-xs opacity-60">{t('item.attempts')}</div>
          <div>
            {item.attempt_count} / {item.max_attempts}
          </div>
        </div>
        <div>
          <div className="text-xs opacity-60">{t('item.recovery')}</div>
          <div>
            {item.next_action
              ? t(`item.next-action.${item.next_action}`, {
                  defaultValue: codeWords(item.next_action),
                })
              : t('none')}
          </div>
        </div>
      </div>
      {item.reason_code ? (
        <div className="mt-3 rounded-lg border border-warning/50 bg-warning/10 p-3 text-sm">
          <p>{itemReasonMessage(t, item.reason_code)}</p>
          {/* The code and its detail (eligibility findings, failing checks,
              the provider's own words) are for support, not the sentence. */}
          <details className="mt-2 text-xs">
            <summary className="cursor-pointer opacity-70">
              {t('item.technical-details')}
            </summary>
            <p className="mt-1 break-words">
              <Identifier>{item.reason_code}</Identifier>{' '}
              {item.reason_detail || t('item.no-detail')}
            </p>
          </details>
        </div>
      ) : null}
      {blockingFailures.length > 0 ? (
        <div className="alert alert-error mt-3 items-start">
          <div>
            <div className="font-semibold">{t('item.blocking-title')}</div>
            {blockingFailures.map((qc) => (
              <div key={qc.id} className="text-xs">
                {t('item.blocking-line', {
                  // The slot key names which check of that kind failed.
                  check: qc.check_slot_key
                    ? `${qcKindLabel(t, qc.check_kind)} (${qc.check_slot_key})`
                    : qcKindLabel(t, qc.check_kind),
                  expected: displayValue(t, qc.expected),
                  observed: displayValue(t, qc.observed),
                })}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <div className="mt-4 flex flex-wrap gap-2">
        <button
          className="btn btn-sm btn-primary"
          disabled={!retry?.allowed || isPending}
          onClick={onRetry}
        >
          {t('item.retry')}
        </button>
        <button
          className="btn btn-sm"
          disabled={!resume?.allowed || isPending}
          onClick={onResume}
        >
          {t('item.resume')}
        </button>
        {!retry?.allowed ? (
          <span className="self-center text-xs opacity-60">
            {t('item.retry-unavailable', {
              reason: retry?.reason_code
                ? actionReasonMessage(t, retry.reason_code)
                : t('not-authorized'),
            })}
          </span>
        ) : null}
      </div>

      <div className="mt-5 space-y-3">
        {attempts.length === 0 ? (
          <p className="text-sm opacity-60">{t('item.no-attempts')}</p>
        ) : null}
        {attempts.map((attempt) => (
          <div
            key={attempt.id}
            className="rounded-lg border border-base-300 p-3"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="font-medium">
                {t('item.attempt-title', {
                  number: attempt.attempt_number,
                  provider: attempt.provider,
                  model: attempt.model,
                })}
              </div>
              <StatusBadge value={attempt.state} />
            </div>
            <div className="mt-2 text-xs">
              {t('item.output', {
                state: attempt.output
                  ? attempt.publication
                    ? t('item.output-published')
                    : t('item.output-isolated')
                  : t('none'),
              })}
            </div>
            {attempt.output && !attempt.publication ? (
              <IsolatedOutputRecovery
                attempt={attempt}
                isPending={isPending}
                onAuthorizeIsolatedQc={onAuthorizeIsolatedQc}
                onAdoptIsolatedOutput={onAdoptIsolatedOutput}
              />
            ) : null}
            {(attempt.provider_calls ?? []).map((call, index) => (
              <div
                key={`${call.step}-${index}`}
                className="mt-2 rounded bg-base-200 p-2 text-xs"
              >
                {t(`item.call-step.${call.step}`, {
                  defaultValue: codeWords(call.step),
                })}
                : {call.provider}/{call.model || t('default-model')} ·{' '}
                {t(`status.${call.dispatch_state}`, {
                  defaultValue: codeWords(call.dispatch_state),
                })}{' '}
                ·{' '}
                {t(`item.call-charge.${call.chargeability}`, {
                  defaultValue: codeWords(call.chargeability),
                })}{' '}
                ·{' '}
                {call.maximum_cost
                  ? t('item.call-max-cost', {
                      amount: call.maximum_cost.amount,
                      currency: call.maximum_cost.currency,
                      state: t(`item.reservation.${call.maximum_cost.state}`, {
                        defaultValue: codeWords(call.maximum_cost.state),
                      }),
                    })
                  : t('item.call-no-max-cost')}
                {call.error_code
                  ? ` · ${t('item.call-error', {
                      message: errorCodeMessage(t, call.error_code),
                    })}`
                  : ''}
              </div>
            ))}
            {(attempt.qc ?? []).length > 0 ? (
              <div className="mt-3 overflow-x-auto">
                <table className="table table-xs">
                  <thead>
                    <tr>
                      <th>{t('item.qc-check')}</th>
                      <th>{t('item.severity')}</th>
                      <th>{t('item.result')}</th>
                      <th>{t('item.rule')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(attempt.qc ?? []).map((qc) => (
                      <tr key={qc.id}>
                        <td>{qcKindLabel(t, qc.check_kind)}</td>
                        <td>
                          {t(`item.qc-severity.${qc.severity}`, {
                            defaultValue: codeWords(qc.severity),
                          })}
                        </td>
                        <td>
                          <StatusBadge value={qc.result} />
                        </td>
                        <td>{qc.rule_version || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="mt-2 text-xs opacity-60">{t('item.no-qc')}</div>
            )}
          </div>
        ))}
      </div>

      <div className="mt-5 rounded-lg border border-base-300 p-3">
        <div className="font-semibold">{t('item.cost-title')}</div>
        {(cost.totals ?? []).length > 0 ? (
          <div className="mt-1 text-lg font-semibold">
            {(cost.totals ?? [])
              .map((total) => `${total.amount} ${total.currency}`)
              .join(' · ')}
          </div>
        ) : (
          <div className="mt-1 text-sm">{t('item.no-cost')}</div>
        )}
        <div className="mt-2 space-y-1 text-xs">
          {(cost.ledger ?? []).map((entry, index) => (
            <div key={`${entry.entry_type}-${index}`}>
              {t(`item.ledger-entry.${entry.entry_type}`, {
                defaultValue: codeWords(entry.entry_type),
              })}
              : {LEDGER_SIGN[entry.direction] ?? `${entry.direction} `}
              {entry.amount} {entry.currency} · {entry.provider}/
              {entry.model || t('default-model')}
            </div>
          ))}
        </div>
      </div>

      <div className="mt-5 rounded-lg border border-base-300 p-3">
        <div className="font-semibold">{t('item.lineage-title')}</div>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          <Identifier>
            {String(lineage.source_asset_version_id || '')}
          </Identifier>
          <span>{t('item.lineage-decision')}</span>
          <Identifier>{String(lineage.decision_id || '')}</Identifier>
          <span>{t('item.lineage-truth')}</span>
          <Identifier>{String(lineage.truth_package_id || '')}</Identifier>
          <span>{t('item.lineage-copy')}</span>
          <Identifier>{String(lineage.copy_version_id || '')}</Identifier>
          <span>{t('item.lineage-localized')}</span>
          <Identifier>
            {String(lineage.localized_asset_version_id || '')}
          </Identifier>
        </div>
        <div className="mt-2 text-xs">
          {t('item.binding', {
            // Review and freshness are server states; whether the binding can
            // enter a new experiment reads as a sentence (#400).
            binding: binding
              ? t('item.binding-detail', {
                  kind: binding.kind
                    ? t(`item.binding-type.${binding.kind}`, {
                        defaultValue: codeWords(binding.kind),
                      })
                    : t('item.binding-kind'),
                  review: statusLabel(t, binding.review_state),
                  freshness: statusLabel(t, binding.truth_freshness),
                  ready: binding.ready_for_experiment
                    ? t('item.binding-ready')
                    : t('item.binding-not-ready'),
                })
              : t('none'),
          })}
        </div>
        {binding?.truth_freshness === 'stale' ? (
          <div className="alert alert-error mt-2 text-xs" role="alert">
            {t('item.stale-binding')}
          </div>
        ) : null}
        {lineage.localized_asset_version_id ? (
          <div className="mt-2 rounded bg-info/10 p-2 text-xs">
            {t('item.child-review')}
          </div>
        ) : null}
      </div>
    </Section>
  );
}

function BatchRail({
  page,
  selectedBatchId,
  onSelect,
}: {
  page?: BatchPage;
  selectedBatchId?: string;
  onSelect: (batchId: string) => void;
}) {
  const { t } = useTranslation('localization');
  const batches = page?.items ?? [];
  if (batches.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-base-300 p-5 text-sm opacity-60">
        {t('rail.empty')}
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {batches.map((batch: BatchSummary) => (
        <button
          key={batch.id}
          className={`w-full rounded-xl border p-3 text-left ${selectedBatchId === batch.id ? 'border-primary bg-primary/10' : 'border-base-300 bg-base-100'}`}
          onClick={() => onSelect(batch.id)}
        >
          <div className="flex items-center justify-between gap-2">
            <Identifier>{batch.id}</Identifier>
            <StatusBadge value={batch.phase} />
          </div>
          <div className="mt-2 flex flex-wrap gap-2 text-xs">
            <span>{t('rail.sources', { count: batch.source_count })}</span>
            <span>{t('rail.markets', { count: batch.market_count })}</span>
            <span>{t('rail.items', { count: batch.item_count })}</span>
            <StatusBadge value={batch.outcome} />
          </div>
        </button>
      ))}
    </div>
  );
}

export function NewBatchPanel({
  workspace,
  onCreated,
  preferredDecisionId,
}: {
  workspace: Workspace;
  preferredDecisionId?: string;
  onCreated: (batchId: string) => Promise<void>;
}) {
  const { t } = useTranslation('localization');
  const command = useDomainCommand('create_localization_batch');
  const sources = useMemo(
    () => workspace.eligible_sources ?? [],
    [workspace.eligible_sources]
  );
  const markets = useMemo(() => workspace.markets ?? [], [workspace.markets]);
  const [sourceId, setSourceId] = useState('');
  const [marketIds, setMarketIds] = useState<string[]>([]);

  useEffect(() => {
    if (!sourceId && sources[0])
      setSourceId(
        sources.find((source) => source.decision_id === preferredDecisionId)
          ?.decision_id ?? sources[0].decision_id
      );
    if (marketIds.length === 0 && markets[0]) setMarketIds([markets[0].market]);
  }, [marketIds.length, markets, sourceId, sources, preferredDecisionId]);

  const selectedSource = sources.find(
    (source) => source.decision_id === sourceId
  );
  const sourceSnapshot = selectedSource?.source_market_snapshot;
  const sourceSnapshotReady = hasCompleteSourceSnapshot(sourceSnapshot);
  const sourceContextProblem = selectedSource?.source_context_problem;
  // Worded per code with its field names (#400); a source without a
  // complete snapshot and no reported problem is missing its context.
  const sourceContextProblemCode =
    sourceContextProblem?.code ?? 'missing_source_context';
  const sourceContextProblemFields = sourceContextProblem?.missing_fields
    ?.length
    ? sourceContextProblem.missing_fields
    : sourceContextProblem?.invalid_fields?.length
      ? sourceContextProblem.invalid_fields
      : (sourceContextProblem?.conflicting_fields ?? []);

  async function createBatch() {
    if (!selectedSource || !sourceSnapshotReady || marketIds.length === 0)
      return;
    const selectedMarkets = markets.filter((market) =>
      marketIds.includes(market.market)
    );
    const result = await command.execute({
      actionInstanceId: newActionId(),
      body: {
        product_version_id: selectedSource.product_version_id,
        offer_version_id: selectedSource.offer_version_id,
        truth_data_mode:
          selectedSource.experiment_evidence_mode === 'production'
            ? 'real'
            : 'mock',
        provider_execution_mode:
          selectedSource.experiment_evidence_mode === 'production'
            ? 'byok'
            : 'mock',
        requested_evidence_tier: selectedSource.experiment_evidence_mode,
        sources: [
          {
            asset_version_id: selectedSource.asset_version_id,
            decision_id: selectedSource.decision_id,
          },
        ],
        market_targets: selectedMarkets.map((market) => ({
          market: market.market,
          market_policy_version_id: market.market_policy_version_id,
        })),
      },
    });
    if (result.kind === 'success') await onCreated(result.data.batch_id);
  }

  return (
    <Section title={t('new-batch.title')} subtitle={t('new-batch.subtitle')}>
      {sources.length === 0 ? (
        <div className="rounded-lg bg-base-200 p-3 text-sm">
          {t('new-batch.no-sources')}
        </div>
      ) : (
        <div className="space-y-3">
          <label className="form-control">
            <span className="label-text mb-1 text-xs">
              {t('new-batch.source-label')}
            </span>
            <select
              className="select select-bordered select-sm"
              value={sourceId}
              onChange={(event) => setSourceId(event.target.value)}
            >
              {sources.map((source: Source) => (
                <option key={source.decision_id} value={source.decision_id}>
                  {source.evidence_origin === 'marketing_business_target'
                    ? t('new-batch.origin-marketing')
                    : t('new-batch.origin-historical')}{' '}
                  · {String(source.offer?.label || source.offer_version_id)} ·{' '}
                  {source.source_market} · {source.asset_version_id}
                </option>
              ))}
            </select>
          </label>
          {selectedSource?.evidence_origin === 'marketing_business_target' && (
            <p className="rounded border p-3 text-sm">
              {t('new-batch.marketing-note')}
            </p>
          )}
          <div>
            <div className="mb-1 text-xs">{t('new-batch.target-markets')}</div>
            <div className="flex flex-wrap gap-2">
              {markets.map((market) => (
                <label
                  key={market.market}
                  className="label cursor-pointer gap-2 rounded border border-base-300 px-2 py-1"
                >
                  <input
                    className="checkbox checkbox-xs"
                    type="checkbox"
                    checked={marketIds.includes(market.market)}
                    onChange={() =>
                      setMarketIds((current) =>
                        current.includes(market.market)
                          ? current.filter((id) => id !== market.market)
                          : [...current, market.market]
                      )
                    }
                  />
                  <span className="label-text text-xs">
                    {market.market} · {market.locale}
                  </span>
                </label>
              ))}
            </div>
          </div>
          {!sourceSnapshotReady ? (
            <div className="alert alert-warning text-sm" role="alert">
              {t(`new-batch.context-problem.${sourceContextProblemCode}`, {
                fields:
                  sourceContextProblemFields
                    .map((field) =>
                      t(`new-batch.context-field.${field}`, {
                        defaultValue: codeWords(field),
                      })
                    )
                    .join(t('list-separator')) ||
                  t('new-batch.missing-context-fields'),
                defaultValue: codeWords(sourceContextProblemCode),
              })}
            </div>
          ) : null}
          <button
            className="btn btn-primary btn-sm"
            disabled={
              command.isPending ||
              marketIds.length === 0 ||
              !sourceSnapshotReady
            }
            onClick={createBatch}
          >
            {t('new-batch.create')}
          </button>
        </div>
      )}
      <ProblemBanner problem={command.problem} />
    </Section>
  );
}

export function LocalizationWorkspace() {
  const { t, i18n } = useTranslation('localization');
  const router = useRouter();
  const selectedBatchId =
    typeof router.query.batch === 'string' ? router.query.batch : undefined;
  const selectedItemId =
    typeof router.query.item === 'string' ? router.query.item : undefined;
  const workspaceQuery = useDomainQuery('get_localization_workspace', {});
  const batchesQuery = useDomainQuery('list_localization_batches', {});
  const batchQuery = useDomainQuery(
    'get_localization_batch',
    selectedBatchId ? { batch_id: selectedBatchId } : undefined,
    { enabled: Boolean(selectedBatchId), staleAfterMs: 10_000 }
  );
  const itemQuery = useDomainQuery(
    'get_localization_item',
    selectedBatchId && selectedItemId
      ? { batch_id: selectedBatchId, item_id: selectedItemId }
      : undefined,
    {
      enabled: Boolean(selectedBatchId && selectedItemId),
      staleAfterMs: 10_000,
    }
  );
  const approveCommand = useDomainCommand('approve_localization_copy');
  const confirmCommand = useDomainCommand('confirm_localization_batch');
  const cancelCommand = useDomainCommand('cancel_localization_batch');
  const retryCommand = useDomainCommand('retry_localization_item');
  const resumeCommand = useDomainCommand('resume_localization_item');
  const authorizeIsolatedQcCommand = useDomainCommand(
    'authorize_localization_isolated_qc'
  );
  const adoptIsolatedOutputCommand = useDomainCommand(
    'adopt_localization_isolated_output'
  );
  const [editProvider, setEditProvider] = useState('mock');
  const [editModel, setEditModel] = useState('localization-edit-v1');
  const [adapterVersion, setAdapterVersion] = useState('v1');

  const page = batchesQuery.data;
  const batch = batchQuery.data;
  const item = itemQuery.data;
  const latestOperationCommand = [...(batch?.commands ?? [])]
    .reverse()
    .find((command) => Boolean(command.operation_id));
  const operation = useOperation({
    operationRecordId: latestOperationCommand?.operation_id ?? null,
    purpose: 'localization',
    actionInstanceId: latestOperationCommand?.id ?? 'localization-idle',
  });

  const updateSelection = (batchId?: string, itemId?: string) => {
    const query = { ...router.query };
    if (batchId) query.batch = batchId;
    else delete query.batch;
    if (itemId) query.item = itemId;
    else delete query.item;
    void router.replace({ pathname: router.pathname, query }, undefined, {
      shallow: true,
    });
  };

  useEffect(() => {
    const firstBatch = page?.items?.[0];
    if (!selectedBatchId && firstBatch) updateSelection(firstBatch.id);
    // Only restore the latest persisted batch when the collection changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, selectedBatchId]);

  useEffect(() => {
    if (!batch || !['preparing', 'running'].includes(batch.phase)) return;
    const timer = window.setInterval(() => {
      void batchQuery.refresh();
      if (selectedItemId) void itemQuery.refresh();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [batch, batchQuery, itemQuery, selectedItemId]);

  const commandProblem =
    approveCommand.problem ||
    confirmCommand.problem ||
    cancelCommand.problem ||
    retryCommand.problem ||
    resumeCommand.problem ||
    authorizeIsolatedQcCommand.problem ||
    adoptIsolatedOutputCommand.problem;
  const copies = (batch?.copies ?? []) as CopyRow[];
  const confirmAction = actionAllowed(batch?.allowed_actions, 'confirm');
  const cancelAction = actionAllowed(batch?.allowed_actions, 'cancel');
  const offerSourceSummary = useMemo(() => {
    if (!batch) return [];
    const sourceRows = (batch.sources ?? []) as BatchSourceRow[];
    return sourceRows.map((source) => ({
      ...source,
      offer_version_id: batch.offer_version_id,
      product_version_id: batch.product_version_id,
    }));
  }, [batch]);

  async function refreshSelected() {
    await Promise.all([
      batchesQuery.refresh(),
      batchQuery.refresh(),
      itemQuery.refresh(),
    ]);
  }

  useEffect(() => {
    if (!operation.isTerminal) return;
    void refreshSelected();
    // Refresh exactly when the latest durable Operation reaches a terminal state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [operation.isTerminal, latestOperationCommand?.operation_id]);

  async function approve(copyId: string) {
    if (!batch) return;
    const result = await approveCommand.execute({
      pathParams: { batch_id: batch.id },
      body: { copy_version_id: copyId },
      ifMatch: batch.revision,
      actionInstanceId: newActionId(),
    });
    if (result.kind === 'success') await refreshSelected();
  }

  async function confirm() {
    if (!batch) return;
    const result = await confirmCommand.execute({
      pathParams: { batch_id: batch.id },
      body: {
        edit_provider: editProvider,
        edit_model: editModel,
        edit_adapter_version: adapterVersion,
        external_qc_steps: [],
        max_attempts: batch.max_attempts,
      },
      ifMatch: batch.revision,
      actionInstanceId: newActionId(),
    });
    if (result.kind === 'success') await refreshSelected();
  }

  async function cancel() {
    if (!batch) return;
    const result = await cancelCommand.execute({
      pathParams: { batch_id: batch.id },
      ifMatch: batch.revision,
      actionInstanceId: newActionId(),
    });
    if (result.kind === 'success') await refreshSelected();
  }

  async function retry() {
    if (!batch || !item) return;
    const result = await retryCommand.execute({
      pathParams: { batch_id: batch.id, item_id: item.id },
      ifMatch: item.revision,
      actionInstanceId: newActionId(),
    });
    if (result.kind === 'success') await refreshSelected();
  }

  async function resume() {
    if (!batch || !item) return;
    const result = await resumeCommand.execute({
      pathParams: { batch_id: batch.id, item_id: item.id },
      ifMatch: item.revision,
      actionInstanceId: newActionId(),
    });
    if (result.kind === 'success') await refreshSelected();
  }

  // Isolated-output recovery (spec §2E/§2H). Neither command sends
  // `ifMatch`: they act on one Attempt's preserved output rather than
  // transitioning the Item against a revision the user was shown.
  function runAttemptCommand(command: {
    execute: (params: {
      pathParams: { batch_id: string; item_id: string; attempt_id: string };
      actionInstanceId: string;
    }) => Promise<{ kind: string }>;
  }) {
    return async (attemptId: string) => {
      if (!batch || !item) return;
      const result = await command.execute({
        pathParams: {
          batch_id: batch.id,
          item_id: item.id,
          attempt_id: attemptId,
        },
        actionInstanceId: newActionId(),
      });
      if (result.kind === 'success') await refreshSelected();
    };
  }

  const authorizeIsolatedQc = runAttemptCommand(authorizeIsolatedQcCommand);
  const adoptIsolatedOutput = runAttemptCommand(adoptIsolatedOutputCommand);

  const loading = workspaceQuery.isLoading || batchesQuery.isLoading;
  return (
    <div className="space-y-5 pb-10">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">{t('workspace.title')}</h1>
          <p className="text-sm opacity-60">{t('workspace.intro')}</p>
        </div>
        <button className="btn btn-sm" onClick={refreshSelected}>
          {t('workspace.refresh')}
        </button>
      </div>

      {/* Through the BFF a user is an Owner or a Member; only a Member is
          told, so an Owner never sees an unknown role while loading (#400). */}
      {workspaceQuery.data?.role === 'member' ? (
        <div className="alert alert-info text-sm">
          {t('workspace.role-note')}
        </div>
      ) : null}
      <ProblemBanner
        problem={
          workspaceQuery.problem ||
          batchesQuery.problem ||
          batchQuery.problem ||
          itemQuery.problem ||
          commandProblem
        }
      />
      {loading ? (
        <div
          className="skeleton h-28 w-full"
          aria-label={t('workspace.loading')}
        />
      ) : null}

      {workspaceQuery.data ? (
        <NewBatchPanel
          preferredDecisionId={
            typeof router.query.source === 'string'
              ? router.query.source
              : undefined
          }
          workspace={workspaceQuery.data}
          onCreated={async (batchId) => {
            await batchesQuery.refresh();
            updateSelection(batchId);
          }}
        />
      ) : null}

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[300px_minmax(0,1fr)]">
        <aside>
          <BatchRail
            page={page}
            selectedBatchId={selectedBatchId}
            onSelect={(id) => updateSelection(id)}
          />
        </aside>
        <main className="space-y-5">
          {selectedBatchId && batchQuery.isLoading ? (
            <div className="skeleton h-52 w-full" />
          ) : null}
          {batch ? (
            <>
              <Section
                title={t('workspace.source-title')}
                subtitle={t('workspace.source-subtitle', {
                  revision: batch.revision,
                  time: formatDateTime(i18n.language, batch.created_at),
                })}
              >
                <div className="flex flex-wrap gap-2">
                  <StatusBadge value={batch.phase} />
                  <StatusBadge value={batch.outcome} />
                  <StatusBadge value={batch.evidence_tier} />
                </div>
                <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
                  <div className="rounded bg-base-200 p-3 text-xs">
                    <div className="font-semibold">
                      {t('workspace.product-offer')}
                    </div>
                    <div className="mt-1">
                      <Identifier>{batch.product_version_id}</Identifier> ·{' '}
                      <Identifier>{batch.offer_version_id}</Identifier>
                    </div>
                  </div>
                  <div className="rounded bg-base-200 p-3 text-xs">
                    <div className="font-semibold">
                      {t('workspace.source-snapshot')}
                    </div>
                    <div className="mt-1 break-words">
                      {displayValue(t, batch.source_market_snapshot)}
                    </div>
                  </div>
                  {offerSourceSummary.map((source) => (
                    <div
                      key={source.asset_version_id}
                      className="rounded bg-base-200 p-3 text-xs"
                    >
                      <div className="font-semibold">
                        {t('workspace.source-creative')}
                      </div>
                      <div className="mt-1">
                        <Identifier>{source.asset_version_id}</Identifier>
                      </div>
                      <div className="mt-1">
                        {t('workspace.decision')}{' '}
                        <Identifier>{source.decision_id}</Identifier>
                      </div>
                      <div className="mt-1">
                        {t('workspace.evidence-modes', {
                          generation: statusLabel(
                            t,
                            source.source_generation_mode
                          ),
                          evidence: statusLabel(
                            t,
                            source.experiment_evidence_mode
                          ),
                        })}
                      </div>
                    </div>
                  ))}
                </div>
                <div className="mt-3 flex flex-wrap gap-2 text-xs">
                  {Object.entries(batch.item_state_counts ?? {}).map(
                    ([state, count]) => (
                      <span
                        key={state}
                        className="rounded bg-base-200 px-2 py-1"
                      >
                        {t('workspace.state-count', {
                          state: t(`status.${state}`, { defaultValue: state }),
                          value: count,
                        })}
                      </span>
                    )
                  )}
                  {batch.item_count === 0 ? (
                    <span>{t('workspace.no-outcome')}</span>
                  ) : null}
                </div>
              </Section>

              <TruthPanel batch={batch} />

              {workspaceQuery.data ? (
                <LocalizationPreparation
                  workspace={workspaceQuery.data}
                  batch={batch}
                  onRefresh={refreshSelected}
                />
              ) : null}

              {latestOperationCommand ? (
                <Section
                  title={t('workspace.operation-title')}
                  subtitle={t('workspace.operation-subtitle')}
                >
                  <OperationStatus
                    operation={
                      operation.operation
                        ? { status: operation.operation.state }
                        : { status: 'queued' }
                    }
                    pollingProblem={operation.problem}
                    outcomeSummary={
                      <span>
                        {commandKindLabel(
                          t,
                          latestOperationCommand.command_kind
                        )}{' '}
                        ·{' '}
                        <Identifier>
                          {latestOperationCommand.operation_id}
                        </Identifier>
                      </span>
                    }
                  />
                  <div className="mt-3 space-y-1 text-xs">
                    {(batch.commands ?? []).map((command) => (
                      <div key={command.id}>
                        {commandKindLabel(t, command.command_kind)} ·{' '}
                        {command.actor_id} ·{' '}
                        {formatDateTime(i18n.language, command.created_at)}
                        {command.operation_id ? (
                          <>
                            {' · '}
                            <Identifier>{command.operation_id}</Identifier>
                          </>
                        ) : null}
                      </div>
                    ))}
                  </div>
                </Section>
              ) : null}

              <Section
                title={t('workspace.copy-title')}
                subtitle={t('workspace.copy-subtitle')}
              >
                {copies.length === 0 ? (
                  <p className="text-sm opacity-60">{t('workspace.no-copy')}</p>
                ) : null}
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  {copies.map((copy) => (
                    <div
                      key={copy.id}
                      className="rounded-lg border border-base-300 p-3 text-sm"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <strong>{copy.target_market}</strong>
                        <StatusBadge value={copy.state} />
                      </div>
                      <div className="mt-1 text-xs">
                        {t('workspace.copy-origin', {
                          origin: t(
                            `workspace.copy-origins.${copy.copy_origin}`,
                            { defaultValue: codeWords(copy.copy_origin) }
                          ),
                        })}{' '}
                        <Identifier>{copy.source_asset_version_id}</Identifier>
                      </div>
                      <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-base-200 p-2 text-xs">
                        {displayValue(t, copy.content)}
                      </pre>
                      <div className="mt-2 text-xs">
                        {t('workspace.copy-evidence')}{' '}
                        <Identifier>{copy.evidence_hash}</Identifier>
                      </div>
                      <button
                        className="btn btn-primary btn-xs mt-2"
                        disabled={
                          copy.state !== 'needs_review' ||
                          approveCommand.isPending
                        }
                        onClick={() => approve(copy.id)}
                      >
                        {t('workspace.approve')}
                      </button>
                    </div>
                  ))}
                </div>
              </Section>

              <BatchMatrix
                batch={batch}
                selectedItemId={selectedItemId}
                onSelectItem={(id) => updateSelection(batch.id, id)}
              />

              <Section
                title={t('workspace.commands-title')}
                subtitle={t('workspace.commands-subtitle')}
              >
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                  <input
                    className="input input-bordered input-sm"
                    aria-label={t('workspace.edit-provider')}
                    value={editProvider}
                    onChange={(event) => setEditProvider(event.target.value)}
                  />
                  <input
                    className="input input-bordered input-sm"
                    aria-label={t('workspace.edit-model')}
                    value={editModel}
                    onChange={(event) => setEditModel(event.target.value)}
                  />
                  <input
                    className="input input-bordered input-sm"
                    aria-label={t('workspace.adapter-version')}
                    value={adapterVersion}
                    onChange={(event) => setAdapterVersion(event.target.value)}
                  />
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    className="btn btn-primary btn-sm"
                    disabled={
                      !confirmAction?.allowed || confirmCommand.isPending
                    }
                    onClick={confirm}
                  >
                    {t('workspace.confirm')}
                  </button>
                  <button
                    className="btn btn-error btn-outline btn-sm"
                    disabled={!cancelAction?.allowed || cancelCommand.isPending}
                    onClick={cancel}
                  >
                    {t('workspace.cancel')}
                  </button>
                  {!confirmAction?.allowed ? (
                    <span className="self-center text-xs opacity-60">
                      {t('workspace.confirm-unavailable', {
                        reason: confirmAction?.reason_code
                          ? actionReasonMessage(t, confirmAction.reason_code)
                          : t('not-authorized'),
                      })}
                    </span>
                  ) : null}
                </div>
                <div className="mt-3 text-xs opacity-60">
                  <Trans
                    t={t}
                    i18nKey="workspace.preparation-note"
                    values={{ operation: 'start_localization_preparation' }}
                    components={{ code: <Identifier>{null}</Identifier> }}
                  />
                </div>
              </Section>

              {selectedItemId && itemQuery.isLoading ? (
                <div className="skeleton h-48 w-full" />
              ) : null}
              {item ? (
                <LocalizationItemInspector
                  item={item}
                  onRetry={retry}
                  onResume={resume}
                  onAuthorizeIsolatedQc={authorizeIsolatedQc}
                  onAdoptIsolatedOutput={adoptIsolatedOutput}
                  isPending={
                    retryCommand.isPending ||
                    resumeCommand.isPending ||
                    authorizeIsolatedQcCommand.isPending ||
                    adoptIsolatedOutputCommand.isPending
                  }
                />
              ) : null}
            </>
          ) : selectedBatchId &&
            !batchQuery.isLoading &&
            !batchQuery.problem ? (
            <div className="rounded-xl border border-dashed border-base-300 p-8 text-center text-sm opacity-60">
              {t('workspace.select-batch')}
            </div>
          ) : null}
        </main>
      </div>
    </div>
  );
}
