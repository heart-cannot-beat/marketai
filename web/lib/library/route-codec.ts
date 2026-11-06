import type { ParsedUrlQuery } from 'querystring';
import type { LibraryCategory, LibraryEntryType } from './navigation';
import {
  LIBRARY_CATEGORIES,
  LIBRARY_ENTRY_TYPES,
  isInternal,
} from './navigation';

export const LIBRARY_TABS = [
  'overview',
  'draft',
  'versions',
  'events',
  'impact',
  'bindings',
  'references',
  'schema',
  'facts',
  'resolution',
  'settings',
] as const;

export type LibraryTab = (typeof LIBRARY_TABS)[number];

export interface LibraryRouteState {
  cat: LibraryCategory;
  entry: string | null;
  tab: LibraryTab | null;
  version: string | null;
  resource_type: LibraryEntryType | null;
  resource: string | null;
  resource_tab: LibraryTab | null;
  resource_version: string | null;
  parent_type: LibraryEntryType | null;
  parent: string | null;
}

const QUERY_KEYS = [
  'cat',
  'entry',
  'tab',
  'version',
  'resource_type',
  'resource',
  'resource_tab',
  'resource_version',
  'parent_type',
  'parent',
] as const;

function str(v: string | string[] | undefined): string | null {
  if (v === undefined) return null;
  if (Array.isArray(v)) return v[0] || null;
  return v || null;
}

function isCategory(v: string | null): v is LibraryCategory {
  return v !== null && (LIBRARY_CATEGORIES as readonly string[]).includes(v);
}

function isEntryType(v: string | null): v is LibraryEntryType {
  return v !== null && (LIBRARY_ENTRY_TYPES as readonly string[]).includes(v);
}

function isTab(v: string | null): v is LibraryTab {
  return v !== null && (LIBRARY_TABS as readonly string[]).includes(v);
}

export function parseLibraryRoute(query: ParsedUrlQuery): LibraryRouteState {
  const cat = str(query.cat);
  const entry = str(query.entry);
  const tab = str(query.tab);
  const version = str(query.version);
  const resource_type = str(query.resource_type);
  const resource = str(query.resource);
  const resource_tab = str(query.resource_tab);
  const resource_version = str(query.resource_version);
  const parent_type = str(query.parent_type);
  const parent = str(query.parent);

  return {
    cat: isCategory(cat) ? cat : 'product',
    entry: entry,
    tab: isTab(tab) ? tab : null,
    version: version,
    resource_type: isEntryType(resource_type) ? resource_type : null,
    resource: resource,
    resource_tab: isTab(resource_tab) ? resource_tab : null,
    resource_version: resource_version,
    parent_type: isEntryType(parent_type) ? parent_type : null,
    parent: parent,
  };
}

export function serializeLibraryRoute(
  state: LibraryRouteState
): Record<string, string> {
  const q: Record<string, string> = {};
  if (state.cat !== 'product') q.cat = state.cat;
  if (state.entry) q.entry = state.entry;
  if (state.tab) q.tab = state.tab;
  if (state.version) q.version = state.version;
  if (state.resource_type) q.resource_type = state.resource_type;
  if (state.resource) q.resource = state.resource;
  if (state.resource_tab) q.resource_tab = state.resource_tab;
  if (state.resource_version) q.resource_version = state.resource_version;
  if (state.parent_type) q.parent_type = state.parent_type;
  if (state.parent) q.parent = state.parent;
  return q;
}

export function normalizeLibraryRoute(
  state: LibraryRouteState
): LibraryRouteState {
  const s = { ...state };

  // Resource type and resource are paired
  if (s.resource_type && !s.resource) {
    s.resource_type = null;
  }
  if (s.resource && !s.resource_type) {
    s.resource = null;
  }

  // Parent type and parent are paired
  if (s.parent_type && !s.parent) {
    s.parent_type = null;
  }
  if (s.parent && !s.parent_type) {
    s.parent = null;
  }

  // No entry → clear everything downstream
  if (!s.entry) {
    s.tab = null;
    s.version = null;
    s.resource_type = null;
    s.resource = null;
    s.resource_tab = null;
    s.resource_version = null;
    s.parent_type = null;
    s.parent = null;
    return s;
  }

  // No resource → clear resource-level state
  if (!s.resource) {
    s.resource_tab = null;
    s.resource_version = null;
  }

  // Version only makes sense with versions/impact/bindings tabs
  if (
    s.version &&
    s.tab !== 'versions' &&
    s.tab !== 'impact' &&
    s.tab !== 'bindings'
  ) {
    s.version = null;
  }

  // Resource version follows same rule at resource level
  if (
    s.resource_version &&
    s.resource_tab !== 'versions' &&
    s.resource_tab !== 'impact' &&
    s.resource_tab !== 'bindings'
  ) {
    s.resource_version = null;
  }

  // Internal entry type in `entry` position normalizes into resource state
  // after direct lookup — this is handled by the hook, not the pure codec.
  // The codec just ensures structural consistency.

  // Validate resource_type is actually internal
  if (s.resource_type && !isInternal(s.resource_type)) {
    s.resource_type = null;
    s.resource = null;
    s.resource_tab = null;
    s.resource_version = null;
    s.parent_type = null;
    s.parent = null;
  }

  return s;
}

export function routesEqual(
  a: LibraryRouteState,
  b: LibraryRouteState
): boolean {
  for (const key of QUERY_KEYS) {
    if (a[key] !== b[key]) return false;
  }
  return true;
}

export function buildLibraryPath(
  slug: string,
  state: LibraryRouteState
): string {
  const q = serializeLibraryRoute(state);
  const params = new URLSearchParams(q);
  const qs = params.toString();
  return `/w/${slug}/library${qs ? `?${qs}` : ''}`;
}
