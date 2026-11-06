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
import { OfferDraftEditor } from '@/components/library/entry-types/offer/OfferDraftEditor';
import { OfferVersionViewer } from '@/components/library/entry-types/offer/OfferVersionViewer';
import { ReferenceGroupDraftEditor } from '@/components/library/entry-types/reference-group/ReferenceGroupDraftEditor';
import { ReferenceGroupVersionViewer } from '@/components/library/entry-types/reference-group/ReferenceGroupVersionViewer';
import { ReferenceSelectionRuleDraftEditor } from '@/components/library/entry-types/reference-selection-rule/ReferenceSelectionRuleDraftEditor';
import { ReferenceSelectionRuleVersionViewer } from '@/components/library/entry-types/reference-selection-rule/ReferenceSelectionRuleVersionViewer';
import { DimensionSchemaDraftEditor } from '@/components/library/entry-types/dimension-schema/DimensionSchemaDraftEditor';
import { DimensionSchemaVersionViewer } from '@/components/library/entry-types/dimension-schema/DimensionSchemaVersionViewer';
import { MarketSourceMappingDraftEditor } from '@/components/library/entry-types/market-source-mapping/MarketSourceMappingDraftEditor';
import { MarketSourceMappingVersionViewer } from '@/components/library/entry-types/market-source-mapping/MarketSourceMappingVersionViewer';
import { ManualFactSetDraftEditor } from '@/components/library/entry-types/manual-fact-set/ManualFactSetDraftEditor';
import { ManualFactSetVersionViewer } from '@/components/library/entry-types/manual-fact-set/ManualFactSetVersionViewer';
import { TruthResolutionPolicyDraftEditor } from '@/components/library/entry-types/truth-resolution-policy/TruthResolutionPolicyDraftEditor';
import { TruthResolutionPolicyVersionViewer } from '@/components/library/entry-types/truth-resolution-policy/TruthResolutionPolicyVersionViewer';
import { ReferenceAssetDraftEditor } from '@/components/library/entry-types/reference-asset/ReferenceAssetDraftEditor';
import { ReferenceAssetVersionViewer } from '@/components/library/entry-types/reference-asset/ReferenceAssetVersionViewer';

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

// ── OfferDraftEditor ───────────────────────────────────────────────

describe('OfferDraftEditor', () => {
  it('returns null when buffer is null', () => {
    const session = makeSession({ buffer: null as any });
    const { container } = render(createElement(OfferDraftEditor, { session }));
    expect(container.innerHTML).toBe('');
  });

  it('renders offer fields with correct values', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: 'Summer Sale',
          item_semantics: 'SKU',
          market_semantics: 'Region',
          claim_semantics: 'Discount',
          date_semantics: 'Valid from',
        },
        _unknown: {},
      },
    });

    render(createElement(OfferDraftEditor, { session }));

    expect(screen.getByDisplayValue('Summer Sale')).toBeInTheDocument();
    expect(screen.getByDisplayValue('SKU')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Region')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Discount')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Valid from')).toBeInTheDocument();
  });

  it('calls updateField on item_semantics change', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: '',
          item_semantics: '',
          market_semantics: '',
          claim_semantics: '',
          date_semantics: '',
        },
        _unknown: {},
      },
    });

    render(createElement(OfferDraftEditor, { session }));

    const input = screen
      .getByText('library:fields.offer.item-semantics')
      .closest('label')!
      .querySelector('input')!;
    fireEvent.change(input, { target: { value: 'Product ID' } });
    expect(session.updateField).toHaveBeenCalledWith(
      'item_semantics',
      'Product ID'
    );
  });
});

