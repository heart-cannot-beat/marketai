/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { PreviewPanel } from '@/components/generation/preview/PreviewPanel';

/**
 * `GenerationPreviewDetail` (`lib/domain-client/generated/domain-v1.d.ts`,
 * mirrored by `app/generation/response_models.py`). Field names here are
 * the real backend/generated ones — `final_prompt`, `maximum_cost`,
 * `exclusion_summary` — not the idealized `prompt_snapshot`/`estimated_max_cost`
 * names floated elsewhere; this mock is pinned to what the domain client
 * actually returns.
 */
const basePreview = {
  purpose: 'initial_generation' as const,
  id: 'preview-1',
  preview_hash: 'hash-abc',
  recipe_input_hash: 'recipe-abc',
  execution_mode: 'byok',
  valid_until: new Date(Date.now() + 600_000).toISOString(),
  draft_id: 'draft-1',
  draft_revision: 3,
  invalidation_reason: null,
  combination_count: 12,
  call_count: 12,
  maximum_cost: { amount: '4.80', currency: 'USD' },
  final_prompt: 'A hero shot of the product on a marble countertop.',
  items: [],
  capability_decisions: [
    { capability: 'image_edit', supported: true, detail: null },
    {
      capability: 'video',
      supported: false,
      detail: 'provider does not support video',
    },
  ],
  exclusion_summary: { excluded_count: 2, reasons: ['duplicate_combination'] },
  allowed_actions: [],
};

function defaultUseDomainQueryImpl(key: string): any {
  if (key === 'get_generation_preview') {
    return {
      data: basePreview,
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

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (...args: unknown[]) =>
    mockUseDomainQuery(...(args as [string])),
}));

describe('PreviewPanel', () => {
  beforeEach(() => {
    mockUseDomainQuery.mockReset();
    mockUseDomainQuery.mockImplementation(defaultUseDomainQueryImpl);
  });

  it('renders combination count, call count, cost, and prompt for a normal preview', () => {
    render(<PreviewPanel previewId="preview-1" />);

    // Both combination_count and call_count are 12 in the fixture -- assert
    // there are exactly two "12" values rendered (the stat blocks) rather
    // than a single ambiguous match.
    expect(screen.getAllByText('12')).toHaveLength(2);
    expect(screen.getByText('$4.80')).toBeInTheDocument();
    expect(
      screen.getByText('A hero shot of the product on a marble countertop.')
    ).toBeInTheDocument();
    expect(screen.getByText(/valid for/i)).toBeInTheDocument();
  });

  it('shows the stale/expired indicator when the preview is past valid_until', () => {
    mockUseDomainQuery.mockReturnValue({
      data: {
        ...basePreview,
        valid_until: new Date(Date.now() - 1000).toISOString(),
      },
      isLoading: false,
      problem: undefined,
      refresh: jest.fn(),
    });

    render(<PreviewPanel previewId="preview-1" />);

    expect(screen.getByText(/expired/i)).toBeInTheDocument();
  });

  it('shows the invalidation reason when the draft changed after preview creation', () => {
    mockUseDomainQuery.mockReturnValue({
      data: {
        ...basePreview,
        invalidation_reason: 'Draft was modified after preview creation',
      },
      isLoading: false,
      problem: undefined,
      refresh: jest.fn(),
    });

    render(<PreviewPanel previewId="preview-1" />);

    expect(screen.getByText(/draft was modified/i)).toBeInTheDocument();
  });

  it('shows a loading indicator while the preview is fetching', () => {
    mockUseDomainQuery.mockReturnValue({
      data: undefined,
      isLoading: true,
      problem: undefined,
      refresh: jest.fn(),
    });

    render(<PreviewPanel previewId="preview-1" />);

    expect(screen.getByRole('status')).toBeInTheDocument();
  });

  it('shows the applied strategy and the constraint-dropped count (Issue #157)', () => {
    mockUseDomainQuery.mockReturnValue({
      data: {
        ...basePreview,
        strategy_applied: {
          kind: 'coverage',
          cap: 12,
          deprecated_shape: true,
          full_count: 24,
          candidate_count: 20,
          selected_count: 12,
        },
        combinations_dropped_by_constraints: 4,
      },
      isLoading: false,
      problem: undefined,
      refresh: jest.fn(),
    });

    render(<PreviewPanel previewId="preview-1" />);

    expect(screen.getByTestId('strategy-applied')).toHaveTextContent(
      'Coverage sample · cap 12 of 24'
    );
    expect(screen.getByTestId('constraints-dropped')).toHaveTextContent('4');
    expect(screen.getByText(/old format/i)).toBeInTheDocument();
  });

  it('renders a full-product strategy without the deprecated-shape note', () => {
    mockUseDomainQuery.mockReturnValue({
      data: {
        ...basePreview,
        strategy_applied: { kind: 'full', full_count: 12, selected_count: 12 },
        combinations_dropped_by_constraints: 0,
      },
      isLoading: false,
      problem: undefined,
      refresh: jest.fn(),
    });

    render(<PreviewPanel previewId="preview-1" />);

    expect(screen.getByTestId('strategy-applied')).toHaveTextContent(
      'Full product · 12 combinations'
    );
    expect(screen.queryByTestId('constraints-dropped')).toBeNull();
    expect(screen.queryByText(/old format/i)).toBeNull();
  });

  it('shows a not-found message when the preview has no data and no problem', () => {
    mockUseDomainQuery.mockReturnValue({
      data: undefined,
      isLoading: false,
      problem: undefined,
      refresh: jest.fn(),
    });

    render(<PreviewPanel previewId="preview-1" />);

    expect(screen.getByText(/preview not found/i)).toBeInTheDocument();
  });
});
