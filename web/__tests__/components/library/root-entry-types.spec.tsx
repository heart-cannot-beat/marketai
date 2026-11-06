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
import { PersonDraftEditor } from '@/components/library/entry-types/person/PersonDraftEditor';
import { PersonVersionViewer } from '@/components/library/entry-types/person/PersonVersionViewer';
import { DimensionTemplateDraftEditor } from '@/components/library/entry-types/dimension-template/DimensionTemplateDraftEditor';
import { DimensionTemplateVersionViewer } from '@/components/library/entry-types/dimension-template/DimensionTemplateVersionViewer';
import { PromptPolicyDraftEditor } from '@/components/library/entry-types/prompt-policy/PromptPolicyDraftEditor';
import { PromptPolicyVersionViewer } from '@/components/library/entry-types/prompt-policy/PromptPolicyVersionViewer';
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

// ── PersonDraftEditor ──────────────────────────────────────────────

describe('PersonDraftEditor', () => {
  it('returns null when buffer is null', () => {
    const session = makeSession({ buffer: null as any });
    const { container } = render(createElement(PersonDraftEditor, { session }));
    expect(container.innerHTML).toBe('');
  });

  it('renders person fields with correct values', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: 'Persona A',
          person_type: 'fictional',
          age_expression: '25-34',
          prompt_fragments: ['friendly', 'casual'],
          rights_regions: ['US', 'EU'],
        },
        _unknown: {},
      },
    });

    render(createElement(PersonDraftEditor, { session }));

    expect(screen.getByDisplayValue('Persona A')).toBeInTheDocument();
    expect(screen.getByDisplayValue('25-34')).toBeInTheDocument();
    expect(screen.getByDisplayValue('friendly')).toBeInTheDocument();
    expect(screen.getByDisplayValue('casual')).toBeInTheDocument();
    expect(screen.getByDisplayValue('US')).toBeInTheDocument();
    expect(screen.getByDisplayValue('EU')).toBeInTheDocument();
  });

  it('calls updateField on age_expression change', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: '',
          person_type: 'fictional',
          age_expression: '',
          prompt_fragments: [],
          rights_regions: [],
        },
        _unknown: {},
      },
    });

    render(createElement(PersonDraftEditor, { session }));

    const ageInput = screen
      .getByText('library:fields.person.age-expression')
      .closest('label')!
      .querySelector('input')!;
    fireEvent.change(ageInput, { target: { value: '18-24' } });
    expect(session.updateField).toHaveBeenCalledWith('age_expression', '18-24');
  });
});

describe('PersonVersionViewer', () => {
  it('renders version fields', () => {
    const version = {
      display_name: 'Persona B',
      person_type: 'licensed',
      age_expression: '30-44',
      prompt_fragments: ['formal'],
      rights_regions: ['UK'],
    };

    render(createElement(PersonVersionViewer, { version }));

    expect(screen.getByText('Persona B')).toBeInTheDocument();
    expect(screen.getByText('licensed')).toBeInTheDocument();
    expect(screen.getByText('30-44')).toBeInTheDocument();
    expect(screen.getByText('formal')).toBeInTheDocument();
    expect(screen.getByText('UK')).toBeInTheDocument();
  });

  it('shows fallback for empty fields', () => {
    const version = {
      display_name: '',
      person_type: '',
      age_expression: '',
      prompt_fragments: [],
      rights_regions: [],
    };

    render(createElement(PersonVersionViewer, { version }));

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

// ── PromptPolicyDraftEditor ────────────────────────────────────────

describe('PromptPolicyDraftEditor', () => {
  it('returns null when buffer is null', () => {
    const session = makeSession({ buffer: null as any });
    const { container } = render(
      createElement(PromptPolicyDraftEditor, { session })
    );
    expect(container.innerHTML).toBe('');
  });

  it('renders policy fields with correct values', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: 'Brand Policy',
          registered_tokens: ['{{product_name}}', '{{tagline}}'],
          locked_phrases: ['Always fresh'],
          brand_tone: 'Professional',
        },
        _unknown: {},
      },
    });

    render(createElement(PromptPolicyDraftEditor, { session }));

    expect(screen.getByDisplayValue('Brand Policy')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Professional')).toBeInTheDocument();
    expect(screen.getByDisplayValue('{{product_name}}')).toBeInTheDocument();
    expect(screen.getByDisplayValue('{{tagline}}')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Always fresh')).toBeInTheDocument();
  });

  it('calls updateField on brand_tone change', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: '',
          registered_tokens: [],
          locked_phrases: [],
          brand_tone: '',
        },
        _unknown: {},
      },
    });

    render(createElement(PromptPolicyDraftEditor, { session }));

    const toneInput = screen
      .getByText('library:fields.prompt-policy.brand-tone')
      .closest('label')!
      .querySelector('textarea')!;
    fireEvent.change(toneInput, { target: { value: 'Casual' } });
    expect(session.updateField).toHaveBeenCalledWith('brand_tone', 'Casual');
  });
});

