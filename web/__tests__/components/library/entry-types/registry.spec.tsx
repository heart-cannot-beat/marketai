import {
  getEntryTypeDefinition,
  getDefinitionsForCategory,
  getTopLevelDefinitions,
  getAllDefinitions,
} from '@/components/library/entry-types/registry';
import {
  LIBRARY_ENTRY_TYPES,
  LIBRARY_CATEGORIES,
  type LibraryCategory,
  type LibraryEntryType,
  isTopLevel,
} from '@/lib/library/navigation';

describe('registry — exhaustive coverage', () => {
  it('has a definition for every entry type', () => {
    for (const et of LIBRARY_ENTRY_TYPES) {
      const def = getEntryTypeDefinition(et);
      expect(def).toBeDefined();
      expect(def.entryType).toBe(et);
    }
  });

  it('getAllDefinitions returns exactly 13 definitions', () => {
    expect(getAllDefinitions()).toHaveLength(13);
  });

  it('every definition has required fields', () => {
    for (const def of getAllDefinitions()) {
      expect(def.labelKey).toBeTruthy();
      expect(def.iconKey).toBeTruthy();
      expect(def.supportedTabs.length).toBeGreaterThan(0);
      expect(def.defaultTab).toBeTruthy();
      expect(def.supportedTabs).toContain(def.defaultTab);
    }
  });
});

describe('registry — top-level definitions', () => {
  it('returns exactly 5 top-level definitions', () => {
    const defs = getTopLevelDefinitions();
    expect(defs).toHaveLength(5);
    for (const def of defs) {
      expect(isTopLevel(def.entryType)).toBe(true);
    }
  });
});

describe('registry — category grouping', () => {
  const expected: Record<LibraryCategory, LibraryEntryType[]> = {
    product: [
      'product',
      'offer',
      'reference_group',
      'reference_selection_rule',
      'dimension_schema',
      'market_source_mapping',
      'reference_asset',
    ],
    person: ['person', 'reference_asset'],
    template: ['dimension_template'],
    prompt_policy: ['prompt_policy'],
    datasource: ['data_source', 'manual_fact_set', 'truth_resolution_policy'],
  };

  it.each(LIBRARY_CATEGORIES as unknown as LibraryCategory[])(
    'getDefinitionsForCategory(%s) includes the root type first',
    (cat) => {
      const defs = getDefinitionsForCategory(cat);
      expect(defs.length).toBeGreaterThan(0);
      expect(isTopLevel(defs[0].entryType)).toBe(true);
    }
  );

  it.each(Object.entries(expected))(
    'category %s contains expected types',
    (cat, expectedTypes) => {
      const defs = getDefinitionsForCategory(cat as LibraryCategory);
      const types = defs.map((d) => d.entryType);
      expect(types).toHaveLength(expectedTypes.length);
      for (const et of expectedTypes) {
        expect(types).toContain(et);
      }
    }
  );
});

describe('registry — tab support', () => {
  it('all types support overview tab', () => {
    for (const def of getAllDefinitions()) {
      expect(def.supportedTabs).toContain('overview');
    }
  });

  it('all types support draft tab', () => {
    for (const def of getAllDefinitions()) {
      expect(def.supportedTabs).toContain('draft');
    }
  });

  it('all types support versions tab', () => {
    for (const def of getAllDefinitions()) {
      expect(def.supportedTabs).toContain('versions');
    }
  });

  it('all types support events tab', () => {
    for (const def of getAllDefinitions()) {
      expect(def.supportedTabs).toContain('events');
    }
  });

  it('dimension_template and dimension_schema support schema tab', () => {
    expect(
      getEntryTypeDefinition('dimension_template').supportedTabs
    ).toContain('schema');
    expect(getEntryTypeDefinition('dimension_schema').supportedTabs).toContain(
      'schema'
    );
  });

  it('data_source exposes manual fact import through the facts tab', () => {
    expect(getEntryTypeDefinition('data_source').supportedTabs).toContain(
      'facts'
    );
  });

  it('manual_fact_set supports facts tab', () => {
    expect(getEntryTypeDefinition('manual_fact_set').supportedTabs).toContain(
      'facts'
    );
  });

  it('truth_resolution_policy supports resolution tab', () => {
    expect(
      getEntryTypeDefinition('truth_resolution_policy').supportedTabs
    ).toContain('resolution');
  });

  it('data_source supports settings tab', () => {
    expect(getEntryTypeDefinition('data_source').supportedTabs).toContain(
      'settings'
    );
  });

  it('product and person support bindings and references', () => {
    for (const et of ['product', 'person'] as LibraryEntryType[]) {
      const def = getEntryTypeDefinition(et);
      expect(def.supportedTabs).toContain('bindings');
      expect(def.supportedTabs).toContain('references');
    }
  });
});
