/**
 * @jest-environment jsdom
 */
import { createElement } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

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

import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import { ProductDraftEditor } from '@/components/library/entry-types/product/ProductDraftEditor';
import { ProductVersionViewer } from '@/components/library/entry-types/product/ProductVersionViewer';
import { DimensionTemplateDraftEditor } from '@/components/library/entry-types/dimension-template/DimensionTemplateDraftEditor';
import { DimensionTemplateVersionViewer } from '@/components/library/entry-types/dimension-template/DimensionTemplateVersionViewer';
import { DataSourceDraftEditor } from '@/components/library/entry-types/data-source/DataSourceDraftEditor';
import { DataSourceVersionViewer } from '@/components/library/entry-types/data-source/DataSourceVersionViewer';

function makeSession(
  overrides?: Partial<UseDraftSessionResult>
): UseDraftSessionResult {
  return {
    isSupported: true,
    buffer: { fields: {}, _unknown: {} },
    isDirty: false,
    isSaving: false,
    saveProblem: undefined,
    isValidating: false,
    validation: null,
    conflict: null,
    updateField: jest.fn(),
    updateFields: jest.fn(),
    replaceContent: jest.fn(),
    save: jest.fn(async () => true),
    validate: jest.fn(async () => true),
    acknowledgeWarning: jest.fn(),
    resolveConflictWithServer: jest.fn(),
    resolveConflictWithLocal: jest.fn(),
    cancelConflict: jest.fn(),
    discard: jest.fn(),
    canPublish: jest.fn(() => false),
    ...overrides,
  };
}

// ── ProductDraftEditor ─────────────────────────────────────────────

describe('ProductDraftEditor', () => {
  it('returns null when buffer is null', () => {
    const session = makeSession({ buffer: null as any });
    const { container } = render(
      createElement(ProductDraftEditor, { session })
    );
    expect(container.innerHTML).toBe('');
  });

  it('renders product fields with correct values', () => {
    const session = makeSession({
      buffer: {
        fields: {
          name: 'Widget Pro',
          description: 'A great widget',
          audience: 'Developers',
          selling_points: ['Fast', 'Reliable'],
        },
        _unknown: {},
      },
    });

    render(createElement(ProductDraftEditor, { session }));

    expect(screen.getByDisplayValue('Widget Pro')).toBeInTheDocument();
    expect(screen.getByDisplayValue('A great widget')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Developers')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Fast')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Reliable')).toBeInTheDocument();
  });

  it('calls updateField on description change', () => {
    const session = makeSession({
      buffer: {
        fields: {
          name: 'W',
          description: '',
          audience: '',
          selling_points: [],
        },
        _unknown: {},
      },
    });

    render(createElement(ProductDraftEditor, { session }));

    const descInput = screen
      .getByText('library:fields.product.description')
      .closest('label')!
      .querySelector('textarea')!;
    fireEvent.change(descInput, { target: { value: 'New desc' } });
    expect(session.updateField).toHaveBeenCalledWith('description', 'New desc');
  });

  it('renders selling points with add button', () => {
    const session = makeSession({
      buffer: {
        fields: {
          name: '',
          description: '',
          audience: '',
          selling_points: ['Point A'],
        },
        _unknown: {},
      },
    });

    render(createElement(ProductDraftEditor, { session }));

    expect(screen.getByDisplayValue('Point A')).toBeInTheDocument();
    expect(
      screen.getByText('library:fields.product.add-selling-point')
    ).toBeInTheDocument();
  });
});

describe('ProductVersionViewer', () => {
  it('renders version fields', () => {
    const version = {
      name: 'Widget Pro',
      description: 'A great product',
      audience: 'Everyone',
      selling_points: ['Fast', 'Reliable'],
    };

    render(createElement(ProductVersionViewer, { version }));

    expect(screen.getByText('Widget Pro')).toBeInTheDocument();
    expect(screen.getByText('A great product')).toBeInTheDocument();
    expect(screen.getByText('Everyone')).toBeInTheDocument();
    expect(screen.getByText('Fast')).toBeInTheDocument();
    expect(screen.getByText('Reliable')).toBeInTheDocument();
  });

  it('shows fallback for empty fields', () => {
    const version = {
      name: '',
      description: '',
      audience: '',
      selling_points: [],
    };

    render(createElement(ProductVersionViewer, { version }));

    const emptyMarkers = screen.getAllByText('library:fields.version.no-value');
    expect(emptyMarkers.length).toBeGreaterThanOrEqual(1);
  });
});

// ── DimensionTemplateDraftEditor ───────────────────────────────────

