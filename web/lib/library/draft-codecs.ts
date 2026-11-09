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
 * `dimension_template`, `data_source`, mounted by `components/library/
 * entry-types/draft-editors.ts`); every other key of their drafts rides along
 * untouched in `_unknown` and is edited through the raw JSON editor.
 *
 * Every other entry type is edited as JSON only, and its codec passes the
 * content straight through: no known keys and no defaults, so the JSON
 * editor shows the draft exactly as the server stores it and Save writes back
 * exactly what was typed (#21). The server's validator stays the one list of
 * their fields; each of its findings names the `path` that is missing or
 * wrong.
 */
export interface ProductFields {
  /** `_validate_product`: `name`, not `display_name` -- the display name is
   * a property of the entry, not of its versioned content. */
  name: string;
  description: string;
  audience: string;
  selling_points: string[];
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

/** `_validate_data_source`. The display name is a property of the entry; the
 * execution mode is written with the source type it pairs with
 * (`ADAPTER_EXECUTION_MODES`, #396). */
export interface DataSourceFields {
  adapter_kind: string;
  execution_mode: string;
  config: Record<string, unknown>;
}

/** An entry type edited as JSON only has no fields of its own: its whole
 * content is `_unknown`. */
export type JsonOnlyFields = Record<string, never>;

interface FormFieldMap {
  product: ProductFields;
  dimension_template: DimensionTemplateFields;
  data_source: DataSourceFields;
}

/** The entry types whose codec knows fields, one per mounted form editor. */
export type FormEntryType = keyof FormFieldMap;

export type EntryFieldMap = {
  [K in LibraryEntryType]: K extends FormEntryType
    ? FormFieldMap[K]
    : JsonOnlyFields;
};

export type FieldShape = FormFieldMap[FormEntryType] | JsonOnlyFields;

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

/** The codec of an entry type edited as JSON only: `parse` keeps the content
 * as it is, `serialize` gives it back unchanged. */
function jsonOnlyCodec(
  entryType: LibraryEntryType
): DraftCodec<JsonOnlyFields> {
  return makeCodec<JsonOnlyFields>(entryType, [], {});
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

const DATA_SOURCE_KEYS = ['adapter_kind', 'execution_mode', 'config'] as const;
const DATA_SOURCE_DEFAULTS: DataSourceFields = {
  adapter_kind: '',
  execution_mode: '',
  config: {},
};

const CODEC_MAP: { [K in LibraryEntryType]: DraftCodec<EntryFieldMap[K]> } = {
  product: makeCodec('product', PRODUCT_KEYS, PRODUCT_DEFAULTS),
  offer: jsonOnlyCodec('offer'),
  person: jsonOnlyCodec('person'),
  reference_asset: jsonOnlyCodec('reference_asset'),
  reference_group: jsonOnlyCodec('reference_group'),
  reference_selection_rule: jsonOnlyCodec('reference_selection_rule'),
  market_source_mapping: jsonOnlyCodec('market_source_mapping'),
  dimension_template: makeCodec(
    'dimension_template',
    DIM_TEMPLATE_KEYS,
    DIM_TEMPLATE_DEFAULTS
  ),
  dimension_schema: jsonOnlyCodec('dimension_schema'),
  prompt_policy: jsonOnlyCodec('prompt_policy'),
  data_source: makeCodec('data_source', DATA_SOURCE_KEYS, DATA_SOURCE_DEFAULTS),
  manual_fact_set: jsonOnlyCodec('manual_fact_set'),
  truth_resolution_policy: jsonOnlyCodec('truth_resolution_policy'),
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