describe('OfferVersionViewer', () => {
  it('renders version fields', () => {
    const version = {
      display_name: 'Summer Sale',
      item_semantics: 'SKU',
      market_semantics: 'Region',
      claim_semantics: 'Discount',
      date_semantics: 'Valid from',
    };

    render(createElement(OfferVersionViewer, { version }));

    expect(screen.getByText('Summer Sale')).toBeInTheDocument();
    expect(screen.getByText('SKU')).toBeInTheDocument();
    expect(screen.getByText('Region')).toBeInTheDocument();
  });

  it('shows fallback for empty fields', () => {
    const version = {
      display_name: '',
      item_semantics: '',
      market_semantics: '',
      claim_semantics: '',
      date_semantics: '',
    };

    render(createElement(OfferVersionViewer, { version }));

    const fallbacks = screen.getAllByText('library:fields.version.no-value');
    expect(fallbacks.length).toBe(5);
  });
});

// ── ReferenceGroupDraftEditor ──────────────────────────────────────

describe('ReferenceGroupDraftEditor', () => {
  it('returns null when buffer is null', () => {
    const session = makeSession({ buffer: null as any });
    const { container } = render(
      createElement(ReferenceGroupDraftEditor, { session })
    );
    expect(container.innerHTML).toBe('');
  });

  it('renders group fields with correct values', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: 'Hero Shots',
          group_intent: 'Primary product imagery',
        },
        _unknown: {},
      },
    });

    render(createElement(ReferenceGroupDraftEditor, { session }));

    expect(screen.getByDisplayValue('Hero Shots')).toBeInTheDocument();
    expect(
      screen.getByDisplayValue('Primary product imagery')
    ).toBeInTheDocument();
  });

  it('calls updateField on group_intent change', () => {
    const session = makeSession({
      buffer: { fields: { display_name: '', group_intent: '' }, _unknown: {} },
    });

    render(createElement(ReferenceGroupDraftEditor, { session }));

    const textarea = screen
      .getByText('library:fields.reference-group.group-intent')
      .closest('label')!
      .querySelector('textarea')!;
    fireEvent.change(textarea, { target: { value: 'Updated intent' } });
    expect(session.updateField).toHaveBeenCalledWith(
      'group_intent',
      'Updated intent'
    );
  });
});

describe('ReferenceGroupVersionViewer', () => {
  it('renders version fields', () => {
    const version = {
      display_name: 'Hero Shots',
      group_intent: 'Primary imagery',
    };
    render(createElement(ReferenceGroupVersionViewer, { version }));

    expect(screen.getByText('Hero Shots')).toBeInTheDocument();
    expect(screen.getByText('Primary imagery')).toBeInTheDocument();
  });

  it('shows fallback for empty fields', () => {
    const version = { display_name: '', group_intent: '' };
    render(createElement(ReferenceGroupVersionViewer, { version }));

    const fallbacks = screen.getAllByText('library:fields.version.no-value');
    expect(fallbacks.length).toBe(2);
  });
});

// ── ReferenceSelectionRuleDraftEditor ──────────────────────────────

describe('ReferenceSelectionRuleDraftEditor', () => {
  it('returns null when buffer is null', () => {
    const session = makeSession({ buffer: null as any });
    const { container } = render(
      createElement(ReferenceSelectionRuleDraftEditor, { session })
    );
    expect(container.innerHTML).toBe('');
  });

  it('renders rule fields with correct values', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: 'Aspect match',
          rule_expression: 'aspect == "front"',
        },
        _unknown: {},
      },
    });

    render(createElement(ReferenceSelectionRuleDraftEditor, { session }));

    expect(screen.getByDisplayValue('Aspect match')).toBeInTheDocument();
    expect(screen.getByDisplayValue('aspect == "front"')).toBeInTheDocument();
  });
});

