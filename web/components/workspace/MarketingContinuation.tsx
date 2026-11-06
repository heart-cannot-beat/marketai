import {
  DomainProblemError,
  errorMessageOf,
  type DomainProblem,
} from '@/lib/i18n/domain-errors';
import { useEffect, useId, useState } from 'react';
import useSWR from 'swr';
import { useTranslation } from 'next-i18next';
import { strategyLanguageFor } from '@/lib/i18n/locales';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';
import { useWorkspace } from './WorkspaceProvider';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { MarketingRound } from './MarketingExecution';

type PlanRequest = components['schemas']['app__marketing__api__PlanRequest'];
type Plan = components['schemas']['FrozenTestPlan'];
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm';
const BUTTON =
  'rounded-lg border border-gray-300 px-4 py-2 text-sm disabled:opacity-40';
function dataOf<T>(result: {
  kind: string;
  data?: T;
  problem?: DomainProblem;
}): T {
  if (result.data === undefined) throw new DomainProblemError(result.problem);
  return result.data;
}

export function MarketingNextPlan({
  row,
  inferredAdLanguage,
  busy,
  onRequest,
}: {
  row: MarketingRound;
  /** The language the project read infers for a plan without one. */
  inferredAdLanguage?: string | null;
  busy: boolean;
  onRequest: (body: PlanRequest) => Promise<void>;
}) {
  const { t, i18n } = useTranslation('common');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const languageId = useId();
  // A round frozen before J-02 has no advertising language to carry over:
  // the next plan asks for one instead of drafting with an unseen inference.
  const choosesLanguage = !row.snapshot.plan.ad_language;
  const [adLanguage, setAdLanguage] = useState(
    row.snapshot.plan.ad_language || inferredAdLanguage || 'en-US'
  );
  const evaluation = row.evaluation?.current === false ? null : row.evaluation;
  const recommended: NonNullable<PlanRequest['direction']> =
    evaluation?.next_action === 'match_expressions_for_candidates'
      ? 'match_expressions'
      : evaluation?.qualified_asset_version_ids.length
        ? 'confirm_combination'
        : 'screen_angles';
  const [direction, setDirection] =
    useState<NonNullable<PlanRequest['direction']>>(recommended);
  const [note, setNote] = useState('');
  const [quoteAck, setQuoteAck] = useState('');
  const [requesting, setRequesting] = useState(false);
  const [error, setError] = useState('');
  const caps = useSWR(
    ['marketing-capabilities', current.workspaceId],
    async () =>
      dataOf(
        await gateway.getMarketingCapabilities({ workspaceSlug: current.slug })
      )
  );
  const quote = caps.data?.paid;
  const canPay =
    row.execution_mode === 'mock' ||
    (!!quote?.available && quoteAck === quote.quote_hash);
  return (
    <section
      className="space-y-3 border-l-2 border-gray-900 pl-4"
      aria-label={t('marketing-next-plan-title')}
    >
      <h3 className="font-semibold">{t('marketing-next-plan-title')}</h3>
      <p className="text-sm">{t('marketing-next-plan-boundary')}</p>
      {(row.continuation || evaluation) && (
        <p className="text-sm">
          {t('marketing-next-budget', {
            remaining:
              row.continuation?.remaining_budget ??
              Math.max(
                0,
                Number(evaluation?.big_round_budget) -
                  Number(evaluation?.big_round_spend)
              ).toFixed(2),
          })}
        </p>
      )}
      <label className="block space-y-1 text-sm">
        <span>{t('marketing-next-direction')}</span>
        <select
          className={INPUT}
          value={direction}
          onChange={(e) => setDirection(e.target.value as typeof direction)}
        >
          {(
            [
              'screen_angles',
              'match_expressions',
              'confirm_combination',
              'optimize_elements',
            ] as const
          ).map((stage) => (
            <option key={stage} value={stage}>
              {t(`marketing-stage-${stage}`)}
              {stage === recommended ? ' · ' + t('marketing-recommended') : ''}
            </option>
          ))}
        </select>
      </label>
      {choosesLanguage && (
        <div className="space-y-1 text-sm">
          <label className="block space-y-1">
            <span>{t('marketing-ad-language')}</span>
            <select
              className={INPUT}
              value={adLanguage}
              aria-describedby={languageId}
              onChange={(e) => setAdLanguage(e.target.value)}
            >
              <option value="en-US">{t('marketing-ad-language-en-US')}</option>
              <option value="zh-CN">{t('marketing-ad-language-zh-CN')}</option>
              {!['en-US', 'zh-CN'].includes(adLanguage) && (
                <option value={adLanguage}>{adLanguage}</option>
              )}
            </select>
          </label>
          <p id={languageId} className="text-gray-600">
            {t('marketing-next-plan-ad-language')}
          </p>
        </div>
      )}
      <label className="block space-y-1 text-sm">
        <span>{t('marketing-next-question')}</span>
        <textarea
          className={INPUT}
          maxLength={1200}
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      </label>
      {row.execution_mode === 'byok' && quote?.available && (
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={quoteAck === quote.quote_hash}
            onChange={(e) =>
              setQuoteAck(e.target.checked ? quote.quote_hash : '')
            }
          />
          <span>
            {t('marketing-next-plan-cost', {
              amount: quote.maximum_cost,
              provider: quote.provider,
              model: quote.model,
            })}
          </span>
        </label>
      )}
      {row.execution_mode === 'byok' && (!quote?.available || caps.error) && (
        <p>{t('marketing-explanation-unavailable')}</p>
      )}
      {error && <p role="alert">{error}</p>}
      <button
        className={BUTTON}
        disabled={
          busy ||
          requesting ||
          !canPay ||
          (direction === 'optimize_elements' && !note.trim())
        }
        onClick={() => {
          setRequesting(true);
          setError('');
          void onRequest({
            kind: 'plan',
            ad_language: adLanguage,
            // The next round is drafted in the interface language too.
            strategy_language: strategyLanguageFor(i18n?.language),
            direction,
            decision_question: note,
            execution_mode: row.execution_mode,
            quote_hash:
              row.execution_mode === 'byok' ? quote?.quote_hash || '' : '',
          })
            .catch((e) => setError(errorMessageOf(t, e)))
            .finally(() => setRequesting(false));
        }}
      >
        {t('marketing-next-plan-draft')}
      </button>
    </section>
  );
}

