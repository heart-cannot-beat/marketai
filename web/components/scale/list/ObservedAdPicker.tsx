import { useEffect, useState } from 'react';
import { useTranslation } from 'next-i18next';
import {
  useCreateScaleDeliveryTarget,
  useScaleObservedAds,
} from '@/hooks/scale/useScaleObservedAds';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';
import { errorMessage } from '@/lib/i18n/domain-errors';

export interface PickedDeliveryTarget {
  id: string;
  market_code: string;
  placement: string;
  ad_remote_id: string;
}

interface ObservedAd {
  meta_connection_id: string;
  ad_account_id: string;
  campaign_remote_id: string;
  adset_remote_id: string;
  ad_remote_id: string;
  ad_creative_id: string;
  source_type: string;
  asset_version_ids: string[];
}

interface ObservedAdPickerProps {
  metaMode: ScaleMetaMode;
  winnerDecisionId: string;
  onPicked: (target: PickedDeliveryTarget) => void;
}

/**
 * Pick the observed ad a Winner runs in (#129). The list is what the
 * Workspace's connections in this mode have observed; picking one asks the
 * server to make the DeliveryTarget from recorded evidence. When the ad does
 * not match the Winner, the server's reason is shown and nothing is created.
 * Nothing here reads from or writes to Meta.
 */
export function ObservedAdPicker({
  metaMode,
  winnerDecisionId,
  onPicked,
}: ObservedAdPickerProps) {
  const { t } = useTranslation('scale');
  const {
    data,
    problem: listProblem,
    isLoading,
  } = useScaleObservedAds(metaMode);
  const createTarget = useCreateScaleDeliveryTarget();
  const [picked, setPicked] = useState<PickedDeliveryTarget | null>(null);
  const [pickingAd, setPickingAd] = useState<string | null>(null);

  useEffect(() => {
    setPicked(null);
    createTarget.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [winnerDecisionId]);

  const ads: ObservedAd[] = data?.items ?? [];
  const unavailable = data?.unavailable ?? [];

  const pick = async (ad: ObservedAd) => {
    setPickingAd(ad.ad_remote_id);
    const outcome = await createTarget.run({
      metaMode,
      winnerDecisionId,
      metaConnectionId: ad.meta_connection_id,
      adRemoteId: ad.ad_remote_id,
    });
    setPickingAd(null);
    if (outcome.kind !== 'success') return;
    const target: PickedDeliveryTarget = {
      id: outcome.data.id,
      market_code: outcome.data.market_code,
      placement: outcome.data.placement,
      ad_remote_id: outcome.data.ad_remote_id,
    };
    setPicked(target);
    onPicked(target);
  };

  return (
    <section className="mt-4" aria-labelledby="observed-ads-title">
      <h4 id="observed-ads-title" className="text-sm font-semibold">
        {t('candidate-selector.observed-ads-title')}
      </h4>
      <p className="mt-1 text-xs text-base-content/70">
        {t('candidate-selector.observed-ads-description')}
      </p>

      {listProblem && (
        <div className="mt-2 text-sm text-error" role="alert">
          {errorMessage(t, listProblem)}
        </div>
      )}
      {isLoading && (
        <div className="mt-2 flex justify-center" aria-busy="true">
          <span className="loading loading-spinner loading-sm" />
        </div>
      )}
      {unavailable.map((item) => (
        <p
          key={item.meta_connection_id}
          className="mt-2 text-xs text-base-content/70"
        >
          {errorMessage(t, { code: item.reason })}
        </p>
      ))}
      {!isLoading && !listProblem && ads.length === 0 && (
        <p className="mt-2 text-xs text-base-content/70">
          {t('candidate-selector.observed-ads-empty')}
        </p>
      )}

      <ul className="mt-2 flex max-h-48 flex-col gap-2 overflow-y-auto">
        {ads.map((ad) => (
          <li
            key={`${ad.meta_connection_id}:${ad.ad_remote_id}`}
            className="flex min-w-0 items-center justify-between gap-2 rounded border border-base-300 p-2"
          >
            <span className="flex min-w-0 flex-col text-xs">
              <span className="truncate">
                {t('candidate-selector.observed-ad-label', {
                  ad: ad.ad_remote_id,
                  adset: ad.adset_remote_id,
                })}
              </span>
              <span className="truncate opacity-70">
                {ad.asset_version_ids.length > 0
                  ? t('candidate-selector.observed-ad-assets', {
                      ids: ad.asset_version_ids.join(', '),
                    })
                  : t('candidate-selector.observed-ad-unmapped')}
              </span>
            </span>
            <button
              type="button"
              className="btn btn-outline btn-xs shrink-0"
              disabled={createTarget.isPending}
              aria-label={t('candidate-selector.use-observed-ad', {
                ad: ad.ad_remote_id,
              })}
              onClick={() => void pick(ad)}
            >
              {pickingAd === ad.ad_remote_id
                ? t('candidate-selector.observed-ad-picking')
                : t('candidate-selector.use-observed-ad-short')}
            </button>
          </li>
        ))}
      </ul>

      {createTarget.problem && (
        <div className="mt-2 text-sm text-error" role="alert">
          {errorMessage(t, createTarget.problem)}
        </div>
      )}
      {picked && !createTarget.problem && (
        <p className="mt-2 text-xs text-success" role="status">
          {t('candidate-selector.observed-ad-picked', {
            market: picked.market_code,
            placement: picked.placement,
            ad: picked.ad_remote_id,
          })}
        </p>
      )}
    </section>
  );
}
