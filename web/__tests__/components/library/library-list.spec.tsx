/**
 * @jest-environment jsdom
 */
import { createElement } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

const mockUseLibraryEntries = jest.fn();

jest.mock('@/hooks/library/useLibraryEntries', () => ({
  useLibraryEntries: (...args: unknown[]) => mockUseLibraryEntries(...args),
}));

jest.mock('@/components/library/commands/CreateRootEntryDialog', () => {
  const React = jest.requireActual<typeof import('react')>('react');
  return {
    CreateRootEntryDialog: ({
      open,
      onCreated,
    }: {
      open: boolean;
      onCreated: (entryId: string) => void;
    }) =>
      open
        ? React.createElement(
            'button',
            { onClick: () => onCreated('new-entry') },
            'finish creation'
          )
        : null,
  };
});

jest.mock('next-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (key: string, opts?: Record<string, string>) => {
      if (opts?.defaultValue) return opts.defaultValue;
      if (opts?.category)
        return `${ns}:${key}`.replace('{{category}}', opts.category);
      return `${ns}:${key}`;
    },
  }),
}));

import { LibraryListView } from '@/components/library/list/LibraryListView';
import {
  LIBRARY_ENTRY_TYPES,
  type LibraryCategory,
} from '@/lib/library/navigation';

function defaultListResult(
  overrides?: Partial<ReturnType<typeof mockUseLibraryEntries>>
) {
  return {
    items: [],
    hasMore: false,
    isLoading: false,
    isLoadingMore: false,
    isRefreshing: false,
    problem: undefined,
    loadMore: jest.fn(),
    refresh: jest.fn(),
    ...overrides,
  };
}

describe('LibraryListView — internal types never in top-level lists', () => {
  const INTERNAL_TYPES = LIBRARY_ENTRY_TYPES.filter(
    (et) =>
      ![
        'product',
        'person',
        'dimension_template',
        'prompt_policy',
        'data_source',
      ].includes(et)
  );

  it.each([
    ['product', 'product'],
    ['person', 'person'],
    ['template', 'dimension_template'],
    ['prompt_policy', 'prompt_policy'],
    ['datasource', 'data_source'],
  ] as [LibraryCategory, string][])(
    'category %s calls useLibraryEntries with root type %s only',
    (category, expectedRootType) => {
      mockUseLibraryEntries.mockReturnValue(defaultListResult());
      render(
        createElement(LibraryListView, {
          category,
          selectedEntryId: null,
          onSelectEntry: jest.fn(),
        })
      );
      expect(mockUseLibraryEntries).toHaveBeenCalledWith(expectedRootType);
    }
  );

  it('never passes an internal type to useLibraryEntries', () => {
    const categories: LibraryCategory[] = [
      'product',
      'person',
      'template',
      'prompt_policy',
      'datasource',
    ];
    for (const cat of categories) {
      mockUseLibraryEntries.mockReturnValue(defaultListResult());
      render(
        createElement(LibraryListView, {
          category: cat,
          selectedEntryId: null,
          onSelectEntry: jest.fn(),
        })
      );
    }
    for (const call of mockUseLibraryEntries.mock.calls) {
      expect(INTERNAL_TYPES).not.toContain(call[0]);
    }
  });
});

