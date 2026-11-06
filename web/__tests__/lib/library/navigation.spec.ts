import {
  LIBRARY_CATEGORIES,
  LIBRARY_ENTRY_TYPES,
  TOP_LEVEL_ENTRY_TYPES,
  type LibraryCategory,
  type LibraryEntryType,
  type TopLevelEntryType,
  getPlacement,
  isTopLevel,
  isInternal,
  rootTypeForCategory,
  categoryForTopLevelEntryType,
} from '@/lib/library/navigation';

describe('navigation — categories and types', () => {
  it('defines exactly 5 categories', () => {
    expect(LIBRARY_CATEGORIES).toHaveLength(5);
    expect([...LIBRARY_CATEGORIES]).toEqual([
      'product',
      'person',
      'template',
      'prompt_policy',
      'datasource',
    ]);
  });

  it('defines exactly 13 entry types', () => {
    expect(LIBRARY_ENTRY_TYPES).toHaveLength(13);
  });

  it('defines exactly 5 top-level entry types', () => {
    expect(TOP_LEVEL_ENTRY_TYPES).toHaveLength(5);
  });

  it('every top-level type is in the full entry type list', () => {
    for (const tl of TOP_LEVEL_ENTRY_TYPES) {
      expect((LIBRARY_ENTRY_TYPES as readonly string[]).includes(tl)).toBe(
        true
      );
    }
  });
});

describe('navigation — placement map', () => {
  const topLevel: TopLevelEntryType[] = [
    'product',
    'person',
    'dimension_template',
    'prompt_policy',
    'data_source',
  ];

  const internal: LibraryEntryType[] = [
    'offer',
    'reference_group',
    'reference_selection_rule',
    'dimension_schema',
    'market_source_mapping',
    'reference_asset',
    'manual_fact_set',
    'truth_resolution_policy',
  ];

  it.each(topLevel)('%s is top_level', (et) => {
    const p = getPlacement(et);
    expect(p.kind).toBe('top_level');
    expect(isTopLevel(et)).toBe(true);
    expect(isInternal(et)).toBe(false);
  });

  it.each(internal)('%s is internal', (et) => {
    const p = getPlacement(et);
    expect(p.kind).toBe('internal');
    expect(isTopLevel(et)).toBe(false);
    expect(isInternal(et)).toBe(true);
  });

  it('every entry type has a placement', () => {
    for (const et of LIBRARY_ENTRY_TYPES) {
      expect(getPlacement(et)).toBeDefined();
      expect(['top_level', 'internal']).toContain(getPlacement(et).kind);
    }
  });

  it('top-level + internal = all 13 types', () => {
    const tl = LIBRARY_ENTRY_TYPES.filter(isTopLevel);
    const int = LIBRARY_ENTRY_TYPES.filter(isInternal);
    expect(tl.length + int.length).toBe(13);
    expect(new Set([...tl, ...int]).size).toBe(13);
  });
});

describe('navigation — category/root-type bijection', () => {
  const mapping: [LibraryCategory, TopLevelEntryType][] = [
    ['product', 'product'],
    ['person', 'person'],
    ['template', 'dimension_template'],
    ['prompt_policy', 'prompt_policy'],
    ['datasource', 'data_source'],
  ];

  it.each(mapping)('rootTypeForCategory(%s) = %s', (cat, rootType) => {
    expect(rootTypeForCategory(cat)).toBe(rootType);
  });

  it.each(mapping)('categoryForTopLevelEntryType(%s) = %s', (cat, rootType) => {
    expect(categoryForTopLevelEntryType(rootType)).toBe(cat);
  });

  it('round-trips all categories', () => {
    for (const cat of LIBRARY_CATEGORIES) {
      const rt = rootTypeForCategory(cat);
      expect(categoryForTopLevelEntryType(rt)).toBe(cat);
    }
  });
});

describe('navigation — internal context placement', () => {
  it('offer belongs only to product', () => {
    const p = getPlacement('offer');
    expect(p.kind).toBe('internal');
    if (p.kind === 'internal') {
      expect(p.canonicalHostCategory).toBe('product');
      expect(p.allowedContextCategories).toEqual(['product']);
      expect(p.parentKinds).toEqual(['product']);
    }
  });

  it('reference_asset is shared across product and person', () => {
    const p = getPlacement('reference_asset');
    expect(p.kind).toBe('internal');
    if (p.kind === 'internal') {
      expect(p.canonicalHostCategory).toBe('product');
      expect(p.allowedContextCategories).toContain('product');
      expect(p.allowedContextCategories).toContain('person');
      expect(p.allowedContextCategories).toHaveLength(2);
    }
  });

  it('dimension_schema belongs to product context (per CONTEXT_REGISTRY)', () => {
    const p = getPlacement('dimension_schema');
    expect(p.kind).toBe('internal');
    if (p.kind === 'internal') {
      expect(p.canonicalHostCategory).toBe('product');
      expect(p.allowedContextCategories).toEqual(['product']);
    }
  });

  it('manual_fact_set and truth_resolution_policy belong to datasource', () => {
    for (const et of [
      'manual_fact_set',
      'truth_resolution_policy',
    ] as LibraryEntryType[]) {
      const p = getPlacement(et);
      expect(p.kind).toBe('internal');
      if (p.kind === 'internal') {
        expect(p.canonicalHostCategory).toBe('datasource');
        expect(p.allowedContextCategories).toEqual(['datasource']);
      }
    }
  });

  it('market_source_mapping parent is offer', () => {
    const p = getPlacement('market_source_mapping');
    if (p.kind === 'internal') {
      expect(p.parentKinds).toEqual(['offer']);
    }
  });

  it('reference_selection_rule parent is reference_group', () => {
    const p = getPlacement('reference_selection_rule');
    if (p.kind === 'internal') {
      expect(p.parentKinds).toEqual(['reference_group']);
    }
  });
});
