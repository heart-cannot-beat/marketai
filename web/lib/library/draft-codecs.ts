import type { LibraryEntryType } from './navigation';

export interface ParsedDraft<T = Record<string, unknown>> {
  fields: T & Record<string, unknown>;
  _unknown: Record<string, unknown>;
}

export interface DraftCodec<T = FieldShape> {
  entryType: LibraryEntryType;
  supportedSchemaVersion: number;
  isSupported(version: number): boolean;
  parse(raw: Record<string, unknown>): ParsedDraft<T>;
  serialize<U>(parsed: ParsedDraft<U>): Record<string, unknown>;
}

/**
 * Field shapes mirror `app/library/contracts.py`'s content validators for
 * the entry types the in-app editor renders a form for (`product`,
 * `dimension_template`, `data_source`); every other key of the draft rides
 * along untouched in `_unknown` and is edited through the raw JSON editor.
 * The remaining shapes below are sketches that have not been aligned with
 * their server validator yet (`_validate_person`, `_validate_offer`, …),
 * which is why their editors are not mounted (`components/library/
 * entry-types/draft-editors.ts`).
 */
export interface ProductFields {
  /** `_validate_product`: `name`, not `display_name` -- the display name is
   * a property of the entry, not of its versioned content. */
  name: string;
  description: string;
  audience: string;
  selling_points: string[];
}

export interface OfferFields {
  display_name: string;
  item_semantics: string;
  market_semantics: string;
  claim_semantics: string;
  date_semantics: string;
}

export interface PersonFields {
  display_name: string;
  person_type: string;
  age_expression: string;
  prompt_fragments: string[];
  rights_regions: string[];
}

export interface ReferenceAssetFields {
  display_name: string;
  asset_purpose: string;
}

export interface ReferenceGroupFields {
  display_name: string;
  group_intent: string;
}

export interface ReferenceSelectionRuleFields {
  display_name: string;
  rule_expression: string;
}

export interface MarketSourceMappingFields {
  display_name: string;
  external_object_id: string;
  market_code: string;
  execution_mode: string;
}

/** One selectable value of a template dimension (`_validate_dimension_template`).
 * `id` is the stable identity *and* the value the Generation preview shows
 * (`preview._resolve_dimensions` projects `value: value_id`). */
export interface DimensionTemplateValue {
  id: string;
  positive_fragments: string[];
  negative_fragments: string[];
}

export interface DimensionTemplateDimension {
  id: string;
  name: string;
  values: DimensionTemplateValue[];
}

export interface DimensionTemplateFields {
  dimensions: DimensionTemplateDimension[];
  /** `requires` / `excludes` / `max_occurrence` rules over dimension ids;
   * preserved verbatim, edited through the raw JSON editor. */
  constraints: unknown[];
  /** The only cardinality the server accepts today (`CARDINALITIES`). */
  combination_cardinality: string;
}

export const DEFAULT_COMBINATION_CARDINALITY = 'one_per_dimension';

export interface DimensionSchemaFields {
  display_name: string;
  dimensions: Array<{
    id: string;
    name: string;
    values: string[];
    constraints: Record<string, unknown>;
  }>;
}

export interface PromptPolicyFields {
  display_name: string;
  registered_tokens: string[];
  locked_phrases: string[];
  brand_tone: string;
}

/** `_validate_data_source`. The display name is a property of the entry; the
 * execution mode is written with the source type it pairs with
 * (`ADAPTER_EXECUTION_MODES`, #396). */
export interface DataSourceFields {
  adapter_kind: string;
  execution_mode: string;
  config: Record<string, unknown>;
}

export interface ManualFactSetFields {
  display_name: string;
  facts: Array<{ id: string; key: string; value: string }>;
}

export interface TruthResolutionPolicyFields {
  display_name: string;
  priority_order: string[];
  conflict_strategy: string;
}