describe('LibraryListView — states', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('shows loading state', () => {
    mockUseLibraryEntries.mockReturnValue(
      defaultListResult({ isLoading: true })
    );
    render(
      createElement(LibraryListView, {
        category: 'product',
        selectedEntryId: null,
        onSelectEntry: jest.fn(),
      })
    );
    expect(screen.getByText('library:list.loading')).toBeInTheDocument();
  });

  it('shows empty state when no items', () => {
    mockUseLibraryEntries.mockReturnValue(defaultListResult());
    render(
      createElement(LibraryListView, {
        category: 'person',
        selectedEntryId: null,
        onSelectEntry: jest.fn(),
      })
    );
    expect(screen.getByText('library:list.empty')).toBeInTheDocument();
  });

  it('opens creation from the empty state and selects the new entry', () => {
    const onSelectEntry = jest.fn();
    mockUseLibraryEntries.mockReturnValue(defaultListResult());
    render(
      createElement(LibraryListView, {
        category: 'product',
        selectedEntryId: null,
        onSelectEntry,
      })
    );

    fireEvent.click(screen.getByText('library:commands.create-entry'));
    fireEvent.click(screen.getByText('finish creation'));
    expect(onSelectEntry).toHaveBeenCalledWith('new-entry');
  });

  it('shows problem/retry when problem with no items', () => {
    const refresh = jest.fn();
    mockUseLibraryEntries.mockReturnValue(
      defaultListResult({
        problem: { type: 'server_error', title: 'Error', status: 500 },
        refresh,
      })
    );
    render(
      createElement(LibraryListView, {
        category: 'product',
        selectedEntryId: null,
        onSelectEntry: jest.fn(),
      })
    );
    expect(screen.getByText('library:problem.title')).toBeInTheDocument();
    fireEvent.click(screen.getByText('library:problem.retry'));
    expect(refresh).toHaveBeenCalled();
  });

  it('renders entries and marks selected', () => {
    mockUseLibraryEntries.mockReturnValue(
      defaultListResult({
        items: [
          {
            id: 'e1',
            display_name: 'Alpha',
            lifecycle: 'active',
            draft_state: null,
            current_version_number: 1,
          },
          {
            id: 'e2',
            display_name: 'Beta',
            lifecycle: 'archived',
            draft_state: 'editing',
            current_version_number: 2,
          },
        ],
      })
    );
    render(
      createElement(LibraryListView, {
        category: 'product',
        selectedEntryId: 'e1',
        onSelectEntry: jest.fn(),
      })
    );
    expect(screen.getByText('Alpha')).toBeInTheDocument();
    expect(screen.getByText('Beta')).toBeInTheDocument();

    const alphaButton = screen.getByText('Alpha').closest('button');
    expect(alphaButton).toHaveAttribute('aria-current', 'true');
  });

  it('shows load-more when hasMore', () => {
    const loadMore = jest.fn();
    mockUseLibraryEntries.mockReturnValue(
      defaultListResult({
        items: [
          {
            id: 'e1',
            display_name: 'Alpha',
            lifecycle: 'active',
            draft_state: null,
            current_version_number: 1,
          },
        ],
        hasMore: true,
        loadMore,
      })
    );
    render(
      createElement(LibraryListView, {
        category: 'product',
        selectedEntryId: null,
        onSelectEntry: jest.fn(),
      })
    );
    const btn = screen.getByText('library:list.load-more');
    fireEvent.click(btn);
    expect(loadMore).toHaveBeenCalled();
  });

  it('shows end-of-list when no more pages', () => {
    mockUseLibraryEntries.mockReturnValue(
      defaultListResult({
        items: [
          {
            id: 'e1',
            display_name: 'Alpha',
            lifecycle: 'active',
            draft_state: null,
            current_version_number: 1,
          },
        ],
      })
    );
    render(
      createElement(LibraryListView, {
        category: 'product',
        selectedEntryId: null,
        onSelectEntry: jest.fn(),
      })
    );
    expect(screen.getByText('library:list.end-of-list')).toBeInTheDocument();
  });

  it('shows refreshing indicator', () => {
    mockUseLibraryEntries.mockReturnValue(
      defaultListResult({
        items: [
          {
            id: 'e1',
            display_name: 'Alpha',
            lifecycle: 'active',
            draft_state: null,
            current_version_number: 1,
          },
        ],
        isRefreshing: true,
      })
    );
    render(
      createElement(LibraryListView, {
        category: 'product',
        selectedEntryId: null,
        onSelectEntry: jest.fn(),
      })
    );
    expect(screen.getByText('library:list.refreshing')).toBeInTheDocument();
  });

  it('shows loading-more state in button', () => {
    mockUseLibraryEntries.mockReturnValue(
      defaultListResult({
        items: [
          {
            id: 'e1',
            display_name: 'Alpha',
            lifecycle: 'active',
            draft_state: null,
            current_version_number: 1,
          },
        ],
        hasMore: true,
        isLoadingMore: true,
      })
    );
    render(
      createElement(LibraryListView, {
        category: 'product',
        selectedEntryId: null,
        onSelectEntry: jest.fn(),
      })
    );
    const btn = screen.getByText('library:list.loading');
    expect(btn).toBeDisabled();
  });

  it('calls onSelectEntry when clicking a row', () => {
    const onSelect = jest.fn();
    mockUseLibraryEntries.mockReturnValue(
      defaultListResult({
        items: [
          {
            id: 'e1',
            display_name: 'Alpha',
            lifecycle: 'active',
            draft_state: null,
            current_version_number: 1,
          },
        ],
      })
    );
    render(
      createElement(LibraryListView, {
        category: 'product',
        selectedEntryId: null,
        onSelectEntry: onSelect,
      })
    );
    fireEvent.click(screen.getByText('Alpha'));
    expect(onSelect).toHaveBeenCalledWith('e1');
  });

  it('shows inline problem when items exist but page-two fails', () => {
    mockUseLibraryEntries.mockReturnValue(
      defaultListResult({
        items: [
          {
            id: 'e1',
            display_name: 'Alpha',
            lifecycle: 'active',
            draft_state: null,
            current_version_number: 1,
          },
        ],
        problem: { type: 'server_error', title: 'Error', status: 500 },
      })
    );
    render(
      createElement(LibraryListView, {
        category: 'product',
        selectedEntryId: null,
        onSelectEntry: jest.fn(),
      })
    );
    expect(screen.getByText('Alpha')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });
});
