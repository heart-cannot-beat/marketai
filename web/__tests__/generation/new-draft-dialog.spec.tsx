/**
 * @jest-environment jsdom
 */
import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from '@testing-library/react';
import { NewDraftDialog } from '@/components/generation/list/NewDraftDialog';

const mockNavigateToDraft = jest.fn();
const mockExecute = jest.fn();

// Matches the per-file mock convention used across
// `__tests__/components/library/*.spec.tsx`: `t(key)` resolves to
// `${namespace}:${key}` rather than real English copy, so assertions target
// the stable key rather than translated prose.
jest.mock('next-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (key: string) => `${ns}:${key}`,
  }),
}));

jest.mock('@/components/generation/shared/useGenerationNav', () => ({
  useGenerationNav: () => ({ navigateToDraft: mockNavigateToDraft }),
}));

// The real `GenerationCapabilities` response model (app/generation/response_models.py)
// is: providers: list[str]; models: dict[str, dict[str, {display_name, format_limits}]]
// keyed by provider; modes: list[str] (not "execution_modes"); format_limits: dict;
// credential_readiness: dict. This mock mirrors that shape rather than the
// `providers: [{id, models}]` shape sketched in the original plan draft.
jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (key: string) => {
    if (key === 'get_generation_capabilities') {
      return {
        data: {
          providers: ['meta', 'mock'],
          models: {
            meta: {
              'llama-image-v1': {
                display_name: 'Llama Image v1',
                format_limits: { max_width: 2048, max_height: 2048 },
              },
            },
            mock: {
              'mock-v1': {
                display_name: 'Mock Generator v1',
                format_limits: { max_width: 2048, max_height: 2048 },
              },
            },
          },
          modes: ['mock', 'sandbox', 'byok'],
          format_limits: {},
          credential_readiness: {},
        },
        problem: undefined,
        isLoading: false,
      };
    }
    return { data: undefined, problem: undefined, isLoading: true };
  },
}));

// `useDomainCommand` resolves the operationId at hook-construction time
// (`useDomainCommand('create_generation_draft')`), not per-call, and
// `execute()` takes a single `{ actionInstanceId, body, ... }` params object
// rather than `execute(operationId, params)` — this mirrors the real
// `web/hooks/domain/useDomainCommand.ts` contract instead of the plan
// draft's assumed shape.
jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: mockExecute,
    retry: jest.fn(),
    reset: jest.fn(),
    result: undefined,
    problem: undefined,
    reconciliation: undefined,
    isPending: false,
  }),
}));

// The product is picked from its card (`list_products`, Issue #162); the
// user never types a `prod_` id.
const PRODUCTS = [
  {
    product_id: 'prod-1',
    name: 'VoltNest 壁挂充电桩',
    variant_label: '11 kW',
    ready: true,
    primary_reference_version_id: null,
  },
  {
    product_id: 'prod-2',
    name: 'Alpha 充电器',
    variant_label: null,
    ready: true,
    primary_reference_version_id: null,
  },
  {
    product_id: 'prod-3',
    name: '还没发布的产品',
    variant_label: null,
    ready: false,
    primary_reference_version_id: null,
  },
];
let mockProducts = PRODUCTS;

jest.mock('@/hooks/products/useProductDirectory', () => ({
  useProductDirectory: () => ({
    products: mockProducts,
    isLoading: false,
    problem: undefined,
    refresh: jest.fn(),
    nameFor: () => undefined,
    find: () => undefined,
  }),
}));

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'OWNER' } }),
}));

jest.mock('@/hooks/library/useReferenceImageUrl', () => ({
  useReferenceImageUrl: () => ({
    blobUrl: null,
    isLoading: false,
    problem: undefined,
  }),
}));

beforeEach(() => {
  mockNavigateToDraft.mockClear();
  mockExecute.mockClear();
  mockProducts = PRODUCTS;
});

