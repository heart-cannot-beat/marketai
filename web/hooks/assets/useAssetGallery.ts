import { useMemo } from 'react';
import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import type { UseDomainPaginatedQueryResult } from '@/hooks/domain/useDomainPaginatedQuery';
import type { AssetFilters } from '@/hooks/assets/useAssetsUrlState';
import type { AssetThumbnailLink } from '@/hooks/assets/useAssetImageUrl';
import type { AssetFacets } from '@/components/assets/FilterBar';
import type { ActionEligibility } from '@/hooks/assets/useAssetActions';

export interface AssetVersionSummary {
  id: string;
  asset_id: string;
  product_id: string;
  run_id: string;
  provider: string;
  model: string;
  status: string;
  aspect_ratio: string;
  stable_dimension_signature: string;
  tags: string[];
  effective_review_value: string | null;
  latest_qc_state: string | null;
  contextual_conclusion: string | null;
  origin_kind: string;
  execution_mode: string;
  mock_evidence: Record<string, unknown> | null;
  byte_size: number;
  /** Signed 512px thumbnail link (`AssetThumbnailLink`), `null` when the
   * version has no derivative yet -- the card then loads the original. */
  thumbnail?: AssetThumbnailLink | null;
  created_at: string;
}

interface AssetGalleryPage {
  items: AssetVersionSummary[];
  next_cursor?: string | null;
  facets: AssetFacets;
  allowed_actions: ActionEligibility[];
}

export interface UseAssetGalleryResult extends UseDomainPaginatedQueryResult<AssetVersionSummary> {
  facets: AssetFacets | undefined;
  allowedActions: ActionEligibility[];
}

/**
 * Maps the URL-derived, camelCase `AssetFilters` (`useAssetsUrlState`) onto
 * the snake_case query params `list_generation_assets` expects, and drops
 * every filter that is `undefined` so an unset filter never becomes a
 * literal `"undefined"` query param. All gallery facets are forwarded to
 * `list_generation_assets`, so URL state and server-side filtering cannot
 * silently diverge.
 */
export function useAssetGallery(filters: AssetFilters): UseAssetGalleryResult {
  const params = useMemo(() => {
    const p: Record<string, string | undefined> = {
      product_id: filters.productId,
      run_id: filters.runId,
      provider: filters.provider,
      origin_kind: filters.originKind,
      execution_mode: filters.executionMode,
      review_value: filters.reviewValue,
      aspect_ratio: filters.aspectRatio,
      qc_state: filters.qcState,
      tag: filters.tag,
      experiment_context: filters.experimentContext,
      conclusion: filters.conclusion,
    };
    const clean: Record<string, string> = {};
    for (const [k, v] of Object.entries(p)) {
      if (v !== undefined) clean[k] = v;
    }
    return clean;
  }, [filters]);

  const query = useDomainPaginatedQuery<
    'list_generation_assets',
    AssetVersionSummary
  >('list_generation_assets', params as any, { enabled: true });
  const firstPage = query.firstPage as AssetGalleryPage | undefined;

  return {
    ...query,
    facets: firstPage?.facets,
    allowedActions: firstPage?.allowed_actions ?? [],
  };
}
