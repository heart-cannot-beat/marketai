import { useEffect, useState } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { CheckCircleIcon } from '@heroicons/react/24/solid';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { AssetThumbnailLink } from '@/hooks/assets/useAssetImageUrl';
import { AssetThumb } from './AssetThumb';

export const MAX_SEEDS = 10;

interface SeedAsset {
  id: string;
  thumbnail?: AssetThumbnailLink | null;
  origin_kind?: string | null;
}

interface ExternalSeed {
  asset_version_id?: string | null;
  thumbnail?: AssetThumbnailLink | null;
}

/** Approved assets first, then confirmed external creatives not yet among
 * them (an external creative confirmed without approval, Issue #168). */
export function mergeSeeds(
  approved: SeedAsset[],
  external: ExternalSeed[]
): SeedAsset[] {
  const seen = new Set(approved.map((item) => item.id));
  const merged = [...approved];
  for (const item of external) {
    if (item.asset_version_id && !seen.has(item.asset_version_id)) {
      seen.add(item.asset_version_id);
      merged.push({
        id: item.asset_version_id,
        thumbnail: item.thumbnail ?? null,
        origin_kind: 'upload',
      });
    }
  }
  return merged;
}

interface SeedAssetPickerProps {
  productId: string;
  value: string[];
  onChange: (ids: string[]) => void;
  disabled?: boolean;
}

/**
 * "优化已有广告": this product's Approved assets as tiles to tick (at most
 * ten). Winners are Approved assets too; a Winner that was never reviewed
 * is reachable from the Experiments area, not listed here. External
 * creatives the user confirmed (Issue #168) are listed alongside, marked
 * 「外部」.
 */
export function SeedAssetPicker({
  productId,
  value,
  onChange,
  disabled = false,
}: SeedAssetPickerProps) {
  const { t } = useTranslation('tests');
  const { t: tx } = useTranslation('external');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const [assets, setAssets] = useState<SeedAsset[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    const external = async () => {
      const result = await gateway.listExternalCreatives({
        workspaceSlug: current.slug,
        productId,
        status: 'confirmed',
      });
      return result.kind === 'ok'
        ? (((result.data as { items?: unknown[] }).items ??
            []) as ExternalSeed[])
        : [];
    };
    Promise.all([
      gateway.listGenerationAssets({
        workspaceSlug: current.slug,
        productId,
        reviewValue: 'approved',
        limit: 48,
      }),
      // An unreachable external list only hides external seeds.
      external().catch(() => [] as ExternalSeed[]),
    ])
      .then(([result, externalItems]) => {
        if (cancelled) return;
        if (result.kind === 'ok') {
          const items = ((result.data as { items?: unknown[] }).items ??
            []) as SeedAsset[];
          setAssets(mergeSeeds(items, externalItems));
        } else {
          setFailed(true);
        }
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [attempt, current.slug, gateway, productId]);

  if (failed) {
    return (
      <div className="flex items-center gap-2 text-sm text-error" role="alert">
        <span>{t('q-seeds-error')}</span>
        <button
          type="button"
          className="btn btn-xs"
          onClick={() => setAttempt((n) => n + 1)}
        >
          {t('retry')}
        </button>
      </div>
    );
  }
  if (assets === null) {
    return (
      <p className="text-sm text-base-content/60" role="status">
        {t('q-seeds-loading')}
      </p>
    );
  }
  if (assets.length === 0) {
    return (
      <p className="text-sm text-base-content/70" data-testid="seed-empty">
        {t('q-seeds-empty')}
      </p>
    );
  }

  function toggle(id: string) {
    if (value.includes(id)) onChange(value.filter((item) => item !== id));
    else if (value.length < MAX_SEEDS) onChange([...value, id]);
  }

  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <p className="text-sm font-medium">{t('q-seeds-label')}</p>
        <p className="text-xs text-base-content/60" aria-live="polite">
          {t('q-seeds-count', { count: value.length })}
        </p>
      </div>
      <ul className="grid grid-cols-3 gap-2 sm:grid-cols-5">
        {assets.map((asset, index) => {
          const checked = value.includes(asset.id);
          const blocked = !checked && value.length >= MAX_SEEDS;
          return (
            <li key={asset.id}>
              <label
                className={classNames(
                  'relative block cursor-pointer overflow-hidden rounded-lg border-2',
                  checked ? 'border-primary' : 'border-transparent',
                  (disabled || blocked) && 'cursor-not-allowed opacity-50'
                )}
              >
                <input
                  type="checkbox"
                  className="sr-only"
                  checked={checked}
                  disabled={disabled || blocked}
                  onChange={() => toggle(asset.id)}
                  aria-label={`${t('q-seeds-pick')} ${index + 1}`}
                />
                <AssetThumb
                  versionId={asset.id}
                  thumbnail={asset.thumbnail}
                  alt=""
                  className="aspect-square w-full"
                />
                {asset.origin_kind === 'upload' && (
                  <span className="badge badge-neutral badge-xs absolute bottom-1 left-1">
                    {tx('seed-external-badge')}
                  </span>
                )}
                {checked && (
                  <CheckCircleIcon
                    className="absolute right-1 top-1 h-5 w-5 text-primary drop-shadow"
                    aria-hidden="true"
                  />
                )}
              </label>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
