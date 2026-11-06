/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { ProductHome } from '@/components/products/ProductHome';
import { RunList } from '@/components/generation/list/RunList';

let mockDirectory: any;
const mockRefresh = jest.fn();

jest.mock('@/components/products/ProductReferenceSettings', () => ({
  ProductReferenceSettings: () => null,
}));
jest.mock('@/components/products/ProductBrandKitSettings', () => ({
  ProductBrandKitSettings: () => null,
}));

jest.mock('@/hooks/products/useProductDirectory', () => ({
  useProductDirectory: () => mockDirectory,
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'MEMBER' } }),
}));
jest.mock('@/hooks/library/useReferenceImageUrl', () => ({
  useReferenceImageUrl: (id?: string) => ({
    blobUrl: id ? `blob:${id}` : null,
    isLoading: false,
    problem: undefined,
  }),
}));
jest.mock('@/components/test-plans/GenerationDefaultsBanner', () => ({
  GenerationDefaultsBanner: () => null,
}));
const mockLearningsSlot = jest.fn<null, [unknown, unknown]>(() => null);
jest.mock('@/components/products/ProductLearningsSlot', () => ({
  ProductLearningsSlot: (props: unknown, ref: unknown) =>
    mockLearningsSlot(props, ref ?? {}),
}));
jest.mock('@/components/generation/shared/useGenerationNav', () => ({
  useGenerationNav: () => ({ buildRunUrl: (id: string) => `/runs/${id}` }),
}));

const PRODUCTS = [
  {
    product_id: 'prod_1',
    entry_id: 'lib_1',
    name: 'VoltNest 壁挂充电桩',
    variant_label: '11 kW',
    ready: true,
    primary_reference_version_id: 'libver_img',
    latest_run: { status: 'succeeded', done: 4, total: 4, failed: 0 },
  },
  {
    product_id: 'prod_2',
    entry_id: 'lib_2',
    name: null,
    variant_label: null,
    ready: false,
    primary_reference_version_id: null,
    latest_run: null,
  },
];

function directory(overrides: Record<string, unknown> = {}) {
  return {
    products: PRODUCTS,
    isLoading: false,
    problem: undefined,
    refresh: mockRefresh,
    nameFor: (id: string) =>
      PRODUCTS.find((product) => product.product_id === id)?.name ?? null,
    find: () => undefined,
    ...overrides,
  };
}

beforeEach(() => {
  window.sessionStorage.clear();
  mockRefresh.mockClear();
  mockDirectory = directory();
});

describe('ProductHome', () => {
  it('shows each product by image, name, model and latest run -- never by id', () => {
    render(<ProductHome />);
    const cards = screen.getAllByTestId('product-card');
    expect(cards).toHaveLength(2);

    const first = within(cards[0]);
    expect(
      first.getByRole('heading', { name: 'VoltNest 壁挂充电桩' })
    ).toBeInTheDocument();
    expect(first.getByText('11 kW')).toBeInTheDocument();
    expect(
      first.getByText('Latest generation: Completed 4/4')
    ).toBeInTheDocument();
    expect(first.getByRole('img')).toHaveAttribute('src', 'blob:libver_img');

    const second = within(cards[1]);
    expect(
      second.getByRole('heading', { name: 'Unnamed product' })
    ).toBeInTheDocument();
    expect(second.getByText('No creatives generated yet')).toBeInTheDocument();
    expect(second.getByRole('button', { name: 'Start test' })).toBeDisabled();

    expect(document.body.textContent).not.toMatch(/prod_|lib_|libver_/);
  });

  it('starts the marketing test with the product handed over, not in the URL (J-32)', () => {
    render(<ProductHome />);
    const link = within(screen.getAllByTestId('product-card')[0]).getByRole(
      'link',
      { name: 'Start test' }
    );
    // One entry: the card opens the marketing test flow, not the old setup.
    expect(link).toHaveAttribute('href', '/w/acme/experiments');
    fireEvent.click(link);
    expect(window.sessionStorage.getItem('products:acme:marketing-test')).toBe(
      'prod_1'
    );
    expect(window.sessionStorage.getItem('products:acme:new-test')).toBeNull();
  });

  it('hides the learnings card on every product, with its import and next round (#337)', () => {
    render(<ProductHome />);
    expect(screen.getAllByTestId('product-card')).toHaveLength(2);
    expect(mockLearningsSlot).not.toHaveBeenCalled();
    expect(screen.queryByTestId('product-learnings')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /What we learned about/ })
    ).not.toBeInTheDocument();
  });

  it('invites adding the first product in an empty Workspace', () => {
    mockDirectory = directory({ products: [] });
    render(<ProductHome />);
    expect(screen.getByText('Add your first product')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Add product' })).toHaveAttribute(
      'href',
      '/w/acme/products/new'
    );
  });

  it('highlights the product just added, once', () => {
    window.sessionStorage.setItem('products:acme:highlight', 'prod_1');
    render(<ProductHome />);
    expect(screen.getByRole('status')).toHaveTextContent(
      'Added “VoltNest 壁挂充电桩”'
    );
    expect(screen.getAllByTestId('product-card')[0]).toHaveAttribute(
      'aria-current',
      'true'
    );
    expect(window.sessionStorage.getItem('products:acme:highlight')).toBeNull();
    expect(mockRefresh).toHaveBeenCalled();
  });

  it('offers a retry when the list fails', () => {
    mockDirectory = directory({
      products: [],
      problem: { code: 'internal', status: 500 },
    });
    render(<ProductHome />);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(mockRefresh).toHaveBeenCalled();
  });
});

describe('RunList', () => {
  it('titles runs with the product name, and 未命名产品 for unknown ones', () => {
    render(
      <RunList
        runs={[
          {
            id: 'gr_1',
            product_id: 'prod_1',
            status: 'running',
            done: 1,
            total: 4,
            created_at: null,
          },
          {
            id: 'gr_2',
            product_id: 'prod_gone',
            status: 'succeeded',
            done: 2,
            total: 2,
            created_at: null,
          },
        ]}
      />
    );
    expect(
      screen.getByRole('link', { name: 'VoltNest 壁挂充电桩' })
    ).toHaveAttribute('href', '/runs/gr_1');
    expect(
      screen.getByRole('link', { name: 'Unnamed product' })
    ).toBeInTheDocument();
    expect(screen.queryByText(/prod_/)).not.toBeInTheDocument();
  });
});
