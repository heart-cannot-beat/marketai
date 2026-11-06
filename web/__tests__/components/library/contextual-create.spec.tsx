/**
 * @jest-environment jsdom
 */
import { createElement } from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';

let mockRun: jest.Mock;
let mockReset: jest.Mock;
let mockInvalidate: jest.Mock;
let mockCommandKind: string | undefined;

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

jest.mock('next/router', () => ({
  useRouter: () => ({
    query: { slug: 'acme' },
    asPath: '/w/acme/library',
    push: jest.fn(),
    replace: jest.fn(),
  }),
}));

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: {
      slug: 'acme',
      workspaceId: 'w1',
      role: 'OWNER',
      membershipId: 'm1',
      displayName: 'Acme',
    },
  }),
}));

jest.mock('@/hooks/library/useLibraryCommand', () => ({
  useLibraryCommand: (commandKind: string) => {
    mockCommandKind = commandKind;
    return {
      run: mockRun,
      retry: jest.fn(),
      reset: mockReset,
      dismiss: jest.fn(),
      isPending: false,
      problem: undefined,
      reconciliation: undefined,
    };
  },
}));

jest.mock('@/hooks/library/useLibraryCache', () => ({
  useLibraryCache: () => ({ invalidate: mockInvalidate }),
}));

import { CreateInternalEntryDialog } from '@/components/library/nested/CreateInternalEntryDialog';

beforeEach(() => {
  mockRun = jest
    .fn()
    .mockResolvedValue({ kind: 'success', data: { id: 'new-entry-1' } });
  mockReset = jest.fn();
  mockInvalidate = jest.fn().mockResolvedValue(undefined);
  mockCommandKind = undefined;
});

