/**
 * @jest-environment jsdom
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { NewTestFlow } from '@/components/test-plans/NewTestFlow';
import { rememberNewTestSetup } from '@/lib/test-plans/handoff';

const mockPush = jest.fn();
const mockReplace = jest.fn();
let mockQuery: Record<string, string> = { slug: 'acme' };
const mockGateway = {
  createTestPlan: jest.fn(),
  listGenerationAssets: jest.fn(),
};
const telemetry = jest.fn();

jest.mock('next/router', () => ({
  useRouter: () => ({
    push: mockPush,
    replace: mockReplace,
    query: mockQuery,
    isReady: true,
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'MEMBER' } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/components/test-plans/GenerationDefaultsBanner', () => ({
  GenerationDefaultsBanner: () => null,
}));
jest.mock('@/hooks/library/useReferenceImageUrl', () => ({
  useReferenceImageUrl: () => ({ blobUrl: null, isLoading: false }),
}));
jest.mock('@/hooks/assets/useAssetImageUrl', () => ({
  useAssetImageUrl: (id: string) => ({
    blobUrl: `blob:${id}`,
    isLoading: false,
    problem: undefined,
    source: 'thumbnail',
  }),
}));
jest.mock('@/hooks/test-plans/useGenerationDefaults', () => ({
  useGenerationDefaults: () => ({
    defaults: { variants: { sample: 1, small: 2, full: 2 } },
  }),
}));

const PRODUCTS = [
  {
    product_id: 'prod_1',
    entry_id: 'lib_1',
    name: 'VoltNest 壁挂充电桩',
    variant_label: '11 kW',
    ready: true,
    primary_reference_version_id: null,
    latest_run: null,
  },
  {
    product_id: 'prod_2',
    entry_id: 'lib_2',
    name: 'VoltNest 便携款',
    variant_label: null,
    ready: true,
    primary_reference_version_id: null,
    latest_run: null,
  },
];
jest.mock('@/hooks/products/useProductDirectory', () => ({
  useProductDirectory: () => ({
    products: PRODUCTS,
    isLoading: false,
    problem: undefined,
    refresh: jest.fn(),
    nameFor: () => null,
    find: (id: string | null | undefined) =>
      PRODUCTS.find((product) => product.product_id === id),
  }),
}));
jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (operation: string, params: { entry_id?: string }) => {
    if (operation !== 'get_library_entry' || params.entry_id !== 'lib_1') {
      return { data: undefined, isLoading: false, refresh: jest.fn() };
    }
    return {
      isLoading: false,
      refresh: jest.fn(),
      data: {
        current_version_id: 'libver_2',
        versions: [
          { id: 'libver_1', content: { audiences: [{ who: '旧客群' }] } },
          {
            id: 'libver_2',
            content: {
              audiences: [
                { who: '刚提车的通勤族', why: '想省电费' },
                { who: '有院子的家庭', why: '多车共用' },
              ],
            },
          },
        ],
      },
    };
  },
}));

async function flush() {
  await act(async () => {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
  mockQuery = { slug: 'acme' };
  (window as any).__TEST_PLAN_TELEMETRY__ = telemetry;
  mockGateway.createTestPlan.mockResolvedValue({
    kind: 'accepted',
    status: 202,
    data: {
      plan_id: 'atp_9',
      status: 'drafting',
      revision: 1,
      replayed: false,
    },
    meta: {},
  });
});

describe('NewTestFlow', () => {
  it('restores a failed plan setup and keeps it after a refresh until submission', async () => {
    const setup = {
      productId: 'prod_1',
      answers: {
        goal: 'optimize' as const,
        audienceCardIndex: null,
        audienceText: '原方案的客群',
        scale: 'small' as const,
        seedAssetVersionIds: ['av_2'],
      },
    };
    rememberNewTestSetup('acme', setup);
    window.sessionStorage.setItem('products:acme:new-test', 'prod_1');
    mockGateway.listGenerationAssets.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: { items: [{ id: 'av_2' }] },
      meta: {},
    });
    const first = render(<NewTestFlow />);
    await flush();
    expect(
      screen.getByRole('radio', { name: 'Improve existing ads' })
    ).toBeChecked();
    expect(screen.getByRole('radio', { name: 'Small test' })).toBeChecked();
    expect(screen.getByRole('textbox')).toHaveValue('原方案的客群');
    expect(mockGateway.createTestPlan).not.toHaveBeenCalled();
    first.unmount();
    render(<NewTestFlow />);
    await flush();
    expect(screen.getByRole('textbox')).toHaveValue('原方案的客群');
    fireEvent.click(screen.getByRole('button', { name: 'Start drafting' }));
    await flush();
    expect(mockGateway.createTestPlan.mock.calls[0][0].body).toEqual({
      product_id: 'prod_1',
      goal: 'optimize',
      scale: 'small',
      audience_text: '原方案的客群',
      seed_asset_version_ids: ['av_2'],
    });
    expect(window.sessionStorage.getItem('tests:acme:new-setup')).toBeNull();
  });

  it('does not reuse another product or workspace setup', async () => {
    const answers = {
      goal: 'explore' as const,
      audienceCardIndex: null,
      audienceText: '旧产品客群',
      scale: 'full' as const,
      seedAssetVersionIds: [],
    };
    rememberNewTestSetup('acme', { productId: 'prod_2', answers });
    rememberNewTestSetup('other', { productId: 'prod_1', answers });
    window.sessionStorage.setItem('products:acme:new-test', 'prod_1');
    render(<NewTestFlow />);
    await flush();
    expect(screen.getByRole('radio', { name: 'Samples first' })).toBeChecked();
    expect(screen.getByRole('radio', { name: '刚提车的通勤族' })).toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Start drafting' }));
    await flush();
    expect(mockGateway.createTestPlan.mock.calls[0][0].body).toMatchObject({
      product_id: 'prod_1',
      scale: 'sample',
      audience_card_index: 0,
    });
  });

  it('"Start with defaults" sends the three defaults for the handed-over product', async () => {
    window.sessionStorage.setItem('products:acme:new-test', 'prod_1');
    render(<NewTestFlow />);
    await flush();

    expect(screen.getByText('VoltNest 壁挂充电桩')).toBeInTheDocument();
    expect(
      screen.getByText(
        'Defaults: Explore new directions, for “刚提车的通勤族”, Samples first.'
      )
    ).toBeInTheDocument();
    // Defaults are pre-selected.
    expect(
      screen.getByRole('radio', { name: 'Explore new directions' })
    ).toBeChecked();
    expect(screen.getByRole('radio', { name: '刚提车的通勤族' })).toBeChecked();
    expect(screen.getByRole('radio', { name: 'Samples first' })).toBeChecked();
    // Rule of thumb before drafting.
    expect(
      screen.getByText(
        'Up to 12 combinations, 2 images each, 24 images at most.'
      )
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole('button', { name: 'Start with defaults' })
    );
    await flush();

    expect(mockGateway.createTestPlan).toHaveBeenCalledTimes(1);
    expect(mockGateway.createTestPlan.mock.calls[0][0]).toMatchObject({
      workspaceSlug: 'acme',
      body: {
        product_id: 'prod_1',
        goal: 'explore',
        scale: 'sample',
        audience_card_index: 0,
        seed_asset_version_ids: [],
      },
    });
    expect(mockPush).toHaveBeenCalledWith('/w/acme/tests/atp_9');
    expect(telemetry).toHaveBeenCalledWith({
      event: 'test_plan_started',
      goal: 'explore',
      scale: 'sample',
      usedDefaults: true,
      replayed: false,
    });
    // The handoff was consumed and no id is on screen.
    expect(window.sessionStorage.getItem('products:acme:new-test')).toBeNull();
    expect(document.body.textContent).not.toMatch(/prod_|lib_|libver_|atp_/);
  });

  it('sends the answers the user changed', async () => {
    window.sessionStorage.setItem('products:acme:new-test', 'prod_1');
    render(<NewTestFlow />);
    await flush();

    fireEvent.click(screen.getByRole('radio', { name: 'Write my own' }));
    fireEvent.change(screen.getByLabelText('Audience description'), {
      target: { value: '露营爱好者' },
    });
    fireEvent.click(screen.getByRole('radio', { name: 'Small test' }));
    fireEvent.click(screen.getByRole('button', { name: 'Start drafting' }));
    await flush();

    expect(mockGateway.createTestPlan.mock.calls[0][0].body).toEqual({
      product_id: 'prod_1',
      goal: 'explore',
      scale: 'small',
      audience_text: '露营爱好者',
      seed_asset_version_ids: [],
    });
  });

  it('optimize lists Approved assets as seeds and requires one', async () => {
    window.sessionStorage.setItem('products:acme:new-test', 'prod_1');
    mockGateway.listGenerationAssets.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: { items: [{ id: 'av_1' }, { id: 'av_2' }] },
      meta: {},
    });
    render(<NewTestFlow />);
    await flush();

    fireEvent.click(
      screen.getByRole('radio', { name: 'Improve existing ads' })
    );
    await flush();
    expect(mockGateway.listGenerationAssets).toHaveBeenCalledWith(
      expect.objectContaining({ productId: 'prod_1', reviewValue: 'approved' })
    );
    expect(
      screen.getByRole('button', { name: 'Start drafting' })
    ).toBeDisabled();
    expect(
      screen.getByText('Improving existing ads needs at least one asset.')
    ).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('Use this asset 2'));
    fireEvent.click(screen.getByRole('button', { name: 'Start drafting' }));
    await flush();
    expect(mockGateway.createTestPlan.mock.calls[0][0].body).toMatchObject({
      goal: 'optimize',
      seed_asset_version_ids: ['av_2'],
    });
  });

  it('asks which product when nothing was handed over, and strips a deep-link id', async () => {
    mockQuery = { slug: 'acme', product: 'prod_2' };
    render(<NewTestFlow />);
    await flush();
    expect(mockReplace).toHaveBeenCalledWith('/w/acme/tests/new', undefined, {
      shallow: true,
    });
    expect(screen.getByText('VoltNest 便携款')).toBeInTheDocument();
    // Remembered for a refresh of this tab.
    expect(window.sessionStorage.getItem('tests:acme:new-product')).toBe(
      'prod_2'
    );
  });

  it('explains a refusal in its own words', async () => {
    window.sessionStorage.setItem('products:acme:new-test', 'prod_1');
    mockGateway.createTestPlan.mockResolvedValue({
      kind: 'problem',
      status: 409,
      problem: {
        code: 'product_not_published',
        status: 409,
        detail: 'x prod_1',
      },
      meta: {},
    });
    render(<NewTestFlow />);
    await flush();
    fireEvent.click(
      screen.getByRole('button', { name: 'Start with defaults' })
    );
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent(
      "Couldn't start drafting: Confirm the product card (publish the product) before setting up an ad test."
    );
    expect(mockPush).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toMatch(/prod_/);
  });
});
