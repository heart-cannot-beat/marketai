export const LIBRARY_CATEGORIES = [
  'product',
  'person',
  'template',
  'prompt_policy',
  'datasource',
] as const;

export type LibraryCategory = (typeof LIBRARY_CATEGORIES)[number];

export const LIBRARY_ENTRY_TYPES = [
  'product',
  'offer',
  'person',
  'reference_asset',
  'reference_group',
  'reference_selection_rule',
  'market_source_mapping',
  'dimension_template',
  'dimension_schema',
  'prompt_policy',
  'data_source',
  'manual_fact_set',
  'truth_resolution_policy',
] as const;

export type LibraryEntryType = (typeof LIBRARY_ENTRY_TYPES)[number];

export const TOP_LEVEL_ENTRY_TYPES = [
  'product',
  'person',
  'dimension_template',
  'prompt_policy',
  'data_source',
] as const;

export type TopLevelEntryType = (typeof TOP_LEVEL_ENTRY_TYPES)[number];

export type InternalEntryType = Exclude<LibraryEntryType, TopLevelEntryType>;

export type EntryNavigationPlacement =
  | {
      kind: 'top_level';
      category: LibraryCategory;
    }
  | {
      kind: 'internal';
      canonicalHostCategory: LibraryCategory;
      allowedContextCategories: readonly LibraryCategory[];
      parentKinds: readonly LibraryEntryType[];
    };

const PLACEMENT_MAP: Record<LibraryEntryType, EntryNavigationPlacement> = {
  product: { kind: 'top_level', category: 'product' },
  person: { kind: 'top_level', category: 'person' },
  dimension_template: { kind: 'top_level', category: 'template' },
  prompt_policy: { kind: 'top_level', category: 'prompt_policy' },
  data_source: { kind: 'top_level', category: 'datasource' },

  offer: {
    kind: 'internal',
    canonicalHostCategory: 'product',
    allowedContextCategories: ['product'],
    parentKinds: ['product'],
  },
  reference_group: {
    kind: 'internal',
    canonicalHostCategory: 'product',
    allowedContextCategories: ['product'],
    parentKinds: ['product'],
  },
  reference_selection_rule: {
    kind: 'internal',
    canonicalHostCategory: 'product',
    allowedContextCategories: ['product'],
    parentKinds: ['reference_group'],
  },
  dimension_schema: {
    kind: 'internal',
    canonicalHostCategory: 'product',
    allowedContextCategories: ['product'],
    parentKinds: ['product'],
  },
  market_source_mapping: {
    kind: 'internal',
    canonicalHostCategory: 'product',
    allowedContextCategories: ['product'],
    parentKinds: ['offer'],
  },
  reference_asset: {
    kind: 'internal',
    canonicalHostCategory: 'product',
    allowedContextCategories: ['product', 'person'],
    parentKinds: ['product', 'person', 'reference_group'],
  },
  manual_fact_set: {
    kind: 'internal',
    canonicalHostCategory: 'datasource',
    allowedContextCategories: ['datasource'],
    parentKinds: ['data_source'],
  },
  truth_resolution_policy: {
    kind: 'internal',
    canonicalHostCategory: 'datasource',
    allowedContextCategories: ['datasource'],
    parentKinds: ['data_source'],
  },
};

export function getPlacement(
  entryType: LibraryEntryType
): EntryNavigationPlacement {
  return PLACEMENT_MAP[entryType];
}

export function isTopLevel(
  entryType: LibraryEntryType
): entryType is TopLevelEntryType {
  return PLACEMENT_MAP[entryType].kind === 'top_level';
}

export function isInternal(
  entryType: LibraryEntryType
): entryType is InternalEntryType {
  return PLACEMENT_MAP[entryType].kind === 'internal';
}

const CATEGORY_TO_ROOT_TYPE: Record<LibraryCategory, TopLevelEntryType> = {
  product: 'product',
  person: 'person',
  template: 'dimension_template',
  prompt_policy: 'prompt_policy',
  datasource: 'data_source',
};

const ROOT_TYPE_TO_CATEGORY: Record<TopLevelEntryType, LibraryCategory> = {
  product: 'product',
  person: 'person',
  dimension_template: 'template',
  prompt_policy: 'prompt_policy',
  data_source: 'datasource',
};

export function rootTypeForCategory(
  category: LibraryCategory
): TopLevelEntryType {
  return CATEGORY_TO_ROOT_TYPE[category];
}

export function categoryForTopLevelEntryType(
  entryType: TopLevelEntryType
): LibraryCategory {
  return ROOT_TYPE_TO_CATEGORY[entryType];
}
