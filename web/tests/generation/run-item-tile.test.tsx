/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent, within } from '@testing-library/react';

const mockUseAssetImageUrl = jest.fn();

jest.mock('@/hooks/assets/useAssetImageUrl', () => ({
  useAssetImageUrl: (...args: unknown[]) => mockUseAssetImageUrl(...args),
}));

import {
  RunItemTile,
  combinationTags,
} from '@/components/generation/progress/RunItemTile';
import { RunItemGrid } from '@/components/generation/progress/RunItemGrid';

/** `AssetThumbnailLink` as `list_generation_run_items` embeds it. */
const thumbnail = {
  url: 'https://storage.example.com/thumb.jpg',
  expires_at: '2099-01-01T00:00:00Z',
  mime_type: 'image/jpeg',
  width: 512,
  height: 512,
};

const succeededItem = {
  id: 'item-1',
  ordinal: 0,
  state: 'succeeded',
  tags: { scene: 'studio', person: 'with_person' },
  bound_asset_version_id: 'av-1',
  thumbnail,
};

const runningItem = {
  id: 'item-2',
  ordinal: 1,
  state: 'running',
  tags: {},
  bound_asset_version_id: null,
  thumbnail: null,
};

beforeEach(() => {
  mockUseAssetImageUrl.mockReset();
  mockUseAssetImageUrl.mockReturnValue({
    blobUrl: 'blob:thumb',
    isLoading: false,
    problem: undefined,
    source: 'thumbnail',
  });
});

describe('combinationTags', () => {
  it('turns the frozen combination values into sorted display tags', () => {
    expect(
      combinationTags({ scene: 'studio', person: 'with_person', car: 'ev' })
    ).toEqual([
      { dimension: 'car', value: 'ev' },
      { dimension: 'person', value: 'with_person' },
      { dimension: 'scene', value: 'studio' },
    ]);
  });

  it('skips empty, null and nested values', () => {
    expect(
      combinationTags({
        scene: '  ',
        person: null,
        nested: { a: 1 },
        count: 2,
      })
    ).toEqual([{ dimension: 'count', value: '2' }]);
    expect(combinationTags(undefined)).toEqual([]);
    expect(combinationTags(null)).toEqual([]);
  });
});

describe('RunItemTile', () => {
  it('renders the thumbnail, the ordinal, the state and the combination values', () => {
    render(
      <RunItemTile item={succeededItem} selected={false} onSelect={jest.fn()} />
    );

    expect(mockUseAssetImageUrl).toHaveBeenCalledWith('av-1', { thumbnail });
    const tile = screen.getByRole('button', { name: 'Item 1: succeeded' });
    const image = within(tile).getByRole('presentation', { hidden: true });
    expect(image).toHaveAttribute('src', 'blob:thumb');
    expect(image).toHaveAttribute('data-image-source', 'thumbnail');
    expect(within(tile).getByText('#1')).toBeInTheDocument();
    expect(within(tile).getByText('Succeeded')).toBeInTheDocument();

    const tags = within(tile).getByTestId('run-item-tile-tags');
    expect(tags).toHaveAttribute('aria-label', 'Combination values');
    expect(
      within(tags)
        .getAllByText(/./)
        .map((el) => el.textContent)
    ).toEqual(['with_person', 'studio']);
    expect(within(tags).getByText('studio')).toHaveAttribute(
      'title',
      'scene: studio'
    );
  });

  it('shows a placeholder and never loads an image for an item without a produced version', () => {
    render(
      <RunItemTile item={runningItem} selected={false} onSelect={jest.fn()} />
    );

    expect(mockUseAssetImageUrl).not.toHaveBeenCalled();
    const tile = screen.getByRole('button', { name: 'Item 2: running' });
    expect(
      within(tile).getByTestId('run-item-tile-placeholder')
    ).toHaveTextContent('No image yet');
    expect(
      within(tile).queryByTestId('run-item-tile-tags')
    ).not.toBeInTheDocument();
    expect(within(tile).getByText('Running')).toBeInTheDocument();
  });

  it('loads the original when the version has no thumbnail link', () => {
    mockUseAssetImageUrl.mockReturnValue({
      blobUrl: 'blob:original',
      isLoading: false,
      problem: undefined,
      source: 'original',
    });
    render(
      <RunItemTile
        item={{ ...succeededItem, thumbnail: null }}
        selected={false}
        onSelect={jest.fn()}
      />
    );

    expect(mockUseAssetImageUrl).toHaveBeenCalledWith('av-1', {
      thumbnail: null,
    });
    expect(screen.getByRole('presentation', { hidden: true })).toHaveAttribute(
      'data-image-source',
      'original'
    );
  });

  it('shows the loading and failure states of the image', () => {
    mockUseAssetImageUrl.mockReturnValue({
      blobUrl: null,
      isLoading: true,
      problem: undefined,
      source: null,
    });
    const { rerender } = render(
      <RunItemTile item={succeededItem} selected={false} onSelect={jest.fn()} />
    );
    expect(document.querySelector('.loading-spinner')).toBeInTheDocument();

    mockUseAssetImageUrl.mockReturnValue({
      blobUrl: null,
      isLoading: false,
      problem: { code: 'transfer_error' },
      source: null,
    });
    rerender(
      <RunItemTile item={succeededItem} selected={false} onSelect={jest.fn()} />
    );
    expect(screen.getByText('Image unavailable')).toBeInTheDocument();
  });

  it('selects on click and reflects selection', () => {
    const onSelect = jest.fn();
    render(
      <RunItemTile item={succeededItem} selected={true} onSelect={onSelect} />
    );
    const tile = screen.getByRole('button', { name: 'Item 1: succeeded' });
    expect(tile).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(tile);
    expect(onSelect).toHaveBeenCalledWith('item-1');
  });
});

describe('RunItemGrid', () => {
  it('renders one tile per item and the load-more control', () => {
    const onPageChange = jest.fn();
    render(
      <RunItemGrid
        items={[succeededItem, runningItem]}
        selectedItemId="item-2"
        onSelectItem={jest.fn()}
        pagination={{ cursor: 'c2', hasMore: true }}
        onPageChange={onPageChange}
      />
    );

    expect(screen.getAllByTestId('run-item-tile')).toHaveLength(2);
    expect(
      screen.getByRole('button', { name: 'Item 2: running' })
    ).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    expect(onPageChange).toHaveBeenCalledWith('c2');
  });
});