describe('CreateInternalEntryDialog', () => {
  const baseProps = {
    entryType: 'offer' as const,
    parentEntryId: 'parent-1',
    parentEntryType: 'product' as const,
    rootEntryId: 'root-1',
    open: true,
    onClose: jest.fn(),
    onCreated: jest.fn(),
  };

  it('renders dialog with title and context label', () => {
    render(createElement(CreateInternalEntryDialog, baseProps));

    expect(
      screen.getByText('library:contextual-create.title')
    ).toBeInTheDocument();
    expect(
      screen.getByText('library:contextual-create.context-label')
    ).toBeInTheDocument();
  });

  it('renders display name input', () => {
    render(createElement(CreateInternalEntryDialog, baseProps));

    expect(
      screen.getByText('library:contextual-create.display-name-label')
    ).toBeInTheDocument();
  });

  it('calls onCreated on successful submit', async () => {
    const onCreated = jest.fn();
    render(
      createElement(CreateInternalEntryDialog, { ...baseProps, onCreated })
    );

    const input = screen
      .getByText('library:contextual-create.display-name-label')
      .closest('label')!
      .querySelector('input')!;
    fireEvent.change(input, { target: { value: 'New Offer' } });

    const submitButton = screen.getByText('library:contextual-create.create');
    await act(async () => {
      fireEvent.click(submitButton);
    });

    expect(mockCommandKind).toBe('create_internal_entry');
    expect(mockRun).toHaveBeenCalledWith({
      root_entry_id: 'root-1',
      body: {
        entry_type: 'offer',
        display_name: 'New Offer',
        parent_type: 'product',
        parent_id: 'parent-1',
      },
    });
    expect(mockInvalidate).toHaveBeenCalledWith({
      resources: ['entryList', 'internalEntries', 'entryTypes'],
    });

    expect(onCreated).toHaveBeenCalledWith('new-entry-1');
  });

  it('shows invalid context message for illegal parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        parentEntryType: 'person' as any,
      })
    );

    expect(
      screen.getByText('library:contextual-create.invalid-context')
    ).toBeInTheDocument();
  });

  it('returns null when not open', () => {
    const { container } = render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        open: false,
      })
    );

    expect(container.innerHTML).toBe('');
  });

  it('calls onClose when cancel is clicked', () => {
    const onClose = jest.fn();
    render(createElement(CreateInternalEntryDialog, { ...baseProps, onClose }));

    const cancelButtons = screen.getAllByText(
      'library:contextual-create.cancel'
    );
    const visibleCancel = cancelButtons.find(
      (b) => b.getAttribute('type') === 'button'
    )!;
    fireEvent.click(visibleCancel);

    expect(onClose).toHaveBeenCalled();
  });

  // Legal context tests for different internal types
  it('allows offer creation under product parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        entryType: 'offer' as const,
        parentEntryType: 'product' as const,
      })
    );

    expect(
      screen.queryByText('library:contextual-create.invalid-context')
    ).toBeNull();
    expect(
      screen.getByText('library:contextual-create.create')
    ).toBeInTheDocument();
  });

  it('allows reference_group creation under product parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        entryType: 'reference_group' as const,
        parentEntryType: 'product' as const,
      })
    );

    expect(
      screen.queryByText('library:contextual-create.invalid-context')
    ).toBeNull();
  });

  it('allows reference_selection_rule creation under reference_group parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        entryType: 'reference_selection_rule' as const,
        parentEntryType: 'reference_group' as const,
      })
    );

    expect(
      screen.queryByText('library:contextual-create.invalid-context')
    ).toBeNull();
  });

  it('rejects reference_selection_rule creation under product parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        entryType: 'reference_selection_rule' as const,
        parentEntryType: 'product' as const,
      })
    );

    expect(
      screen.getByText('library:contextual-create.invalid-context')
    ).toBeInTheDocument();
  });

  it('allows dimension_schema creation under product parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        entryType: 'dimension_schema' as const,
        parentEntryType: 'product' as const,
      })
    );

    expect(
      screen.queryByText('library:contextual-create.invalid-context')
    ).toBeNull();
  });

  it('allows market_source_mapping creation under offer parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        entryType: 'market_source_mapping' as const,
        parentEntryType: 'offer' as const,
      })
    );

    expect(
      screen.queryByText('library:contextual-create.invalid-context')
    ).toBeNull();
  });

  it('rejects market_source_mapping creation under product parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        entryType: 'market_source_mapping' as const,
        parentEntryType: 'product' as const,
      })
    );

    expect(
      screen.getByText('library:contextual-create.invalid-context')
    ).toBeInTheDocument();
  });

  it('allows manual_fact_set creation under data_source parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        entryType: 'manual_fact_set' as const,
        parentEntryType: 'data_source' as const,
      })
    );

    expect(
      screen.queryByText('library:contextual-create.invalid-context')
    ).toBeNull();
  });

  it('rejects manual_fact_set creation under product parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        entryType: 'manual_fact_set' as const,
        parentEntryType: 'product' as const,
      })
    );

    expect(
      screen.getByText('library:contextual-create.invalid-context')
    ).toBeInTheDocument();
  });

  it('allows truth_resolution_policy creation under data_source parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        entryType: 'truth_resolution_policy' as const,
        parentEntryType: 'data_source' as const,
      })
    );

    expect(
      screen.queryByText('library:contextual-create.invalid-context')
    ).toBeNull();
  });

  it('allows reference_asset creation under product parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        entryType: 'reference_asset' as const,
        parentEntryType: 'product' as const,
      })
    );

    expect(
      screen.queryByText('library:contextual-create.invalid-context')
    ).toBeNull();
  });

  it('allows reference_asset creation under person parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        entryType: 'reference_asset' as const,
        parentEntryType: 'person' as const,
      })
    );

    expect(
      screen.queryByText('library:contextual-create.invalid-context')
    ).toBeNull();
  });

  it('allows reference_asset creation under reference_group parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        entryType: 'reference_asset' as const,
        parentEntryType: 'reference_group' as const,
      })
    );

    expect(
      screen.queryByText('library:contextual-create.invalid-context')
    ).toBeNull();
  });

  it('rejects reference_asset creation under data_source parent', () => {
    render(
      createElement(CreateInternalEntryDialog, {
        ...baseProps,
        entryType: 'reference_asset' as const,
        parentEntryType: 'data_source' as const,
      })
    );

    expect(
      screen.getByText('library:contextual-create.invalid-context')
    ).toBeInTheDocument();
  });
});
