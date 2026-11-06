import {
  DomainProblemError,
  type DomainProblem,
} from '@/lib/i18n/domain-errors';
import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import useSWR from 'swr';
import { MarketingCallRecovery } from './MarketingCallRecovery';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useWorkspace } from './WorkspaceProvider';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { strategyLanguageFor } from '@/lib/i18n/locales';

type Feedback = components['schemas']['PrepareFeedback'];
type Explain = components['schemas']['RequestExplanation'];
type Call = components['schemas']['WorkflowExplanation'];
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm';
const BUTTON =
  'rounded-lg border border-gray-300 px-4 py-2 text-sm disabled:opacity-40';
function dataOf<T>(result: {
  kind: string;
  data?: T;
  problem?: DomainProblem;
}): T {
  if (result.kind === 'problem') throw new DomainProblemError(result.problem);
  if (result.data === undefined) throw new Error('marketing-action-failed');
  return result.data;
}

export function MarketingFeedbackForm({
  assetVersionId,
  mode,
  busy,
  onPrepare,
}: {
  assetVersionId: string;
  mode: 'mock' | 'byok';
  busy: boolean;
  onPrepare: (body: Feedback) => void;
}) {
  const { t } = useTranslation('common');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState('');
  const [chosen, setChosen] = useState('');
  const caps = useSWR(
    open ? ['marketing-generation-capabilities', current.workspaceId] : null,
    async () =>
      dataOf(
        await gateway.getGenerationCapabilities({ workspaceSlug: current.slug })
      )
  );
  const options = (caps.data?.provider_options || []).filter(
    (p) => p.available && p.execution_mode === mode
  );
  const provider = options.find((p) => p.id === chosen) || options[0];
  return (
    <details
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
      className="space-y-3 border-l border-gray-200 pl-4"
    >
      <summary className="cursor-pointer text-sm font-medium">
        {t('marketing-feedback-title')}
      </summary>
      <p className="text-sm text-gray-600">{t('marketing-feedback-note')}</p>
      <label className="block space-y-1 text-sm">
        <span>{t('marketing-feedback-adjust')}</span>
        <textarea
          className={INPUT}
          value={note}
          maxLength={1200}
          onChange={(e) => setNote(e.target.value)}
        />
      </label>
      <label className="block space-y-1 text-sm">
        <span>{t('marketing-feedback-provider')}</span>
        <select
          className={INPUT}
          value={provider?.id || ''}
          onChange={(e) => setChosen(e.target.value)}
        >
          {options.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
      </label>
      {caps.error && <p role="alert">{t('marketing-action-failed')}</p>}
      <button
        className={BUTTON}
        disabled={busy || !provider}
        onClick={() =>
          provider &&
          onPrepare({
            asset_version_id: assetVersionId,
            note,
            provider: provider.id as Feedback['provider'],
            model: provider.default_model,
          })
        }
      >
        {t('marketing-feedback-prepare')}
      </button>
    </details>
  );
}

export function MarketingExplanationPanel({
  call,
  mode,
  busy,
  canRequest,
  onRequest,
  onResolved,
}: {
  call?: Call | null;
  mode: 'mock' | 'byok';
  busy: boolean;
  canRequest: boolean;
  onRequest: (body: Explain) => void;
  onResolved?: () => void;
}) {
  const { t, i18n } = useTranslation('common');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  // Written in the plan's strategy language; a plan without one uses the interface's (J-43).
  const language = strategyLanguageFor(i18n?.language);
  const [acceptedQuote, setAcceptedQuote] = useState('');
  const caps = useSWR(
    canRequest ? ['marketing-capabilities', current.workspaceId] : null,
    async () =>
      dataOf(
        await gateway.getMarketingCapabilities({ workspaceSlug: current.slug })
      )
  );
  const quote = caps.data?.paid;
  const pending =
    call && ['queued', 'running', 'uncertain'].includes(call.state);
  return (
    <section
      className="space-y-3 border-l-2 border-gray-900 pl-4"
      aria-label={t('marketing-explanation-title')}
    >
      <h3 className="font-semibold">{t('marketing-explanation-title')}</h3>
      <p className="text-sm text-gray-600">
        {t('marketing-explanation-boundary')}
      </p>
      {call && (
        <p role="status">{t(`marketing-explanation-state-${call.state}`)}</p>
      )}
      {call && !call.current && (
        <p role="note">{t('marketing-explanation-stale')}</p>
      )}
      {call?.state === 'uncertain' && (
        <MarketingCallRecovery
          key={call.id}
          call={call}
          kind="planning"
          onResolved={() => {
            onResolved?.();
          }}
        />
      )}
      {call?.current && call.data && (
        <div className="space-y-4">
          {/* Shown as recorded, never translated (J-43). */}
          {call.language && call.language !== language && (
            <p role="note" className="text-xs text-gray-500">
              {t(`marketing-ai-output-language-${call.language}`)}
            </p>
          )}
          {call.execution_mode === 'mock' && (
            <p className="text-sm text-amber-800">
              {t('marketing-explanation-mock')}
            </p>
          )}
          {call.data.paragraphs.map((p) => (
            <article key={p.evidence_id} className="space-y-1 text-sm">
              <p className="font-medium">{p.observation}</p>
              <p>{p.explanation}</p>
            </article>
          ))}
          <p className="text-xs text-gray-500">
            {t('marketing-explanation-source', {
              source: call.data.evidence_status,
            })}
          </p>
        </div>
      )}
      {canRequest && (!call?.current || call.state !== 'succeeded') && (
        <>
          {mode === 'byok' && quote?.available && (
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={acceptedQuote === quote.quote_hash}
                onChange={(e) =>
                  setAcceptedQuote(e.target.checked ? quote.quote_hash : '')
                }
              />
              <span>
                {t('marketing-explanation-cost', {
                  amount: quote.maximum_cost,
                  provider: quote.provider,
                  model: quote.model,
                })}
              </span>
            </label>
          )}
          {mode === 'byok' && (!quote?.available || caps.error) && (
            <p role="note">{t('marketing-explanation-unavailable')}</p>
          )}
          <button
            className={BUTTON}
            disabled={
              busy ||
              !!pending ||
              (mode === 'byok' &&
                (!quote?.available || acceptedQuote !== quote.quote_hash))
            }
            onClick={() =>
              onRequest({
                execution_mode: mode,
                quote_hash: mode === 'byok' ? quote?.quote_hash || '' : '',
                strategy_language: language,
              })
            }
          >
            {t(
              mode === 'mock'
                ? 'marketing-explanation-preview-mock'
                : 'marketing-explanation-request'
            )}
          </button>
        </>
      )}
    </section>
  );
}
