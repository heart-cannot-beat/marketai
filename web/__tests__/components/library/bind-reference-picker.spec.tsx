/**
 * @jest-environment jsdom
 *
 * `BindReferenceDialog` is a picker over the Workspace's published
 * references (issue #158): the user clicks a card with the controlled
 * derivative thumbnail; the `libver_` id is never typed.
 */
import { createElement } from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

let mockRun: jest.Mock;
let mockReset: jest.Mock;
let mockInvalidate: jest.Mock;
let mockReferences: {
  items: Array<{
    entryId: string;
    displayName: string;
    versionId: string;
    versionNo: number | null;
    updatedAt: string | null;
  }>;
  hasMore: boolean;
  isLoading: boolean;
  isLoadingMore: boolean;
  problem: unknown;
  loadMore: jest.Mock;
  refresh: jest.Mock;
};
let mockImageUrls: Record<string, string | null>;

jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, string>) => {
      if (opts) {
        let result = `library:${key}`;
        for (const [k, v] of Object.entries(opts)) {
          result = result.replace(`{{${k}}}`, String(v));
        }
        return result;
      }
      return `library:${key}`;
    },
  }),
}));

jest.mock('@/hooks/library/useLibraryCommand', () => ({
  useLibraryCommand: () => ({
    run: mockRun,
    retry: jest.fn(),
    reset: mockReset,
    dismiss: jest.fn(),
    isPending: false,
    problem: undefined,
    reconciliation: undefined,
  }),
}));

jest.mock('@/hooks/library/useLibraryCache', () => ({
  useLibraryCache: () => ({ invalidate: mockInvalidate }),
}));

jest.mock('@/hooks/library/usePublishedReferenceAssets', () => ({
  usePublishedReferenceAssets: () => mockReferences,
}));

jest.mock('@/hooks/library/useReferenceImageUrl', () => ({
  useReferenceImageUrl: (versionId: string) => ({
    blobUrl: mockImageUrls[versionId] ?? null,
    isLoading: false,
    problem: undefined,
  }),
}));

import { BindReferenceDialog } from '@/components/library/commands/BindReferenceDialog';

function renderDialog(
  overrides: Partial<Parameters<typeof BindReferenceDialog>[0]> = {}
) {
  const onClose = jest.fn();
  const onBound = jest.fn();
  render(
    createElement(BindReferenceDialog, {
      entryId: 'entry-1',
      draftEtag: '"3"',
      open: true,
      onClose,
      onBound,
      ...overrides,
    })
  );
  return { onClose, onBound };
}

beforeEach(() => {
  mockRun = jest.fn();
  mockReset = jest.fn();
  mockInvalidate = jest.fn().mockResolvedValue(undefined);
  mockImageUrls = { 'libver-front': 'blob:front' };
  mockReferences = {
    items: [
      {
        entryId: 'lib-front',
        displayName: 'Charger front',
        versionId: 'libver-front',
        versionNo: 1,
        updatedAt: null,
      },
      {
        entryId: 'lib-side',
        displayName: 'Charger side',
        versionId: 'libver-side',
        versionNo: 2,
        updatedAt: null,
      },
    ],
    hasMore: false,
    isLoading: false,
    isLoadingMore: false,
    problem: undefined,
    loadMore: jest.fn(),
    refresh: jest.fn(),
  };
});

describe('BindReferenceDialog picker', () => {
  it('renders one card per published reference with its thumbnail and no id input', () => {
    renderDialog();

    const cards = screen.getAllByTestId('reference-picker-card');
    expect(cards).toHaveLength(2);
    expect(screen.getByText('Charger front')).toBeInTheDocument();
    expect(screen.getByText('Charger side')).toBeInTheDocument();
    expect(screen.getByAltText('Charger front')).toHaveAttribute(
      'src',
      'blob:front'
    );
    expect(
      screen.getByTestId('reference-thumbnail-placeholder')
    ).toBeInTheDocument();
    expect(
      screen.queryByPlaceholderText(
        'library:commands.bind-reference-version-placeholder'
      )
    ).toBeNull();
  });

  it("binds the picked card's current version with the entered purpose", async () => {
    mockRun.mockResolvedValue({ kind: 'success', data: {}, meta: {} });
    const { onBound } = renderDialog();

    fireEvent.click(
      screen.getByRole('radio', { name: 'Charger side', hidden: true })
    );
    expect(
      screen.getByRole('radio', { name: 'Charger side', hidden: true })
    ).toHaveAttribute('aria-checked', 'true');
    expect(
      screen.getByText('library:commands.bind-reference-selected')
    ).toBeInTheDocument();

    const confirm = screen.getByText('library:commands.confirm-bind');
    expect(confirm).toBeDisabled();

    const inputs = screen.getAllByRole('textbox', { hidden: true });
    fireEvent.change(inputs[0], { target: { value: 'hero' } });
    fireEvent.change(inputs[1], { target: { value: 'side' } });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);

    await waitFor(() => expect(onBound).toHaveBeenCalled());
    expect(mockRun).toHaveBeenCalledWith({
      entry_id: 'entry-1',
      body: {
        reference_version_id: 'libver-side',
        purpose: 'hero',
        viewpoint: 'side',
        priority: 0,
        conditions: null,
        draft_etag: '"3"',
      },
    });
    expect(mockInvalidate).toHaveBeenCalledWith({
      resources: ['entry', 'bindings', 'events'],
    });
  });

  it('shows the empty state when nothing is published yet', () => {
    mockReferences = { ...mockReferences, items: [] };
    renderDialog();

    expect(
      screen.getByText('library:commands.bind-reference-empty')
    ).toBeInTheDocument();
    expect(screen.queryAllByTestId('reference-picker-card')).toHaveLength(0);
  });

  it('shows the load failure state', () => {
    mockReferences = {
      ...mockReferences,
      items: [],
      problem: { code: 'internal_error' },
    };
    renderDialog();

    expect(
      screen.getByText('library:commands.bind-reference-load-failed')
    ).toBeInTheDocument();
  });

  it('offers to load more pages', () => {
    mockReferences = { ...mockReferences, hasMore: true };
    renderDialog();

    fireEvent.click(
      screen.getByText('library:commands.bind-reference-load-more')
    );
    expect(mockReferences.loadMore).toHaveBeenCalled();
  });
});