describe('ReferenceSelectionRuleVersionViewer', () => {
  it('renders version fields', () => {
    const version = {
      display_name: 'Aspect match',
      rule_expression: 'aspect == "front"',
    };
    render(createElement(ReferenceSelectionRuleVersionViewer, { version }));

    expect(screen.getByText('Aspect match')).toBeInTheDocument();
    expect(screen.getByText('aspect == "front"')).toBeInTheDocument();
  });

  it('shows fallback for empty fields', () => {
    const version = { display_name: '', rule_expression: '' };
    render(createElement(ReferenceSelectionRuleVersionViewer, { version }));

    const fallbacks = screen.getAllByText('library:fields.version.no-value');
    expect(fallbacks.length).toBe(2);
  });
});

// ── DimensionSchemaDraftEditor ─────────────────────────────────────

describe('DimensionSchemaDraftEditor', () => {
  it('returns null when buffer is null', () => {
    const session = makeSession({ buffer: null as any });
    const { container } = render(
      createElement(DimensionSchemaDraftEditor, { session })
    );
    expect(container.innerHTML).toBe('');
  });

  it('renders schema fields with dimensions including constraints', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: 'Color Schema',
          dimensions: [
            {
              id: 'd1',
              name: 'Color',
              values: ['Red', 'Blue'],
              constraints: { max: 3 },
            },
          ],
        },
        _unknown: {},
      },
    });

    render(createElement(DimensionSchemaDraftEditor, { session }));

    expect(screen.getByDisplayValue('Color Schema')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Color')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Red, Blue')).toBeInTheDocument();
    expect(
      screen.getByText('library:fields.dimension-schema.constraints')
    ).toBeInTheDocument();
  });

  it('renders add dimension button', () => {
    const session = makeSession({
      buffer: { fields: { display_name: '', dimensions: [] }, _unknown: {} },
    });

    render(createElement(DimensionSchemaDraftEditor, { session }));

    expect(
      screen.getByText('library:fields.dimension-schema.add-dimension')
    ).toBeInTheDocument();
  });
});

describe('DimensionSchemaVersionViewer', () => {
  it('renders version fields with dimensions', () => {
    const version = {
      display_name: 'Size Schema',
      dimensions: [
        {
          id: 'd1',
          name: 'Size',
          values: ['S', 'M', 'L'],
          constraints: { required: true },
        },
      ],
    };

    render(createElement(DimensionSchemaVersionViewer, { version }));

    expect(screen.getByText('Size Schema')).toBeInTheDocument();
    expect(screen.getByText('Size')).toBeInTheDocument();
    expect(screen.getByText('S, M, L')).toBeInTheDocument();
  });

  it('shows fallback for empty fields', () => {
    const version = { display_name: '', dimensions: [] };
    render(createElement(DimensionSchemaVersionViewer, { version }));

    const fallbacks = screen.getAllByText('library:fields.version.no-value');
    expect(fallbacks.length).toBeGreaterThanOrEqual(1);
  });
});

// ── MarketSourceMappingDraftEditor ─────────────────────────────────

describe('MarketSourceMappingDraftEditor', () => {
  it('returns null when buffer is null', () => {
    const session = makeSession({ buffer: null as any });
    const { container } = render(
      createElement(MarketSourceMappingDraftEditor, { session })
    );
    expect(container.innerHTML).toBe('');
  });

  it('renders mapping fields with correct values', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: 'US Feed',
          external_object_id: 'ext-123',
          market_code: 'US',
          execution_mode: 'scheduled',
        },
        _unknown: {},
      },
    });

    render(createElement(MarketSourceMappingDraftEditor, { session }));

    expect(screen.getByDisplayValue('US Feed')).toBeInTheDocument();
    expect(screen.getByDisplayValue('ext-123')).toBeInTheDocument();
    expect(screen.getByDisplayValue('US')).toBeInTheDocument();
  });

  it('calls updateField on market_code change', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: '',
          external_object_id: '',
          market_code: '',
          execution_mode: '',
        },
        _unknown: {},
      },
    });

    render(createElement(MarketSourceMappingDraftEditor, { session }));

    const input = screen
      .getByText('library:fields.market-source-mapping.market-code')
      .closest('label')!
      .querySelector('input')!;
    fireEvent.change(input, { target: { value: 'EU' } });
    expect(session.updateField).toHaveBeenCalledWith('market_code', 'EU');
  });
});

