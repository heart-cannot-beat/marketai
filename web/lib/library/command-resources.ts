import type { LibraryCommandKind } from '@/hooks/library/command-definitions';

/**
 * Semantic cache resources that Library commands may invalidate.
 * Each resource maps to one or more SWR cache key patterns via
 * `useLibraryCache`; command definitions name resources here so
 * they never inspect SWR key internals.
 */
export type LibraryResource =
  | 'entry'
  | 'entryList'
  | 'versions'
  | 'events'
  | 'impact'
  | 'bindings'
  | 'entryTypes'
  | 'internalEntries';

export type InvalidationSet = ReadonlySet<LibraryResource>;

const INVALIDATION_MAP: Record<LibraryCommandKind, InvalidationSet> = {
  create_entry: new Set<LibraryResource>(['entryList', 'entryTypes']),

  create_internal_entry: new Set<LibraryResource>([
    'entryList',
    'internalEntries',
    'entryTypes',
  ]),

  save_draft: new Set<LibraryResource>(['entry', 'events']),

  validate_draft: new Set<LibraryResource>(['entry']),

  publish: new Set<LibraryResource>([
    'entry',
    'entryList',
    'versions',
    'events',
    'impact',
    'bindings',
  ]),

  archive: new Set<LibraryResource>(['entry', 'entryList', 'events']),

  bind_reference: new Set<LibraryResource>(['entry', 'bindings', 'events']),

  withdraw: new Set<LibraryResource>([
    'entry',
    'entryList',
    'versions',
    'events',
    'impact',
    'bindings',
  ]),

  fork: new Set<LibraryResource>([
    'entryList',
    'versions',
    'events',
    'entryTypes',
  ]),

  clone: new Set<LibraryResource>(['entryList', 'entryTypes']),

  apply_template: new Set<LibraryResource>([
    'entryList',
    'internalEntries',
    'events',
    'entryTypes',
  ]),

  create_reference_object: new Set<LibraryResource>([
    'entry',
    'bindings',
    'events',
  ]),

  create_reference_download: new Set<LibraryResource>([]),

  promote_reference: new Set<LibraryResource>([
    'entry',
    'entryList',
    'bindings',
    'events',
  ]),
};

export function getInvalidationSet(
  commandKind: LibraryCommandKind
): InvalidationSet {
  return INVALIDATION_MAP[commandKind];
}

export function shouldInvalidate(
  commandKind: LibraryCommandKind,
  resource: LibraryResource
): boolean {
  return INVALIDATION_MAP[commandKind].has(resource);
}