describe('PromptPolicyVersionViewer', () => {
  it('renders version fields', () => {
    const version = {
      display_name: 'Brand Policy v1',
      registered_tokens: ['{{name}}'],
      locked_phrases: ['Stay cool'],
      brand_tone: 'Friendly',
    };

    render(createElement(PromptPolicyVersionViewer, { version }));

    expect(screen.getByText('Brand Policy v1')).toBeInTheDocument();
    expect(screen.getByText('{{name}}')).toBeInTheDocument();
    expect(screen.getByText('Stay cool')).toBeInTheDocument();
    expect(screen.getByText('Friendly')).toBeInTheDocument();
  });

  it('shows fallback for empty fields', () => {
    const version = {
      display_name: '',
      registered_tokens: [],
      locked_phrases: [],
      brand_tone: '',
    };

    render(createElement(PromptPolicyVersionViewer, { version }));

    const emptyMarkers = screen.getAllByText('library:fields.version.no-value');
    expect(emptyMarkers.length).toBeGreaterThanOrEqual(1);
  });
});

// ── DataSourceDraftEditor ──────────────────────────────────────────

describe('DataSourceDraftEditor', () => {
  it('returns null when buffer is null', () => {
    const session = makeSession({ buffer: null as any });
    const { container } = render(
      createElement(DataSourceDraftEditor, { session })
    );
    expect(container.innerHTML).toBe('');
  });

  it('renders data source fields with correct values', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: 'Sales Feed',
          adapter_kind: 'rest_api',
          execution_mode: 'scheduled',
          config: { base_url: 'https://api.example.com' },
        },
        _unknown: {},
      },
    });

    render(createElement(DataSourceDraftEditor, { session }));

    expect(screen.getByDisplayValue('Sales Feed')).toBeInTheDocument();
    expect(screen.getByDisplayValue('rest_api')).toBeInTheDocument();
  });

  it('calls updateField on adapter_kind change', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: '',
          adapter_kind: '',
          execution_mode: '',
          config: {},
        },
        _unknown: {},
      },
    });

    render(createElement(DataSourceDraftEditor, { session }));

    const adapterInput = screen
      .getByText('library:fields.data-source.adapter-kind')
      .closest('label')!
      .querySelector('input')!;
    fireEvent.change(adapterInput, { target: { value: 'graphql' } });
    expect(session.updateField).toHaveBeenCalledWith('adapter_kind', 'graphql');
  });

  it('renders config section', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: '',
          adapter_kind: '',
          execution_mode: '',
          config: { base_url: 'https://api.example.com' },
        },
        _unknown: {},
      },
    });

    render(createElement(DataSourceDraftEditor, { session }));

    expect(
      screen.getByText('library:fields.data-source.config')
    ).toBeInTheDocument();
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