describe('DimensionTemplateDraftEditor', () => {
  it('returns null when buffer is null', () => {
    const session = makeSession({ buffer: null as any });
    const { container } = render(
      createElement(DimensionTemplateDraftEditor, { session })
    );
    expect(container.innerHTML).toBe('');
  });

  it('renders dimensions in the server shape (value objects with fragments)', () => {
    const session = makeSession({
      buffer: {
        fields: {
          dimensions: [
            {
              id: 'size',
              name: 'Size',
              values: [
                {
                  id: 'S',
                  positive_fragments: ['small build', 'compact'],
                  negative_fragments: [],
                },
                {
                  id: 'M',
                  positive_fragments: [],
                  negative_fragments: ['huge'],
                },
              ],
            },
          ],
          constraints: [
            { type: 'max_occurrence', dimension_id: 'size', max: 1 },
          ],
          combination_cardinality: 'one_per_dimension',
        },
        _unknown: {},
      },
    });

    render(createElement(DimensionTemplateDraftEditor, { session }));

    expect(screen.getByDisplayValue('Size')).toBeInTheDocument();
    expect(screen.getByDisplayValue('size')).toBeInTheDocument();
    expect(screen.getByDisplayValue('S')).toBeInTheDocument();
    expect(screen.getByDisplayValue('M')).toBeInTheDocument();
    expect(screen.getByDisplayValue(/small build/)).toHaveValue(
      'small build\ncompact'
    );
    expect(screen.getByDisplayValue('huge')).toBeInTheDocument();
    expect(screen.getByText('one_per_dimension')).toBeInTheDocument();
    expect(
      screen.getByText(/library:fields\.dimension-template\.constraints-count/)
    ).toBeInTheDocument();
  });

  it('writes a value edit back as a value object', () => {
    const session = makeSession({
      buffer: {
        fields: {
          dimensions: [
            {
              id: 'size',
              name: 'Size',
              values: [
                { id: 'S', positive_fragments: [], negative_fragments: [] },
              ],
            },
          ],
          constraints: [],
          combination_cardinality: 'one_per_dimension',
        },
        _unknown: {},
      },
    });

    render(createElement(DimensionTemplateDraftEditor, { session }));

    fireEvent.change(screen.getByDisplayValue('S'), {
      target: { value: 'XS' },
    });
    expect(session.updateField).toHaveBeenCalledWith('dimensions', [
      {
        id: 'size',
        name: 'Size',
        values: [{ id: 'XS', positive_fragments: [], negative_fragments: [] }],
      },
    ]);

    fireEvent.click(
      screen.getByText('library:fields.dimension-template.add-value')
    );
    expect(session.updateField).toHaveBeenLastCalledWith('dimensions', [
      {
        id: 'size',
        name: 'Size',
        values: [
          { id: 'S', positive_fragments: [], negative_fragments: [] },
          { id: '', positive_fragments: [], negative_fragments: [] },
        ],
      },
    ]);
  });

  it('renders add dimension button', () => {
    const session = makeSession({
      buffer: {
        fields: {
          dimensions: [],
          constraints: [],
          combination_cardinality: 'one_per_dimension',
        },
        _unknown: {},
      },
    });

    render(createElement(DimensionTemplateDraftEditor, { session }));

    expect(
      screen.getByText('library:fields.dimension-template.add-dimension')
    ).toBeInTheDocument();
  });
});

describe('DimensionTemplateVersionViewer', () => {
  it('renders version fields with dimensions', () => {
    const version = {
      dimensions: [
        {
          id: 'color',
          name: 'Color',
          values: [
            { id: 'Red', positive_fragments: [], negative_fragments: [] },
            { id: 'Blue', positive_fragments: [], negative_fragments: [] },
          ],
        },
      ],
      combination_cardinality: 'one_per_dimension',
    };

    render(createElement(DimensionTemplateVersionViewer, { version }));

    expect(screen.getByText('Color')).toBeInTheDocument();
    expect(screen.getByText('Red, Blue')).toBeInTheDocument();
    expect(screen.getByText('one_per_dimension')).toBeInTheDocument();
  });

  it('shows fallback for empty fields', () => {
    const version = { dimensions: [] };

    render(createElement(DimensionTemplateVersionViewer, { version }));

    const emptyMarkers = screen.getAllByText('library:fields.version.no-value');
    expect(emptyMarkers.length).toBeGreaterThanOrEqual(1);
  });
});

// ── DataSourceDraftEditor ──────────────────────────────────────────