export function MarketingReusePicker({
  row,
  productId,
  busy,
  onAttach,
}: {
  row: MarketingRound;
  productId: string | null;
  busy: boolean;
  onAttach: (body: components['schemas']['ReuseCreative']) => Promise<void>;
}) {
  const { t } = useTranslation('common');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const blobs = useBlobTransferClient();
  const [open, setOpen] = useState(false);
  const [cursor, setCursor] = useState<string | undefined>();
  const [selected, setSelected] = useState('');
  const [combination, setCombination] = useState(
    row.snapshot.plan.combinations[0]?.id || ''
  );
  const [imageUrl, setImageUrl] = useState('');
  const [error, setError] = useState('');
  const page = useSWR(
    open && productId
      ? [
          'marketing-reuse-assets',
          current.workspaceId,
          productId,
          row.execution_mode,
          cursor,
        ]
      : null,
    async () =>
      dataOf(
        await gateway.listGenerationAssets({
          workspaceSlug: current.slug,
          productId: productId!,
          executionMode: row.execution_mode,
          cursor,
          limit: 20,
        })
      )
  );
  const options = (page.data?.items || []).filter(
    (a) => !row.assets.some((existing) => existing.asset_version_id === a.id)
  );
  const previewFailure = t('marketing-reuse-preview-failed');
  useEffect(() => {
    setImageUrl('');
    setError('');
    if (!selected) return;
    const controller = new AbortController();
    let url = '';
    void (async () => {
      const intent = dataOf(
        await gateway.createGenerationAssetVersionDownloadIntent({
          workspaceSlug: current.slug,
          versionId: selected,
          actionInstanceId: crypto.randomUUID(),
        })
      );
      if (!intent.download_grant || !intent.expires_at)
        throw new Error('download-unavailable');
      const image = await blobs.downloadBlob(
        { ...intent.download_grant, expires_at: intent.expires_at },
        { signal: controller.signal }
      );
      if (controller.signal.aborted) return;
      url = URL.createObjectURL(image);
      setImageUrl(url);
    })().catch(() => {
      if (!controller.signal.aborted) setError(previewFailure);
    });
    return () => {
      controller.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [selected, current.slug, gateway, blobs, previewFailure]);
  return (
    <details
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
      className="space-y-3 border-l border-gray-200 pl-4"
    >
      <summary className="cursor-pointer text-sm font-medium">
        {t('marketing-reuse-title')}
      </summary>
      <p className="text-sm">{t('marketing-reuse-boundary')}</p>
      {page.error && <p role="alert">{t('marketing-workflow-load-failed')}</p>}
      {!options.length && page.data && <p>{t('marketing-reuse-empty')}</p>}
      <label className="block space-y-1 text-sm">
        <span>{t('marketing-reuse-creative')}</span>
        <select
          className={INPUT}
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
        >
          <option value="">{t('marketing-reuse-select')}</option>
          {options.map((a, index) => (
            <option key={a.id} value={a.id}>
              {t('marketing-reuse-option', {
                number: index + 1,
                date: a.created_at?.slice(0, 10) || '',
                provider: a.provider,
                model: a.model,
                shape: a.aspect_ratio,
              })}
            </option>
          ))}
        </select>
      </label>
      {imageUrl && (
        // Private exact image uses the scoped blob transfer, not a public optimizer.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={imageUrl}
          alt={t('marketing-reuse-preview')}
          className="max-h-72 max-w-full rounded-lg object-contain"
        />
      )}
      {error && <p role="alert">{error}</p>}
      {page.data?.next_cursor && (
        <button
          className={BUTTON}
          onClick={() => {
            setSelected('');
            setCursor(page.data!.next_cursor!);
          }}
        >
          {t('marketing-reuse-more')}
        </button>
      )}
      {cursor && (
        <button
          className={BUTTON}
          onClick={() => {
            setSelected('');
            setCursor(undefined);
          }}
        >
          {t('marketing-reuse-first')}
        </button>
      )}
      <label className="block space-y-1 text-sm">
        <span>{t('marketing-reuse-combination')}</span>
        <select
          className={INPUT}
          value={combination}
          onChange={(e) => setCombination(e.target.value)}
        >
          {row.snapshot.plan.combinations.map((c) => (
            <option key={c.id} value={c.id}>
              {
                row.snapshot.plan.angles.find((a) => a.id === c.angle_id)
                  ?.proposition
              }{' '}
              ·{' '}
              {
                row.snapshot.plan.angles
                  .find((a) => a.id === c.angle_id)
                  ?.expressions.find((e) => e.id === c.expression_id)?.name
              }{' '}
              · {c.headline}
            </option>
          ))}
        </select>
      </label>
      <button
        className={BUTTON}
        disabled={busy || !selected || !imageUrl || !combination}
        onClick={() => {
          void onAttach({
            asset_version_id: selected,
            combination_id: combination,
          })
            .then(() => setSelected(''))
            .catch(() => setError(t('marketing-action-failed')));
        }}
      >
        {t('marketing-reuse-attach')}
      </button>
    </details>
  );
}

export function MarketingComparisonView({ plan }: { plan: Plan }) {
  const { t } = useTranslation('common');
  const expressions = Array.from(
    new Map(
      plan.angles.flatMap((a) => a.expressions).map((e) => [e.id, e])
    ).values()
  );
  return (
    <details className="space-y-3">
      <summary className="cursor-pointer text-sm font-medium">
        {t('marketing-comparison-view')}
      </summary>
      <p className="text-sm">{t('marketing-comparison-view-boundary')}</p>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">
            {t('marketing-comparison-view')}
          </caption>
          <thead>
            <tr>
              <th className="p-2">{t('marketing-stage-match_expressions')}</th>
              {plan.angles.map((a) => (
                <th key={a.id} className="p-2">
                  {a.proposition}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {expressions.map((e) => (
              <tr key={e.id}>
                <th className="p-2">{e.name}</th>
                {plan.angles.map((candidate) => {
                  const matches = plan.combinations.filter(
                    (c) =>
                      c.angle_id === candidate.id && c.expression_id === e.id
                  );
                  return (
                    <td key={candidate.id} className="p-2">
                      {matches.length
                        ? matches.map((c) => c.headline).join(' / ')
                        : t('marketing-unplanned-pairing')}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