describe('MarketSourceMappingVersionViewer', () => {
  it('renders version fields', () => {
    const version = {
      display_name: 'US Feed',
      external_object_id: 'ext-123',
      market_code: 'US',
      execution_mode: 'scheduled',
    };

    render(createElement(MarketSourceMappingVersionViewer, { version }));

    expect(screen.getByText('US Feed')).toBeInTheDocument();
    expect(screen.getByText('ext-123')).toBeInTheDocument();
    expect(screen.getByText('US')).toBeInTheDocument();
    expect(screen.getByText('scheduled')).toBeInTheDocument();
  });

  it('shows fallback for empty fields', () => {
    const version = {
      display_name: '',
      external_object_id: '',
      market_code: '',
      execution_mode: '',
    };

    render(createElement(MarketSourceMappingVersionViewer, { version }));

    const fallbacks = screen.getAllByText('library:fields.version.no-value');
    expect(fallbacks.length).toBe(4);
  });
});

// ── ManualFactSetDraftEditor ───────────────────────────────────────

describe('ManualFactSetDraftEditor', () => {
  it('returns null when buffer is null', () => {
    const session = makeSession({ buffer: null as any });
    const { container } = render(
      createElement(ManualFactSetDraftEditor, { session })
    );
    expect(container.innerHTML).toBe('');
  });

  it('renders fact set fields with facts', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: 'Product Facts',
          facts: [{ id: 'f1', key: 'material', value: 'cotton' }],
        },
        _unknown: {},
      },
    });

    render(createElement(ManualFactSetDraftEditor, { session }));

    expect(screen.getByDisplayValue('Product Facts')).toBeInTheDocument();
    expect(screen.getByDisplayValue('material')).toBeInTheDocument();
    expect(screen.getByDisplayValue('cotton')).toBeInTheDocument();
  });

  it('renders add fact button', () => {
    const session = makeSession({
      buffer: { fields: { display_name: '', facts: [] }, _unknown: {} },
    });

    render(createElement(ManualFactSetDraftEditor, { session }));

    expect(
      screen.getByText('library:fields.manual-fact-set.add-fact')
    ).toBeInTheDocument();
  });
});

describe('ManualFactSetVersionViewer', () => {
  it('renders version fields with facts', () => {
    const version = {
      display_name: 'Product Facts',
      facts: [
        { id: 'f1', key: 'material', value: 'cotton' },
        { id: 'f2', key: 'origin', value: 'USA' },
      ],
    };

    render(createElement(ManualFactSetVersionViewer, { version }));

    expect(screen.getByText('Product Facts')).toBeInTheDocument();
    expect(screen.getByText('material')).toBeInTheDocument();
    expect(screen.getByText('cotton')).toBeInTheDocument();
    expect(screen.getByText('origin')).toBeInTheDocument();
    expect(screen.getByText('USA')).toBeInTheDocument();
  });

  it('shows fallback for empty fields', () => {
    const version = { display_name: '', facts: [] };
    render(createElement(ManualFactSetVersionViewer, { version }));

    const fallbacks = screen.getAllByText('library:fields.version.no-value');
    expect(fallbacks.length).toBeGreaterThanOrEqual(1);
  });
});

// ── TruthResolutionPolicyDraftEditor ───────────────────────────────