describe('DataSourceDraftEditor', () => {
  const sourceSession = (fields: Record<string, unknown>) =>
    makeSession({
      buffer: {
        fields: { adapter_kind: '', execution_mode: '', config: {}, ...fields },
        _unknown: {},
      },
    });
  const sourceSelect = () =>
    screen.getByLabelText(
      'library:fields.data-source.adapter-kind'
    ) as HTMLSelectElement;

  it('returns null when buffer is null', () => {
    const session = makeSession({ buffer: null as any });
    const { container } = render(
      createElement(DataSourceDraftEditor, { session })
    );
    expect(container.innerHTML).toBe('');
  });

  it('offers the server source types as a list, Shopify disabled', () => {
    render(
      createElement(DataSourceDraftEditor, { session: sourceSession({}) })
    );

    const options = Array.from(sourceSelect().options).map((option) => [
      option.value,
      option.disabled,
    ]);
    expect(options).toEqual([
      ['', true],
      ['manual', false],
      ['mock', false],
      ['shopify', true],
    ]);
    // No name box and no execution mode list: the entry owns the name and
    // the source type sets the mode.
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.getAllByRole('combobox')).toHaveLength(1);
  });

  it.each([
    ['manual', { adapter_kind: 'manual', execution_mode: 'real' }],
    ['mock', { adapter_kind: 'mock', execution_mode: 'mock' }],
  ])('writes %s together with its execution mode', (kind, written) => {
    const session = sourceSession({});
    render(createElement(DataSourceDraftEditor, { session }));

    fireEvent.change(sourceSelect(), { target: { value: kind } });
    expect(session.updateFields).toHaveBeenCalledWith(written);
    expect(session.updateField).not.toHaveBeenCalled();
  });

  it('marks a controlled Mock source with the controlled Mock badge', () => {
    render(
      createElement(DataSourceDraftEditor, {
        session: sourceSession({
          adapter_kind: 'mock',
          execution_mode: 'mock',
        }),
      })
    );
    expect(screen.getByTestId('execution-mode-badge')).toHaveAttribute(
      'data-mode',
      'controlled-mock'
    );
  });

  it('shows a stored Shopify source as chosen and real', () => {
    render(
      createElement(DataSourceDraftEditor, {
        session: sourceSession({
          adapter_kind: 'shopify',
          execution_mode: 'real',
        }),
      })
    );
    expect(sourceSelect().value).toBe('shopify');
    expect(screen.getByTestId('execution-mode-badge')).toHaveAttribute(
      'data-mode',
      'real'
    );
  });

  it('prompts to choose again for a stored value it does not offer, without editing', () => {
    const session = sourceSession({
      adapter_kind: 'Shopify',
      execution_mode: 'scheduled',
    });
    render(createElement(DataSourceDraftEditor, { session }));

    expect(sourceSelect().value).toBe('');
    expect(
      screen.getByText('library:fields.data-source.adapter-invalid')
    ).toBeInTheDocument();
    expect(
      screen.queryByText('library:fields.data-source.execution-mode-mismatch')
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('execution-mode-badge')
    ).not.toBeInTheDocument();
    expect(session.updateField).not.toHaveBeenCalled();
    expect(session.updateFields).not.toHaveBeenCalled();
  });

  it('prompts to choose again when the stored mode does not match', () => {
    const session = sourceSession({
      adapter_kind: 'manual',
      execution_mode: 'mock',
    });
    render(createElement(DataSourceDraftEditor, { session }));

    expect(sourceSelect().value).toBe('manual');
    expect(
      screen.getByText('library:fields.data-source.execution-mode-mismatch')
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId('execution-mode-badge')
    ).not.toBeInTheDocument();
    expect(session.updateFields).not.toHaveBeenCalled();
  });

  it('renders config section', () => {
    render(
      createElement(DataSourceDraftEditor, {
        session: sourceSession({
          config: { mock_scenario: 'full' },
        }),
      })
    );

    expect(
      screen.getByText('library:fields.data-source.config')
    ).toBeInTheDocument();
    expect(screen.getByText(/mock_scenario/)).toBeInTheDocument();
  });
});

describe('DataSourceVersionViewer', () => {
  it('renders version fields', () => {
    const version = {
      display_name: 'Sales Feed v1',
      adapter_kind: 'rest_api',
      execution_mode: 'scheduled',
      config: { base_url: 'https://api.example.com' },
    };

    render(createElement(DataSourceVersionViewer, { version }));

    expect(screen.getByText('Sales Feed v1')).toBeInTheDocument();
    expect(screen.getByText('rest_api')).toBeInTheDocument();
    expect(screen.getByText('scheduled')).toBeInTheDocument();
  });

  it('shows fallback for empty fields', () => {
    const version = {
      display_name: '',
      adapter_kind: '',
      execution_mode: '',
      config: {},
    };

    render(createElement(DataSourceVersionViewer, { version }));

    const emptyMarkers = screen.getAllByText('library:fields.version.no-value');
    expect(emptyMarkers.length).toBeGreaterThanOrEqual(1);
  });
});
