import { useMemo } from 'react';
import { useTranslation } from 'next-i18next';
import { RunItemStateBadge } from '@/components/generation/shared/RunItemStateBadge';
import {
  useAssetImageUrl,
  type AssetThumbnailLink,
} from '@/hooks/assets/useAssetImageUrl';

/** Subset of `GenerationRunItemSummary` (`list_generation_run_items`,
 * `lib/domain-client/generated/domain-v1.d.ts`) a tile renders. */
export interface RunItemTileItem {
  id: string;
  ordinal: number;
  state: string;
  /** Combination values, `{ dimension: value }` -- the frozen recipe's
   * dimension values for this item (`run_queries._combination_value_tags`). */
  tags?: Record<string, unknown>;
  /** The AssetVersion the item produced; `null` until it succeeds. */
  bound_asset_version_id?: string | null;
  /** That version's signed thumbnail link; `null` without a derivative --
   * the tile then loads the original through the download-intent route. */
  thumbnail?: AssetThumbnailLink | null;
}

interface RunItemTileProps {
  item: RunItemTileItem;
  selected: boolean;
  onSelect: (itemId: string) => void;
}

export interface CombinationTag {
  dimension: string;
  value: string;
}

/** `{ scene: "studio", person: "with_person" }` -> stable, display-ready
 * `[{ dimension, value }]` (sorted by dimension so a re-polled page never
 * reorders a tile's tags). Empty, non-string and object values are left
 * out: a tag is a value label, not a JSON dump. */
export function combinationTags(
  tags: Record<string, unknown> | undefined | null
): CombinationTag[] {
  if (!tags) return [];
  return Object.keys(tags)
    .sort()
    .flatMap((dimension) => {
      const raw = tags[dimension];
      if (raw === null || raw === undefined || typeof raw === 'object') {
        return [];
      }
      const value = String(raw).trim();
      return value ? [{ dimension, value }] : [];
    });
}

/**
 * One selectable tile of `RunItemGrid`: the produced asset's thumbnail
 * (or the original when the version has no derivative), the ordinal, the
 * state badge and the combination value tags. The image hook is only
 * mounted once the item has a bound AssetVersion, so a pending/running
 * item costs no download-intent command and no BFF call.
 */
export function RunItemTile({ item, selected, onSelect }: RunItemTileProps) {
  const { t } = useTranslation('common');
  const tags = useMemo(() => combinationTags(item.tags), [item.tags]);

  return (
    <button
      type="button"
      data-testid="run-item-tile"
      className={`card card-compact bg-base-100 shadow-sm cursor-pointer hover:shadow-md transition-shadow text-left ${
        selected ? 'ring-2 ring-primary' : ''
      }`}
      onClick={() => onSelect(item.id)}
      aria-label={t('generation-run-item-label', {
        ordinal: item.ordinal + 1,
        state: item.state,
      })}
      aria-pressed={selected}
    >
      <div className="relative aspect-square overflow-hidden rounded-t-[inherit] bg-base-200">
        {item.bound_asset_version_id ? (
          <RunItemThumbnail
            versionId={item.bound_asset_version_id}
            thumbnail={item.thumbnail ?? null}
          />
        ) : (
          <div
            className="absolute inset-0 flex items-center justify-center text-xs text-base-content/40"
            data-testid="run-item-tile-placeholder"
          >
            {t('generation-run-item-no-image')}
          </div>
        )}
      </div>
      <div className="card-body gap-1 p-2">
        <div className="flex items-center justify-between gap-1">
          <span className="text-xs text-base-content/60">
            #{item.ordinal + 1}
          </span>
          <RunItemStateBadge state={item.state} />
        </div>
        {tags.length > 0 && (
          <div
            className="flex flex-wrap gap-1"
            data-testid="run-item-tile-tags"
            aria-label={t('generation-run-item-tags-label')}
          >
            {tags.map((tag) => (
              <span
                key={tag.dimension}
                className="badge badge-ghost badge-xs max-w-full truncate"
                title={`${tag.dimension}: ${tag.value}`}
              >
                {tag.value}
              </span>
            ))}
          </div>
        )}
      </div>
    </button>
  );
}

interface RunItemThumbnailProps {
  versionId: string;
  thumbnail: AssetThumbnailLink | null;
}

function RunItemThumbnail({ versionId, thumbnail }: RunItemThumbnailProps) {
  const { t } = useTranslation('common');
  const { blobUrl, isLoading, problem, source } = useAssetImageUrl(versionId, {
    thumbnail,
  });

  if (blobUrl) {
    return (
      <img
        src={blobUrl}
        alt=""
        className="h-full w-full object-cover"
        loading="lazy"
        data-image-source={source ?? undefined}
      />
    );
  }
  if (isLoading) {
    return (
      <div className="absolute inset-0 flex items-center justify-center">
        <span className="loading loading-spinner loading-xs" />
      </div>
    );
  }
  if (problem) {
    return (
      <div className="absolute inset-0 flex items-center justify-center p-1 text-center text-xs text-error">
        {t('generation-run-item-image-failed')}
      </div>
    );
  }
  return null;
}
