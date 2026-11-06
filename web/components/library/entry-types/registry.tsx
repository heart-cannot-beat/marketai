import type { EntryTypeDefinition } from '@/lib/library/entry-type-definition';
import type {
  LibraryEntryType,
  LibraryCategory,
} from '@/lib/library/navigation';
import {
  LIBRARY_ENTRY_TYPES,
  isTopLevel,
  getPlacement,
  rootTypeForCategory,
} from '@/lib/library/navigation';

const COMMON_TABS = ['overview', 'draft', 'versions', 'events'] as const;

const DEFINITIONS: Record<LibraryEntryType, EntryTypeDefinition> = {
  product: {
    entryType: 'product',
    labelKey: 'library:entry-type.product',
    iconKey: 'product',
    supportedTabs: [...COMMON_TABS, 'impact', 'bindings', 'references'],
    defaultTab: 'overview',
    hasDraft: true,
    hasVersions: true,
  },
  offer: {
    entryType: 'offer',
    labelKey: 'library:entry-type.offer',
    iconKey: 'offer',
    supportedTabs: [...COMMON_TABS, 'impact'],
    defaultTab: 'overview',
    hasDraft: true,
    hasVersions: true,
  },
  person: {
    entryType: 'person',
    labelKey: 'library:entry-type.person',
    iconKey: 'person',
    supportedTabs: [...COMMON_TABS, 'impact', 'bindings', 'references'],
    defaultTab: 'overview',
    hasDraft: true,
    hasVersions: true,
  },
  reference_asset: {
    entryType: 'reference_asset',
    labelKey: 'library:entry-type.reference_asset',
    iconKey: 'reference_asset',
    supportedTabs: [...COMMON_TABS, 'bindings'],
    defaultTab: 'overview',
    hasDraft: true,
    hasVersions: true,
  },
  reference_group: {
    entryType: 'reference_group',
    labelKey: 'library:entry-type.reference_group',
    iconKey: 'reference_group',
    supportedTabs: [...COMMON_TABS, 'references'],
    defaultTab: 'overview',
    hasDraft: true,
    hasVersions: true,
  },
  reference_selection_rule: {
    entryType: 'reference_selection_rule',
    labelKey: 'library:entry-type.reference_selection_rule',
    iconKey: 'selection_rule',
    supportedTabs: [...COMMON_TABS],
    defaultTab: 'overview',
    hasDraft: true,
    hasVersions: true,
  },
  market_source_mapping: {
    entryType: 'market_source_mapping',
    labelKey: 'library:entry-type.market_source_mapping',
    iconKey: 'market_mapping',
    supportedTabs: [...COMMON_TABS],
    defaultTab: 'overview',
    hasDraft: true,
    hasVersions: true,
  },
  dimension_template: {
    entryType: 'dimension_template',
    labelKey: 'library:entry-type.dimension_template',
    iconKey: 'dimension_template',
    supportedTabs: [...COMMON_TABS, 'impact', 'schema'],
    defaultTab: 'overview',
    hasDraft: true,
    hasVersions: true,
  },
  dimension_schema: {
    entryType: 'dimension_schema',
    labelKey: 'library:entry-type.dimension_schema',
    iconKey: 'dimension_schema',
    supportedTabs: [...COMMON_TABS, 'schema'],
    defaultTab: 'overview',
    hasDraft: true,
    hasVersions: true,
  },
  prompt_policy: {
    entryType: 'prompt_policy',
    labelKey: 'library:entry-type.prompt_policy',
    iconKey: 'prompt_policy',
    supportedTabs: [...COMMON_TABS, 'impact'],
    defaultTab: 'overview',
    hasDraft: true,
    hasVersions: true,
  },
  data_source: {
    entryType: 'data_source',
    labelKey: 'library:entry-type.data_source',
    iconKey: 'data_source',
    supportedTabs: [...COMMON_TABS, 'impact', 'facts', 'settings'],
    defaultTab: 'overview',
    hasDraft: true,
    hasVersions: true,
  },
  manual_fact_set: {
    entryType: 'manual_fact_set',
    labelKey: 'library:entry-type.manual_fact_set',
    iconKey: 'fact_set',
    supportedTabs: [...COMMON_TABS, 'facts'],
    defaultTab: 'overview',
    hasDraft: true,
    hasVersions: true,
  },
  truth_resolution_policy: {
    entryType: 'truth_resolution_policy',
    labelKey: 'library:entry-type.truth_resolution_policy',
    iconKey: 'resolution_policy',
    supportedTabs: [...COMMON_TABS, 'resolution'],
    defaultTab: 'overview',
    hasDraft: true,
    hasVersions: true,
  },
};

export function getEntryTypeDefinition(
  entryType: LibraryEntryType
): EntryTypeDefinition {
  return DEFINITIONS[entryType];
}

export function getDefinitionsForCategory(
  category: LibraryCategory
): EntryTypeDefinition[] {
  const rootType = rootTypeForCategory(category);
  const result: EntryTypeDefinition[] = [DEFINITIONS[rootType]];

  for (const et of LIBRARY_ENTRY_TYPES) {
    if (et === rootType) continue;
    const placement = getPlacement(et);
    if (
      placement.kind === 'internal' &&
      placement.allowedContextCategories.includes(category)
    ) {
      result.push(DEFINITIONS[et]);
    }
  }

  return result;
}

export function getTopLevelDefinitions(): EntryTypeDefinition[] {
  return LIBRARY_ENTRY_TYPES.filter(isTopLevel).map((et) => DEFINITIONS[et]);
}

export function getAllDefinitions(): EntryTypeDefinition[] {
  return LIBRARY_ENTRY_TYPES.map((et) => DEFINITIONS[et]);
}

const _exhaustiveCheck: Record<LibraryEntryType, EntryTypeDefinition> =
  DEFINITIONS;
void _exhaustiveCheck;
