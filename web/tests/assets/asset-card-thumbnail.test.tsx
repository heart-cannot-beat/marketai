/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';

const mockUseAssetImageUrl = jest.fn();

jest.mock('@/hooks/assets/useAssetImageUrl', () => ({
  useAssetImageUrl: (...args: unknown[]) => mockUseAssetImageUrl(...args),
}));

jest.mock('@/components/assets/AssetsProvider', () => ({
  useAssets: () => ({
    selection: {
      isSelected: () => false,
      toggle: jest.fn(),
    },
  }),
}));

import { AssetCard } from '@/components/assets/AssetCard';
import { AssetGrid } from '@/components/assets/AssetGrid';

/** `AssetThumbnailLink` as `list_generation_assets` embeds it (Issue #159). */
const thumbnail = {
  url: 'https://storage.example.com/thumb.jpg',
  expires_at: '2099-01-01T00:00:00Z',
  mime_type: 'image/jpeg',
  width: 512,
  height: 512,
};

function summary(overrides: Record<string, unknown> = {}) {
  return {
    id: 'av-1',
    asset_id: 'a1',
    product_id: 'p1',
    run_id: 'r1',
    provider: 'openai',
    model: 'gpt-image-2',
    status: 'ok',
    aspect_ratio: '1:1',
    stable_dimension_signature: '',
    tags: [],
    effective_review_value: null,
    latest_qc_state: null,
    contextual_conclusion: null,
    origin_kind: 'generation',
    execution_mode: 'byok',
    mock_evidence: null,
    byte_size: 1259492,
    thumbnail,
    created_at: '2026-10-10T00:00:00Z',
    ...overrides,
  } as any;
}

beforeEach(() => {
  mockUseAssetImageUrl.mockReset();
  mockUseAssetImageUrl.mockReturnValue({
    blobUrl: 'blob:thumb',
    isLoading: false,
    problem: undefined,
    source: 'thumbnail',
  });
});

describe('AssetCard thumbnails', () => {
  it('hands the gallery thumbnail link to the image hook', () => {
    render(
      <AssetCard
        versionId="av-1"
        displayNumber={1}
        aspectRatio="1:1"
        provider="openai"
        effectiveReview={null}
        executionMode="byok"
        thumbnail={thumbnail}
        isSelected={false}
        onSelect={jest.fn()}
        onOpen={jest.fn()}
      />
    );

    expect(mockUseAssetImageUrl).toHaveBeenCalledWith('av-1', { thumbnail });
    const image = screen.getByRole('presentation', { hidden: true });
    expect(image).toHaveAttribute('src', 'blob:thumb');
    expect(image).toHaveAttribute('data-image-source', 'thumbnail');
  });

  it('defaults to the original when no link is given', () => {
    render(
      <AssetCard
        versionId="av-1"
        displayNumber={1}
        aspectRatio="1:1"
        provider="openai"
        effectiveReview={null}
        executionMode="byok"
        isSelected={false}
        onSelect={jest.fn()}
        onOpen={jest.fn()}
      />
    );

    expect(mockUseAssetImageUrl).toHaveBeenCalledWith('av-1', {
      thumbnail: null,
    });
  });
});

describe('AssetGrid thumbnails', () => {
  it("passes each item's link through, null for versions without one", () => {
    render(
      <AssetGrid
        items={[
          summary({ id: 'av-1', thumbnail }),
          summary({ id: 'av-2', thumbnail: null }),
          summary({ id: 'av-3', thumbnail: undefined }),
        ]}
        hasMore={false}
        isLoading={false}
        isLoadingMore={false}
        onLoadMore={jest.fn()}
        onOpenVersion={jest.fn()}
      />
    );

    expect(screen.getAllByTestId('asset-card')).toHaveLength(3);
    expect(mockUseAssetImageUrl).toHaveBeenCalledWith('av-1', { thumbnail });
    expect(mockUseAssetImageUrl).toHaveBeenCalledWith('av-2', {
      thumbnail: null,
    });
    expect(mockUseAssetImageUrl).toHaveBeenCalledWith('av-3', {
      thumbnail: null,
    });
  });
});
