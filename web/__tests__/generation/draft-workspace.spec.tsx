/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { DraftWorkspace } from '@/components/generation/draft/DraftWorkspace';

/**
 * `GenerationDraftDetail` (`lib/domain-client/generated/domain-v1.d.ts`) has
 * no `title` field -- only `product_id`. `DraftWorkspace` falls back to
 * `product_id` when `title` is absent (see the component), so this mock
 * supplies `title` to pin the "prefer title when present" branch; the
 * "falls back to product_id" branch is covered separately below.
 */
const mockDraft = {
  id: 'draft-1',
  title: 'Test Draft',
  product_id: 'prod-1',
  execution_mode: 'byok',
  provider: 'meta',
  model: 'llama-3',
  state: 'draft',
  selectors: {},
  revision: 1,
  allowed_actions: [
    { action: 'update', allowed: true, reason_code: null },
    { action: 'preview', allowed: true, reason_code: null },
  ],
};

// `useDomainQuery` is wrapped in `jest.fn()` (rather than a plain arrow
// function) so individual tests can override its return value with
// `mockReturnValueOnce`/`mockImplementation` -- a bare factory function
// isn't a jest mock and has no such methods.
function defaultUseDomainQueryImpl(key: string): any {
  if (key === 'get_generation_draft') {
    return {
      data: mockDraft,
      isLoading: false,
      problem: undefined,
      refresh: jest.fn(),
    };
  }
  return {
    data: undefined,
    isLoading: true,
    problem: undefined,
    refresh: jest.fn(),
  };
}

const mockUseDomainQuery = jest.fn(defaultUseDomainQueryImpl);

function draftWithoutTitle(): Record<string, unknown> {
  const clone: Record<string, unknown> = { ...mockDraft };
  delete clone.title;
  return clone;
}

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (...args: unknown[]) =>
    mockUseDomainQuery(...(args as [string])),
}));

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: jest
      .fn()
      .mockResolvedValue({ kind: 'success', data: {}, meta: {} }),
    isPending: false,
  }),
}));

jest.mock('@/components/generation/draft/DraftComposer', () => ({
  DraftComposer: () => <div data-testid="draft-composer" />,
}));

describe('DraftWorkspace', () => {
  beforeEach(() => {
    mockUseDomainQuery.mockReset();
    mockUseDomainQuery.mockImplementation(defaultUseDomainQueryImpl);
  });

  it('renders draft title and composer', () => {
    render(<DraftWorkspace draftId="draft-1" previewId={null} />);
    expect(screen.getByText('Test Draft')).toBeInTheDocument();
    expect(screen.getByTestId('draft-composer')).toBeInTheDocument();
  });

  function withProducts(items: Array<{ product_id: string; name: string }>) {
    mockUseDomainQuery.mockImplementation((key: string) => {
      if (key === 'get_generation_draft') {
        return {
          data: draftWithoutTitle(),
          isLoading: false,
          problem: undefined,
          refresh: jest.fn(),
        };
      }
      if (key === 'list_products') {
        return {
          data: { items: items.map((item) => ({ ...item, ready: true })) },
          isLoading: false,
          problem: undefined,
          refresh: jest.fn(),
        };
      }
      return {
        data: undefined,
        isLoading: true,
        problem: undefined,
        refresh: jest.fn(),
      };
    });
  }

  it('heads the page with the product name, never its id, when title is absent', () => {
    withProducts([{ product_id: 'prod-1', name: 'VoltNest 壁挂充电桩' }]);
    render(<DraftWorkspace draftId="draft-1" previewId={null} />);
    expect(
      screen.getByRole('heading', { level: 1, name: 'VoltNest 壁挂充电桩' })
    ).toBeInTheDocument();
    expect(screen.queryByText('prod-1')).not.toBeInTheDocument();
  });

  it('reads Unnamed product when the product is unknown', () => {
    withProducts([]);
    render(<DraftWorkspace draftId="draft-1" previewId={null} />);
    expect(
      screen.getByRole('heading', { level: 1, name: 'Unnamed product' })
    ).toBeInTheDocument();
    expect(screen.queryByText('prod-1')).not.toBeInTheDocument();
  });

  it('renders children slot', () => {
    render(
      <DraftWorkspace draftId="draft-1" previewId="prev-1">
        <div data-testid="preview-slot" />
      </DraftWorkspace>
    );
    expect(screen.getByTestId('preview-slot')).toBeInTheDocument();
  });

  it('shows loading state', () => {
    mockUseDomainQuery.mockReturnValue({
      data: undefined,
      isLoading: true,
      problem: undefined,
      refresh: jest.fn(),
    });
    render(<DraftWorkspace draftId="draft-1" previewId={null} />);
    expect(screen.getByRole('status')).toBeInTheDocument();
  });

  it('does not render Generate Preview button when previewId is already set', () => {
    render(<DraftWorkspace draftId="draft-1" previewId="prev-1" />);
    expect(screen.queryByText(/generate preview/i)).not.toBeInTheDocument();
  });

  it('renders Generate Preview button when preview is allowed and no preview exists', () => {
    render(<DraftWorkspace draftId="draft-1" previewId={null} />);
    expect(screen.getByText(/generate preview/i)).toBeInTheDocument();
  });

  it('hides Generate Preview button when preview is not allowed', () => {
    mockUseDomainQuery.mockImplementation((key: string) => {
      if (key === 'get_generation_draft') {
        return {
          data: {
            ...mockDraft,
            allowed_actions: [
              { action: 'update', allowed: true, reason_code: null },
              {
                action: 'preview',
                allowed: false,
                reason_code: 'draft_incomplete',
              },
            ],
          },
          isLoading: false,
          problem: undefined,
          refresh: jest.fn(),
        };
      }
      return {
        data: undefined,
        isLoading: true,
        problem: undefined,
        refresh: jest.fn(),
      };
    });
    render(<DraftWorkspace draftId="draft-1" previewId={null} />);
    expect(screen.queryByText(/generate preview/i)).not.toBeInTheDocument();
  });
});