export type FieldShape =
  | ProductFields
  | OfferFields
  | PersonFields
  | ReferenceAssetFields
  | ReferenceGroupFields
  | ReferenceSelectionRuleFields
  | MarketSourceMappingFields
  | DimensionTemplateFields
  | DimensionSchemaFields
  | PromptPolicyFields
  | DataSourceFields
  | ManualFactSetFields
  | TruthResolutionPolicyFields;

export interface EntryFieldMap {
  product: ProductFields;
  offer: OfferFields;
  person: PersonFields;
  reference_asset: ReferenceAssetFields;
  reference_group: ReferenceGroupFields;
  reference_selection_rule: ReferenceSelectionRuleFields;
  market_source_mapping: MarketSourceMappingFields;
  dimension_template: DimensionTemplateFields;
  dimension_schema: DimensionSchemaFields;
  prompt_policy: PromptPolicyFields;
  data_source: DataSourceFields;
  manual_fact_set: ManualFactSetFields;
  truth_resolution_policy: TruthResolutionPolicyFields;
}

function extractKnown<T>(
  raw: Record<string, unknown>,
  knownKeys: readonly string[],
  defaults: T
): ParsedDraft<T> {
  const fields = { ...defaults } as Record<string, unknown>;
  const _unknown: Record<string, unknown> = {};

  for (const [key, value] of Object.entries(raw)) {
    if (knownKeys.includes(key)) {
      fields[key] = value;
    } else {
      _unknown[key] = value;
    }
  }

  return { fields: fields as T & Record<string, unknown>, _unknown };
}

function recombine<T>(parsed: ParsedDraft<T>): Record<string, unknown> {
  return {
    ...parsed._unknown,
    ...(parsed.fields as Record<string, unknown>),
  };
}

function makeCodec<T>(
  entryType: LibraryEntryType,
  knownKeys: readonly string[],
  defaults: T
): DraftCodec<T> {
  return {
    entryType,
    supportedSchemaVersion: 1,
    isSupported(version: number): boolean {
      return version === 1;
    },
    parse(raw: Record<string, unknown>): ParsedDraft<T> {
      return extractKnown(raw, knownKeys, defaults);
    },
    serialize<U>(parsed: ParsedDraft<U>): Record<string, unknown> {
      return recombine(parsed);
    },
  };
}

const PRODUCT_KEYS = [
  'name',
  'description',
  'audience',
  'selling_points',
] as const;
const PRODUCT_DEFAULTS: ProductFields = {
  name: '',
  description: '',
  audience: '',
  selling_points: [],
};

const OFFER_KEYS = [
  'display_name',
  'item_semantics',
  'market_semantics',
  'claim_semantics',
  'date_semantics',
] as const;
const OFFER_DEFAULTS: OfferFields = {
  display_name: '',
  item_semantics: '',
  market_semantics: '',
  claim_semantics: '',
  date_semantics: '',
};

const PERSON_KEYS = [
  'display_name',
  'person_type',
  'age_expression',
  'prompt_fragments',
  'rights_regions',
] as const;
const PERSON_DEFAULTS: PersonFields = {
  display_name: '',
  person_type: 'fictional',
  age_expression: '',
  prompt_fragments: [],
  rights_regions: [],
};

const REF_ASSET_KEYS = ['display_name', 'asset_purpose'] as const;
const REF_ASSET_DEFAULTS: ReferenceAssetFields = {
  display_name: '',
  asset_purpose: '',
};

const REF_GROUP_KEYS = ['display_name', 'group_intent'] as const;
const REF_GROUP_DEFAULTS: ReferenceGroupFields = {
  display_name: '',
  group_intent: '',
};

const REF_RULE_KEYS = ['display_name', 'rule_expression'] as const;
const REF_RULE_DEFAULTS: ReferenceSelectionRuleFields = {
  display_name: '',
  rule_expression: '',
};

const MSM_KEYS = [
  'display_name',
  'external_object_id',
  'market_code',
  'execution_mode',
] as const;
const MSM_DEFAULTS: MarketSourceMappingFields = {
  display_name: '',
  external_object_id: '',
  market_code: '',
  execution_mode: '',
};

