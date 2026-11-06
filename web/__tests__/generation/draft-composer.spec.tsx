/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import {
  DraftComposer,
  maxVariantsFromCapabilities,
} from '@/components/generation/draft/DraftComposer';

const CAPABILITIES = {
  providers: ['mock', 'meta'],
  modes: ['mock'],
  models: {
    mock: {
      'mock-v1': {
        display_name: 'Mock Generator v1',
        format_limits: { max_variants_per_combination: 4 },
      },
    },
    meta: {
      'llama-image-v1': {
        display_name: 'Llama',
        format_limits: { max_variants_per_combination: 2 },
      },
    },
  },
  format_limits: {},
  credential_readiness: {},
};

const mockExecute = jest.fn();

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: (...args: unknown[]) => mockExecute(...args),
    isPending: false,
  }),
}));

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (key: string) => {
    if (key === 'get_generation_capabilities') {
      return { data: CAPABILITIES, isLoading: false, refresh: jest.fn() };
    }
    return {
      data: { items: [] },
      isLoading: false,
      problem: undefined,
      refresh: jest.fn(),
    };
  },
}));

jest.mock('@/hooks/generation/useTemplateDimensions', () => ({
  useTemplateDimensions: (ids: string[]) => ({
    dimensions:
      ids.length > 0
        ? [
            {
              key: 'lib_t:angle',
              templateEntryId: 'lib_t',
              templateName: 'Grid',
              dimensionId: 'angle',
              name: 'Angle',
              values: [{ id: 'studio', name: 'Studio' }],
            },
          ]
        : [],
    isLoading: false,
    failedTemplateIds: [],
  }),
}));

const draft = {
  id: 'gd_1',
  product_id: 'prod_1',
  provider: 'mock',
  model: 'mock-v1',
  execution_mode: 'mock',
  state: 'editing',
  revision: 3,
  selectors: {
    dimension_template_ids: ['lib_t'],
    seed: 7,
    product_entry_id: 'lib_p',
  },
  combination_strategy: { mode: 'sampled', sample_cap: 5 },
  format_config: { variants_per_combination: 2 },
  exclusions: {},
  allowed_actions: [{ action: 'update', allowed: true, reason_code: null }],
};

describe('DraftComposer (Issue #157)', () => {
  beforeEach(() => {
    mockExecute.mockReset();
    mockExecute.mockResolvedValue({
      kind: 'success',
      data: { revision: 4 },
      meta: {},
    });
  });

  test('no longer renders the dead Product dropdown or Seed input', () => {
    render(<DraftComposer draft={draft} onDraftMutated={jest.fn()} />);
    expect(document.getElementById('product-select')).toBeNull();
    expect(document.getElementById('seed-input')).toBeNull();
    expect(screen.queryByLabelText(/seed/i)).toBeNull();
  });

  test('passes the model variant ceiling from capabilities to the format field', () => {
    render(<DraftComposer draft={draft} onDraftMutated={jest.fn()} />);
    expect(document.getElementById('format-variant-count')).toHaveAttribute(
      'max',
      '4'
    );
    expect(
      screen.getByText('Max 4 per combination for this model')
    ).toBeInTheDocument();
  });

  test('rewrites a legacy strategy in the canonical shape through one CAS patch', async () => {
    const onDraftMutated = jest.fn();
    render(<DraftComposer draft={draft} onDraftMutated={onDraftMutated} />);

    fireEvent.click(screen.getByRole('button', { name: 'Full' }));

    await waitFor(() => expect(mockExecute).toHaveBeenCalledTimes(1));
    expect(mockExecute).toHaveBeenCalledWith(
      expect.objectContaining({
        pathParams: { draft_id: 'gd_1' },
        ifMatch: 3,
        body: { combination_strategy: { kind: 'full' } },
      })
    );
    await waitFor(() => expect(onDraftMutated).toHaveBeenCalledWith(4));
  });

  test('builds exclusions.rules from the selected template dimensions', async () => {
    render(<DraftComposer draft={draft} onDraftMutated={jest.fn()} />);

    fireEvent.change(document.getElementById('exclusion-dimension')!, {
      target: { value: 'lib_t:angle' },
    });
    fireEvent.change(document.getElementById('exclusion-value')!, {
      target: { value: 'studio' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add value' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add exclusion' }));

    await waitFor(() => expect(mockExecute).toHaveBeenCalledTimes(1));
    expect(mockExecute.mock.calls[0][0].body).toEqual({
      exclusions: {
        rules: [{ dimensions: { 'lib_t:angle': 'studio' }, reason: 'manual' }],
      },
    });
  });
});

describe('maxVariantsFromCapabilities', () => {
  test('reads the exact model limit and tolerates missing data', () => {
    expect(
      maxVariantsFromCapabilities(CAPABILITIES, 'meta', 'llama-image-v1')
    ).toBe(2);
    expect(
      maxVariantsFromCapabilities(CAPABILITIES, 'mock', 'unknown')
    ).toBeNull();
    expect(
      maxVariantsFromCapabilities(undefined, 'mock', 'mock-v1')
    ).toBeNull();
  });
});