describe('TruthResolutionPolicyDraftEditor', () => {
  it('returns null when buffer is null', () => {
    const session = makeSession({ buffer: null as any });
    const { container } = render(
      createElement(TruthResolutionPolicyDraftEditor, { session })
    );
    expect(container.innerHTML).toBe('');
  });

  it('renders policy fields with correct values', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: 'Default Resolution',
          priority_order: ['manual', 'api'],
          conflict_strategy: 'first_wins',
        },
        _unknown: {},
      },
    });

    render(createElement(TruthResolutionPolicyDraftEditor, { session }));

    expect(screen.getByDisplayValue('Default Resolution')).toBeInTheDocument();
    expect(screen.getByDisplayValue('manual')).toBeInTheDocument();
    expect(screen.getByDisplayValue('api')).toBeInTheDocument();
  });

  it('renders add priority button', () => {
    const session = makeSession({
      buffer: {
        fields: { display_name: '', priority_order: [], conflict_strategy: '' },
        _unknown: {},
      },
    });

    render(createElement(TruthResolutionPolicyDraftEditor, { session }));

    expect(
      screen.getByText('library:fields.truth-resolution-policy.add-priority')
    ).toBeInTheDocument();
  });
});

describe('TruthResolutionPolicyVersionViewer', () => {
  it('renders version fields', () => {
    const version = {
      display_name: 'Default Resolution',
      priority_order: ['manual', 'api'],
      conflict_strategy: 'first_wins',
    };

    render(createElement(TruthResolutionPolicyVersionViewer, { version }));

    expect(screen.getByText('Default Resolution')).toBeInTheDocument();
    expect(screen.getByText('manual')).toBeInTheDocument();
    expect(screen.getByText('api')).toBeInTheDocument();
    expect(screen.getByText('first_wins')).toBeInTheDocument();
  });

  it('shows fallback for empty fields', () => {
    const version = {
      display_name: '',
      priority_order: [],
      conflict_strategy: '',
    };

    render(createElement(TruthResolutionPolicyVersionViewer, { version }));

    const fallbacks = screen.getAllByText('library:fields.version.no-value');
    expect(fallbacks.length).toBeGreaterThanOrEqual(1);
  });
});

// ── ReferenceAssetDraftEditor ──────────────────────────────────────

describe('ReferenceAssetDraftEditor', () => {
  it('returns null when buffer is null', () => {
    const session = makeSession({ buffer: null as any });
    const { container } = render(
      createElement(ReferenceAssetDraftEditor, { session })
    );
    expect(container.innerHTML).toBe('');
  });

  it('renders asset fields with correct values', () => {
    const session = makeSession({
      buffer: {
        fields: {
          display_name: 'Hero Image',
          asset_purpose: 'Primary product photo',
        },
        _unknown: {},
      },
    });

    render(createElement(ReferenceAssetDraftEditor, { session }));

    expect(screen.getByDisplayValue('Hero Image')).toBeInTheDocument();
    expect(
      screen.getByDisplayValue('Primary product photo')
    ).toBeInTheDocument();
  });

  it('calls updateField on asset_purpose change', () => {
    const session = makeSession({
      buffer: { fields: { display_name: '', asset_purpose: '' }, _unknown: {} },
    });

    render(createElement(ReferenceAssetDraftEditor, { session }));

    const textarea = screen
      .getByText('library:fields.reference-asset.asset-purpose')
      .closest('label')!
      .querySelector('textarea')!;
    fireEvent.change(textarea, { target: { value: 'Updated purpose' } });
    expect(session.updateField).toHaveBeenCalledWith(
      'asset_purpose',
      'Updated purpose'
    );
  });
});

describe('ReferenceAssetVersionViewer', () => {
  it('renders version fields', () => {
    const version = {
      display_name: 'Hero Image',
      asset_purpose: 'Primary photo',
    };
    render(createElement(ReferenceAssetVersionViewer, { version }));

    expect(screen.getByText('Hero Image')).toBeInTheDocument();
    expect(screen.getByText('Primary photo')).toBeInTheDocument();
  });

  it('shows fallback for empty fields', () => {
    const version = { display_name: '', asset_purpose: '' };
    render(createElement(ReferenceAssetVersionViewer, { version }));

    const fallbacks = screen.getAllByText('library:fields.version.no-value');
    expect(fallbacks.length).toBe(2);
  });
});