describe('NewDraftDialog', () => {
  it('defaults a new draft to the advertised controlled Mock path', async () => {
    render(<NewDraftDialog open onClose={jest.fn()} />);

    await waitFor(() => {
      expect(
        screen.getByLabelText('common:generation-new-draft-provider-label')
      ).toHaveValue('mock');
      expect(
        screen.getByLabelText('common:generation-new-draft-model-label')
      ).toHaveValue('mock-v1');
      expect(
        screen.getByLabelText('common:generation-new-draft-mode-label')
      ).toHaveValue('mock');
    });
  });

  it('renders providers from capabilities and updates models when a provider is selected', () => {
    render(<NewDraftDialog open onClose={jest.fn()} />);

    const providerSelect = screen.getByLabelText(
      'common:generation-new-draft-provider-label'
    );
    expect(within(providerSelect).getByText('meta')).toBeInTheDocument();
    expect(within(providerSelect).getByText('mock')).toBeInTheDocument();

    fireEvent.change(providerSelect, { target: { value: 'meta' } });

    const modelSelect = screen.getByLabelText(
      'common:generation-new-draft-model-label'
    );
    expect(within(modelSelect).getByText('Llama Image v1')).toBeInTheDocument();
  });

  it('creates draft and navigates on submit', async () => {
    mockExecute.mockResolvedValue({
      kind: 'success',
      data: { id: 'draft-new-1' },
      meta: { etag: null, retryAt: null, location: null },
    });

    render(<NewDraftDialog open onClose={jest.fn()} />);

    fireEvent.click(screen.getByRole('radio', { name: 'VoltNest 壁挂充电桩' }));
    fireEvent.change(
      screen.getByLabelText('common:generation-new-draft-provider-label'),
      { target: { value: 'meta' } }
    );
    fireEvent.change(
      screen.getByLabelText('common:generation-new-draft-model-label'),
      { target: { value: 'llama-image-v1' } }
    );
    fireEvent.change(
      screen.getByLabelText('common:generation-new-draft-mode-label'),
      { target: { value: 'byok' } }
    );
    fireEvent.click(screen.getByRole('button', { name: /create/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          actionInstanceId: expect.any(String),
          body: expect.objectContaining({
            product_id: 'prod-1',
            provider: 'meta',
            model: 'llama-image-v1',
            execution_mode: 'byok',
          }),
        })
      );
    });

    await waitFor(() => {
      expect(mockNavigateToDraft).toHaveBeenCalledWith('draft-new-1');
    });
  });

  it('does not navigate when the command returns a problem', async () => {
    mockExecute.mockResolvedValue({
      kind: 'problem',
      problem: {
        type: 'client_error',
        title: 'Invalid',
        status: 422,
        detail: 'product_id is invalid',
        instance: '',
        code: 'invalid_product',
        trace_id: 't1',
        retryable: false,
        field_errors: [],
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    render(<NewDraftDialog open onClose={jest.fn()} />);

    fireEvent.click(screen.getByRole('radio', { name: 'VoltNest 壁挂充电桩' }));
    fireEvent.change(
      screen.getByLabelText('common:generation-new-draft-provider-label'),
      { target: { value: 'meta' } }
    );
    fireEvent.change(
      screen.getByLabelText('common:generation-new-draft-model-label'),
      { target: { value: 'llama-image-v1' } }
    );
    fireEvent.change(
      screen.getByLabelText('common:generation-new-draft-mode-label'),
      { target: { value: 'byok' } }
    );
    fireEvent.click(screen.getByRole('button', { name: /create/i }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalled();
    });

    expect(mockNavigateToDraft).not.toHaveBeenCalled();
  });

  it('disables submit when required fields are empty', () => {
    render(<NewDraftDialog open onClose={jest.fn()} />);
    const submitBtn = screen.getByRole('button', { name: /create/i });
    expect(submitBtn).toBeDisabled();
  });

  it('lists products by name and model, never by id; unpublished ones cannot be picked', () => {
    render(<NewDraftDialog open onClose={jest.fn()} />);

    expect(
      screen.getByRole('radio', { name: 'VoltNest 壁挂充电桩' })
    ).toBeEnabled();
    expect(screen.getByText('11 kW')).toBeInTheDocument();
    expect(
      screen.getByRole('radio', { name: '还没发布的产品' })
    ).toBeDisabled();
    expect(screen.getByText('products:picker-not-ready')).toBeInTheDocument();
    expect(screen.queryByText(/prod-\d/)).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('preselects the product handed over from the product home', () => {
    render(
      <NewDraftDialog open onClose={jest.fn()} initialProductId="prod-2" />
    );
    expect(screen.getByRole('radio', { name: 'Alpha 充电器' })).toBeChecked();
  });

  it('chooses the only usable product without asking', () => {
    mockProducts = [PRODUCTS[0], PRODUCTS[2]];
    render(<NewDraftDialog open onClose={jest.fn()} />);
    expect(
      screen.getByRole('radio', { name: 'VoltNest 壁挂充电桩' })
    ).toBeChecked();
  });

  it('points to adding a product when there is none', () => {
    mockProducts = [];
    render(<NewDraftDialog open onClose={jest.fn()} />);
    expect(screen.getByText('products:picker-empty')).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: 'products:picker-add' })
    ).toHaveAttribute('href', '/w/acme/products/new');
  });

  it('renders nothing when closed', () => {
    const { container } = render(
      <NewDraftDialog open={false} onClose={jest.fn()} />
    );
    expect(container).toBeEmptyDOMElement();
  });
});
