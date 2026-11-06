/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { GenerationListView } from '@/components/generation/list/GenerationListView';

const mockReplace = jest.fn();
let mockQuery: Record<string, string> = { slug: 'acme' };
const dialogProps = jest.fn();
const oneDraft = [
  {
    id: 'gd_1',
    product_id: 'prod_1',
    provider: 'mock',
    model: 'mock-v1',
    execution_mode: 'mock',
    state: 'editing',
    revision: 1,
    updated_at: null,
  },
];
let mockDrafts: typeof oneDraft = oneDraft;

jest.mock('next/router', () => ({
  useRouter: () => ({
    query: mockQuery,
    pathname: '/w/[slug]/generate',
    replace: mockReplace,
  }),
}));
jest.mock('@/hooks/domain/useDomainPaginatedQuery', () => ({
  useDomainPaginatedQuery: (operationId: string) => ({
    items: operationId === 'list_generation_drafts' ? mockDrafts : [],
    hasMore: false,
    isLoading: false,
    isLoadingMore: false,
    problem: undefined,
    loadMore: jest.fn(),
    refresh: jest.fn(),
  }),
}));
jest.mock('@/hooks/products/useProductDirectory', () => ({
  useProductDirectory: () => ({
    nameFor: (id: string) => (id === 'prod_1' ? 'VoltNest 壁挂充电桩' : null),
  }),
}));
jest.mock('@/components/generation/shared/useGenerationNav', () => ({
  useGenerationNav: () => ({
    buildDraftUrl: (id: string) => `/drafts/${id}`,
    buildRunUrl: (id: string) => `/runs/${id}`,
  }),
}));
jest.mock('@/components/generation/shared/ExecutionModeTag', () => ({
  ExecutionModeTag: () => null,
}));
jest.mock('@/components/generation/list/NewDraftDialog', () => ({
  NewDraftDialog: (props: any) => {
    dialogProps(props);
    return props.open ? <div data-testid="new-draft-dialog" /> : null;
  },
}));

beforeEach(() => {
  window.sessionStorage.clear();
  mockReplace.mockClear();
  dialogProps.mockClear();
  mockQuery = { slug: 'acme' };
  mockDrafts = oneDraft;
});

describe('GenerationListView empty state', () => {
  // The header button and the empty-state action open the same dialog; a
  // second "New Draft" made the golden path's strict `getByRole` race the
  // drafts fetch.
  it('names the empty-state action apart from the header New Draft', () => {
    mockDrafts = [];
    render(<GenerationListView />);

    expect(screen.getByText('No generations yet')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'New Draft' })).toHaveLength(
      1
    );
    expect(
      screen.getByRole('button', { name: 'Start your first draft' })
    ).toBeInTheDocument();
  });
});

describe('GenerationListView product names (#162)', () => {
  it('titles drafts with the product name, not the prod_ id', () => {
    render(<GenerationListView />);
    expect(
      screen.getByRole('link', { name: 'VoltNest 壁挂充电桩' })
    ).toHaveAttribute('href', '/drafts/gd_1');
    expect(screen.queryByText(/prod_/)).not.toBeInTheDocument();
  });

  it('opens New Draft with the product handed over from the product home', () => {
    window.sessionStorage.setItem('products:acme:new-draft', 'prod_1');
    mockQuery = { slug: 'acme', new: '1' };
    render(<GenerationListView />);

    expect(screen.getByTestId('new-draft-dialog')).toBeInTheDocument();
    expect(dialogProps).toHaveBeenLastCalledWith(
      expect.objectContaining({ open: true, initialProductId: 'prod_1' })
    );
    // The `?new=1` marker is dropped from the URL; the id never was in it.
    expect(mockReplace).toHaveBeenCalledWith(
      { pathname: '/w/[slug]/generate', query: { slug: 'acme' } },
      undefined,
      { shallow: true }
    );
  });
});
