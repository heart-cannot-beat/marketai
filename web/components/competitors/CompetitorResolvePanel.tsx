import { useEffect, useId, useState } from 'react';
import { useTranslation } from 'next-i18next';
import useSWR from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { formatDateTime } from '@/lib/i18n/format';
import { CandidateCard } from './CandidateCard';
import { CredentialHint, SimulatedBadge } from './CredentialNotice';
import {
  ACTIVE_STATUSES,
  POLL_MS,
  codeMessage,
  dataOf,
  formatCount,
  formatUsd,
  quoteLineCounts,
  useCommand,
  type Competitor,
  type CompetitorCandidate,
  type CompetitorResolution,
  type ExecutionMode,
} from './shared';

const BUTTON =
  'rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40';
const SECONDARY =
  'rounded-lg border border-gray-300 px-4 py-2 text-sm disabled:opacity-40';
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm focus:border-gray-600 focus:outline-none focus:ring-2 focus:ring-gray-200';

/**
 * Resolve and confirm one competitor's Meta page (spec §4.3, §4.4). A name
 * search and the domain probes are paid, so the quote is on screen before
 * "Resolve"; an alias-only page link is resolved the same way (#269). A
 * link carrying a page id confirms for free.
 */
export function CompetitorResolvePanel({
  competitor,
  country,
  onChanged,
  onClose,
}: {
  competitor: Competitor;
  country: string;
  onChanged: () => Promise<unknown>;
  onClose: () => void;
}) {
  const { t, i18n } = useTranslation('competitors');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const command = useCommand();
  const linkId = useId();
  const latest = competitor.latest_resolution;
  const [resolveJobId, setResolveJobId] = useState<string | null>(
    latest && (latest.reusable || ACTIVE_STATUSES.includes(latest.status))
      ? latest.resolve_job_id
      : null
  );
  const [mode, setMode] = useState<ExecutionMode | null>(null);
  const [pageUrl, setPageUrl] = useState('');

  const quote = useSWR(
    competitor.can_resolve
      ? [
          'competitor-resolve-quote',
          current.workspaceId,
          competitor.competitor_id,
          country,
          mode,
        ]
      : null,
    async () =>
      dataOf(
        await gateway.quoteCompetitorResolve({
          workspaceSlug: current.slug,
          competitorId: competitor.competitor_id,
          country,
          executionMode: mode ?? undefined,
        })
      )
  );
  const resolution = useSWR(
    resolveJobId
      ? [
          'competitor-resolution',
          current.workspaceId,
          competitor.competitor_id,
          resolveJobId,
        ]
      : null,
    async () =>
      dataOf(
        await gateway.getCompetitorResolution({
          workspaceSlug: current.slug,
          competitorId: competitor.competitor_id,
          resolveJobId: resolveJobId!,
        })
      ),
    {
      refreshInterval: (latest) =>
        !latest || ACTIVE_STATUSES.includes(latest.status) ? POLL_MS : 0,
    }
  );
  const found = resolution.data;
  // A resolve that confirmed by itself changes the row; reload it once.
  const finishedStatus =
    found && !ACTIVE_STATUSES.includes(found.status) ? found.status : null;
  useEffect(() => {
    if (finishedStatus) void onChanged();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finishedStatus, found?.auto_confirmed]);

  const chosenMode: ExecutionMode | undefined =
    mode ?? quote.data?.execution_mode;
  const mockOnly = !!quote.data && !quote.data.available_modes.includes('byok');

  async function startResolve() {
    if (!quote.data) return;
    const body = { execution_mode: chosenMode, country };
    const started = await command.run(
      `resolve:${competitor.competitor_id}:${JSON.stringify(body)}`,
      async (action) =>
        dataOf(
          await gateway.resolveCompetitor({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            competitorId: competitor.competitor_id,
            body,
          })
        )
    );
    if (started) {
      setResolveJobId(started.resolve_job_id);
      await resolution.mutate(started, { revalidate: false });
    }
  }

  async function confirm(body: { resolve_job_id: string; page_id: string }) {
    const confirmed = await command.run(
      `confirm:${competitor.competitor_id}:${JSON.stringify(body)}`,
      async (action) =>
        dataOf(
          await gateway.confirmCompetitor({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            competitorId: competitor.competitor_id,
            body,
          })
        )
    );
    if (confirmed) {
      await onChanged();
      onClose();
    }
  }

  async function confirmByLink() {
    const body = { page_url: pageUrl.trim() };
    const confirmed = await command.run(
      `link:${competitor.competitor_id}:${body.page_url}`,
      async (action) =>
        dataOf(
          await gateway.confirmCompetitor({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            competitorId: competitor.competitor_id,
            body,
          })
        )
    );
    if (confirmed) {
      await onChanged();
      onClose();
    }
  }

  const resolving = !!found && ACTIVE_STATUSES.includes(found.status);
  // A reusable resolve confirms without paying again (72 hours).
  const showQuote =
    competitor.can_resolve && (!found || (!found.reusable && !resolving));

  return (
    <div
      className="space-y-4 rounded-lg border border-gray-200 bg-gray-50 p-4"
      data-testid="competitor-resolve-panel"
    >
      {showQuote && (
        <div className="space-y-3" data-testid="competitor-resolve-quote">
          <h4 className="text-sm font-medium">{t('resolve-title')}</h4>
          <p className="text-sm text-gray-600">
            {competitor.input_kind === 'facebook_page'
              ? t('resolve-intro-alias', {
                  alias: competitor.input_page_alias ?? '',
                })
              : t('resolve-intro-name')}
          </p>
          {quote.error && (
            <p className="text-sm text-red-700" role="alert">
              {t('resolve-quote-error')}
            </p>
          )}
          {!quote.data && !quote.error && (
            <p className="text-sm text-gray-500" role="status">
              {t('quote-loading')}
            </p>
          )}
          {quote.data && (
            <>
              {quote.data.available_modes.includes('byok') && (
                <ModeChoice
                  name={`resolve-mode-${competitor.competitor_id}`}
                  value={chosenMode ?? 'byok'}
                  onChange={setMode}
                  disabled={resolving}
                />
              )}
              {mockOnly && (
                <CredentialHint
                  messages={[
                    codeMessage(
                      t,
                      quote.data.byok_unavailable_code ??
                        'market_data_credential_missing',
                      quote.data.byok_unavailable_params
                    ),
                  ]}
                />
              )}
              <div
                className="rounded-lg border border-gray-200 bg-white p-3 text-sm"
                data-testid="competitor-resolve-cost"
                data-mode={quote.data.execution_mode}
              >
                <p className="flex flex-wrap items-center gap-2 font-medium">
                  {quote.data.execution_mode === 'mock' ? (
                    <>
                      <SimulatedBadge />
                      {t('resolve-cost-mock', {
                        estimate: quote.data.simulated_usd
                          ? formatUsd(i18n.language, quote.data.simulated_usd)
                          : t('quote-no-estimate'),
                      })}
                    </>
                  ) : (
                    t('resolve-cost', {
                      credits: formatCount(
                        i18n.language,
                        quote.data.max_credits
                      ),
                      usd: formatUsd(i18n.language, quote.data.max_usd),
                    })
                  )}
                </p>
                {(quote.data.lines ?? []).length > 0 && (
                  <ul className="mt-1 space-y-0.5 text-gray-600">
                    {(quote.data.lines ?? []).map((line) => (
                      <li key={line.operation}>
                        {t(
                          `resolve-line-${line.operation}`,
                          quoteLineCounts(t, i18n.language, line)
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                <p className="mt-1 text-xs text-gray-500">
                  {t('resolve-cost-note')}
                </p>
              </div>
              <button
                type="button"
                className={BUTTON}
                disabled={!!command.busy || resolving}
                onClick={() => void startResolve()}
              >
                {found ? t('resolve-again') : t('resolve-start')}
              </button>
            </>
          )}
        </div>
      )}

      {found && (
        <ResolutionView
          resolution={found}
          websiteDomain={competitor.website_domain}
          confirmedPageId={competitor.confirmation?.page_id ?? null}
          busy={!!command.busy}
          onConfirm={(page_id) =>
            void confirm({ resolve_job_id: found.resolve_job_id, page_id })
          }
        />
      )}
      {resolution.error && !found && (
        <p className="text-sm text-red-700" role="alert">
          {t('resolution-load-error')}
        </p>
      )}

      <div className="space-y-2">
        <label htmlFor={linkId} className="block text-sm font-medium">
          {t('confirm-by-link-label')}
        </label>
        <p className="text-xs text-gray-500">{t('confirm-by-link-hint')}</p>
        <div className="flex flex-col gap-2 sm:flex-row">
          <input
            id={linkId}
            className={INPUT}
            value={pageUrl}
            inputMode="url"
            placeholder="https://www.facebook.com/ads/library/?view_all_page_id=…"
            onChange={(e) => setPageUrl(e.target.value)}
          />
          <button
            type="button"
            className={SECONDARY}
            disabled={!!command.busy || !pageUrl.trim()}
            onClick={() => void confirmByLink()}
          >
            {t('confirm-by-link')}
          </button>
        </div>
      </div>

      {(command.errorFor('resolve:') ||
        command.errorFor('confirm:') ||
        command.errorFor('link:')) && (
        <p className="text-sm text-red-700" role="alert">
          {command.errorFor('resolve:') ||
            command.errorFor('confirm:') ||
            command.errorFor('link:')}
        </p>
      )}
      <button type="button" className="text-sm underline" onClick={onClose}>
        {t('close')}
      </button>
    </div>
  );
}

export function ModeChoice({
  name,
  value,
  onChange,
  disabled,
}: {
  name: string;
  value: ExecutionMode;
  onChange: (mode: ExecutionMode) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation('competitors');
  return (
    <fieldset className="space-y-1 text-sm" disabled={disabled}>
      <legend className="font-medium">{t('mode-label')}</legend>
      {(['byok', 'mock'] as const).map((option) => (
        <label key={option} className="flex items-start gap-2">
          <input
            type="radio"
            name={name}
            value={option}
            checked={value === option}
            onChange={() => onChange(option)}
            className="mt-1"
          />
          <span>
            {t(`mode-${option}`)}
            <span className="block text-xs text-gray-500">
              {t(`mode-${option}-hint`)}
            </span>
          </span>
        </label>
      ))}
    </fieldset>
  );
}

function ResolutionView({
  resolution,
  websiteDomain,
  confirmedPageId,
  busy,
  onConfirm,
}: {
  resolution: CompetitorResolution;
  websiteDomain?: string | null;
  confirmedPageId: string | null;
  busy: boolean;
  onConfirm: (pageId: string) => void;
}) {
  const { t, i18n } = useTranslation('competitors');
  const candidates = [...(resolution.candidates ?? [])].sort(
    (a, b) =>
      Number(!!a.excluded_reason) - Number(!!b.excluded_reason) ||
      a.rank - b.rank
  );
  const preferred =
    candidates.find((c) => c.page_id === confirmedPageId) ??
    candidates.find((c) => c.domain_hit === true) ??
    candidates[0];
  const [selected, setSelected] = useState<string | null>(null);
  const shown: CompetitorCandidate | undefined =
    candidates.find((c) => c.page_id === selected) ?? preferred;
  const index = shown ? candidates.indexOf(shown) : -1;

  return (
    <div className="space-y-3" data-testid="competitor-resolution">
      <p
        className="flex flex-wrap items-center gap-2 text-sm"
        role="status"
        data-testid="competitor-resolution-status"
        data-status={resolution.status}
      >
        {resolution.execution_mode === 'mock' && <SimulatedBadge />}
        <span>{t(`resolution-status-${resolution.status}`)}</span>
        {resolution.waits > 0 &&
          ACTIVE_STATUSES.includes(resolution.status) && (
            <span className="text-gray-600">{t('turn-waiting')}</span>
          )}
      </p>
      {resolution.failure && (
        <p className="text-sm text-red-700" role="alert">
          {codeMessage(t, resolution.failure.code, resolution.failure.params)}
        </p>
      )}
      {resolution.cost && (
        <p className="text-xs text-gray-500">
          {resolution.execution_mode === 'mock'
            ? t('resolution-cost-mock', { calls: resolution.cost.calls })
            : t('resolution-cost', {
                calls: resolution.cost.calls,
                credits: formatCount(
                  i18n.language,
                  resolution.cost.credits_charged
                ),
                usd: formatUsd(i18n.language, resolution.cost.usd),
              })}
        </p>
      )}
      {resolution.auto_confirmed && (
        <p className="text-sm font-medium text-green-700">
          {t('resolution-auto-confirmed')}
        </p>
      )}
      {resolution.reusable && resolution.expires_at && (
        <p className="text-xs text-gray-500">
          {t('resolution-reusable-until', {
            time: formatDateTime(i18n.language, resolution.expires_at, {
              dateStyle: 'medium',
              timeStyle: 'short',
            }),
          })}
        </p>
      )}
      {!ACTIVE_STATUSES.includes(resolution.status) &&
        candidates.length === 0 && (
          <p className="text-sm text-gray-600">
            {t('resolution-no-candidates')}
          </p>
        )}
      {candidates.length > 0 && shown && (
        <>
          <div
            className="flex flex-wrap gap-2"
            role="group"
            aria-label={t('candidates-label')}
          >
            {candidates.map((candidate, i) => (
              <button
                key={candidate.page_id}
                type="button"
                aria-pressed={candidate === shown}
                className={`max-w-full truncate rounded-full border px-3 py-1 text-xs ${
                  candidate === shown
                    ? 'border-gray-900 bg-gray-900 text-white'
                    : 'border-gray-300 bg-white'
                } ${candidate.excluded_reason ? 'opacity-60' : ''}`}
                onClick={() => setSelected(candidate.page_id)}
              >
                {candidate.page_name || candidate.page_alias || `#${i + 1}`}
                {candidate.domain_hit === true ? ' ✓' : ''}
              </button>
            ))}
          </div>
          <div className="flex items-center justify-between gap-2 text-sm text-gray-600">
            <button
              type="button"
              className="underline disabled:no-underline disabled:opacity-40"
              disabled={index <= 0}
              onClick={() => setSelected(candidates[index - 1].page_id)}
            >
              {t('candidate-previous')}
            </button>
            <span>
              {t('candidate-position', {
                index: index + 1,
                total: candidates.length,
              })}
            </span>
            <button
              type="button"
              className="underline disabled:no-underline disabled:opacity-40"
              disabled={index >= candidates.length - 1}
              onClick={() => setSelected(candidates[index + 1].page_id)}
            >
              {t('candidate-next')}
            </button>
          </div>
          <CandidateCard candidate={shown} websiteDomain={websiteDomain} />
          {shown.page_id === confirmedPageId ? (
            <p className="text-sm font-medium text-green-700">
              {t('candidate-is-confirmed')}
            </p>
          ) : (
            <button
              type="button"
              className={BUTTON}
              disabled={
                busy ||
                !resolution.reusable ||
                shown.excluded_reason === 'deleted'
              }
              onClick={() => onConfirm(shown.page_id)}
            >
              {t('candidate-confirm')}
            </button>
          )}
          {!resolution.reusable && (
            <p className="text-xs text-gray-500">{t('resolution-expired')}</p>
          )}
        </>
      )}
    </div>
  );
}
