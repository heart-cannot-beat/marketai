import { DomainProblemError, errorMessageOf } from '@/lib/i18n/domain-errors';
import { useRef, useState } from 'react';
import Link from 'next/link';
import useSWR from 'swr';
import { useTranslation } from 'next-i18next';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useWorkspace } from './WorkspaceProvider';
import { useEmphasis } from './marketing-focus';

import type { components } from '@/lib/domain-client/generated/domain-v1';
type Options = components['schemas']['EvidenceHandoffOptions'];

export function MarketingEvidenceHandoff({
  roundId,
  revision,
  evaluationId,
}: {
  roundId: string;
  revision: number;
  evaluationId: string;
}) {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const { t } = useTranslation('common');
  const emphasis = useEmphasis();
  const [assetId, setAssetId] = useState('');
  const [offerId, setOfferId] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef<{ identity: string; action: string } | null>(null);
  const options = useSWR<Options>(
    [
      'marketing-evidence-handoff',
      current.workspaceId,
      roundId,
      revision,
      evaluationId,
    ],
    async () => {
      const result = await gateway.getMarketingEvidenceHandoff({
        workspaceSlug: current.slug,
        roundId,
      });
      if (result.kind !== 'ok')
        throw 'problem' in result
          ? new DomainProblemError(result.problem)
          : new Error('marketing-handoff-load-error');
      return result.data.data;
    }
  );
  const selected = options.data?.assets.find(
    (a) => a.asset_version_id === assetId
  );
  const offer = options.data?.offers.find((o) => o.id === offerId);
  async function create() {
    if (!selected || !offer || !confirmed || !note.trim() || busy) return;
    const body = {
      evaluation_id: evaluationId,
      asset_version_id: assetId,
      offer_version_id: offerId,
      confirms_tested_context: confirmed,
      note,
    };
    const identity = JSON.stringify(body);
    if (pending.current?.identity !== identity)
      pending.current = { identity, action: crypto.randomUUID() };
    setBusy(true);
    setError('');
    try {
      const result = await gateway.createMarketingEvidenceHandoff({
        workspaceSlug: current.slug,
        roundId,
        expectedRevision: revision,
        actionInstanceId: pending.current.action,
        body,
      });
      if (result.kind !== 'created')
        throw 'problem' in result
          ? new DomainProblemError(result.problem)
          : new Error('marketing-handoff-reconcile');
      pending.current = null;
      setConfirmed(false);
      await options.mutate();
    } catch (e) {
      setError(errorMessageOf(t, e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="min-w-0 space-y-3 rounded-lg border border-gray-200 p-4 text-sm"
      aria-label={t('marketing-handoff-title')}
    >
      <h3 className="font-semibold">{t('marketing-handoff-title')}</h3>
      <p>{t('marketing-handoff-boundary')}</p>
      {options.isLoading && (
        <p role="status">{t('marketing-handoff-loading')}</p>
      )}
      {(error || options.error) && (
        <p role="alert" className="text-red-700">
          {error || errorMessageOf(t, options.error)}
        </p>
      )}
      {options.data && !options.data.assets.length && (
        <p>{t('marketing-handoff-not-qualified')}</p>
      )}
      {!!options.data?.assets.length && (
        <>
          <label className="block">
            {t('marketing-handoff-asset')}
            <select
              className="mt-1 w-full min-w-0 rounded border p-2"
              disabled={busy}
              value={assetId}
              onChange={(e) => {
                setAssetId(e.target.value);
                setConfirmed(false);
              }}
            >
              <option value="">{t('marketing-handoff-choose-asset')}</option>
              {options.data.assets.map((a) => (
                <option key={a.asset_version_id} value={a.asset_version_id}>
                  {a.label}
                </option>
              ))}
            </select>
          </label>
          {selected && (
            <p>
              {t(
                selected.scale
                  ? 'marketing-handoff-combination-scope'
                  : 'marketing-handoff-asset-scope'
              )}
            </p>
          )}
          <label className="block">
            {t('marketing-handoff-offer')}
            <select
              className="mt-1 w-full min-w-0 rounded border p-2"
              value={offerId}
              disabled={busy}
              onChange={(e) => {
                setOfferId(e.target.value);
                setConfirmed(false);
              }}
            >
              <option value="">{t('marketing-handoff-choose-offer')}</option>
              {options.data.offers.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.product} · {o.label} · {o.context.market} /{' '}
                  {o.context.placement}
                </option>
              ))}
            </select>
          </label>
          {!options.data.offers.length && (
            <p>
              {t('marketing-handoff-no-offer')}{' '}
              <Link
                className="underline"
                href={`/w/${current.slug}/library?cat=product`}
              >
                {t('marketing-handoff-configure-offer')}
              </Link>
            </p>
          )}
          {offer && (
            <div className="space-y-1 break-words border-l-2 pl-3">
              <p>
                {offer.product} · {offer.label}
              </p>
              <p>{Object.values(offer.context).join(' · ')}</p>
              <p>{offer.landing_page}</p>
              <p>
                {[offer.item?.sku, offer.item?.variant_label]
                  .filter(Boolean)
                  .map(String)
                  .join(' · ')}
                {offer.item?.quantity
                  ? ` × ${String(offer.item.quantity)}`
                  : ''}
              </p>
            </div>
          )}
          <label className="block">
            {t('marketing-handoff-note')}
            <textarea
              className="mt-1 w-full rounded border p-2"
              disabled={busy}
              maxLength={2000}
              value={note}
              onChange={(e) => {
                setNote(e.target.value);
                setConfirmed(false);
              }}
            />
          </label>
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={busy}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            {t('marketing-handoff-confirm')}
          </label>
          <button
            className={emphasis(
              'evidence_handoff',
              'rounded bg-gray-900 px-4 py-2 text-white disabled:opacity-40',
              'rounded border border-gray-300 px-4 py-2 disabled:opacity-40'
            )}
            disabled={busy || !selected || !offer || !confirmed || !note.trim()}
            onClick={create}
          >
            {t('marketing-handoff-create')}
          </button>
        </>
      )}
      {options.data?.existing.map((e) => (
        <div key={e.decision_id} className="flex flex-wrap gap-3 border-t pt-3">
          <p>
            {t(
              e.current
                ? 'marketing-handoff-current'
                : 'marketing-handoff-stale'
            )}
          </p>
          {e.current && (
            <>
              <Link
                className="underline"
                href={`/w/${current.slug}/localization?source=${e.decision_id}`}
              >
                {t('marketing-handoff-open-localization')}
              </Link>
              {e.scope === 'repeatable_combination' && (
                <Link
                  className="underline"
                  href={`/w/${current.slug}/scale?source=${e.decision_id}`}
                >
                  {t('marketing-handoff-open-scale')}
                </Link>
              )}
            </>
          )}
        </div>
      ))}
    </section>
  );
}