const DIM_TEMPLATE_KEYS = [
  'dimensions',
  'constraints',
  'combination_cardinality',
] as const;
const DIM_TEMPLATE_DEFAULTS: DimensionTemplateFields = {
  dimensions: [],
  constraints: [],
  combination_cardinality: DEFAULT_COMBINATION_CARDINALITY,
};

const DIM_SCHEMA_KEYS = ['display_name', 'dimensions'] as const;
const DIM_SCHEMA_DEFAULTS: DimensionSchemaFields = {
  display_name: '',
  dimensions: [],
};

const PROMPT_POLICY_KEYS = [
  'display_name',
  'registered_tokens',
  'locked_phrases',
  'brand_tone',
] as const;
const PROMPT_POLICY_DEFAULTS: PromptPolicyFields = {
  display_name: '',
  registered_tokens: [],
  locked_phrases: [],
  brand_tone: '',
};

const DATA_SOURCE_KEYS = ['adapter_kind', 'execution_mode', 'config'] as const;
const DATA_SOURCE_DEFAULTS: DataSourceFields = {
  adapter_kind: '',
  execution_mode: '',
  config: {},
};

const FACT_SET_KEYS = ['display_name', 'facts'] as const;
const FACT_SET_DEFAULTS: ManualFactSetFields = {
  display_name: '',
  facts: [],
};

const RESOLUTION_KEYS = [
  'display_name',
  'priority_order',
  'conflict_strategy',
] as const;
const RESOLUTION_DEFAULTS: TruthResolutionPolicyFields = {
  display_name: '',
  priority_order: [],
  conflict_strategy: '',
};

const CODEC_MAP: { [K in LibraryEntryType]: DraftCodec<EntryFieldMap[K]> } = {
  product: makeCodec('product', PRODUCT_KEYS, PRODUCT_DEFAULTS),
  offer: makeCodec('offer', OFFER_KEYS, OFFER_DEFAULTS),
  person: makeCodec('person', PERSON_KEYS, PERSON_DEFAULTS),
  reference_asset: makeCodec(
    'reference_asset',
    REF_ASSET_KEYS,
    REF_ASSET_DEFAULTS
  ),
  reference_group: makeCodec(
    'reference_group',
    REF_GROUP_KEYS,
    REF_GROUP_DEFAULTS
  ),
  reference_selection_rule: makeCodec(
    'reference_selection_rule',
    REF_RULE_KEYS,
    REF_RULE_DEFAULTS
  ),
  market_source_mapping: makeCodec(
    'market_source_mapping',
    MSM_KEYS,
    MSM_DEFAULTS
  ),
  dimension_template: makeCodec(
    'dimension_template',
    DIM_TEMPLATE_KEYS,
    DIM_TEMPLATE_DEFAULTS
  ),
  dimension_schema: makeCodec(
    'dimension_schema',
    DIM_SCHEMA_KEYS,
    DIM_SCHEMA_DEFAULTS
  ),
  prompt_policy: makeCodec(
    'prompt_policy',
    PROMPT_POLICY_KEYS,
    PROMPT_POLICY_DEFAULTS
  ),
  data_source: makeCodec('data_source', DATA_SOURCE_KEYS, DATA_SOURCE_DEFAULTS),
  manual_fact_set: makeCodec(
    'manual_fact_set',
    FACT_SET_KEYS,
    FACT_SET_DEFAULTS
  ),
  truth_resolution_policy: makeCodec(
    'truth_resolution_policy',
    RESOLUTION_KEYS,
    RESOLUTION_DEFAULTS
  ),
};

export function getCodec<K extends LibraryEntryType>(
  entryType: K
): DraftCodec<EntryFieldMap[K]> {
  return CODEC_MAP[entryType];
}

export function isSchemaSupported(
  entryType: LibraryEntryType,
  version: number
): boolean {
  return CODEC_MAP[entryType].isSupported(version);
}
