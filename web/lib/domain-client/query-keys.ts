import type { DomainOperationMap } from './generated/operation-map';

type OperationId = keyof DomainOperationMap;

type BaseParams = Record<string, string | number | boolean | undefined>;

/**
 * Immutable, structurally stable SWR cache key for a domain read.
 *
 * Shape: `['domain', workspaceSlug, operationId, serializedParams]`.
 * The fourth element is a JSON-stringified snapshot of the params object
 * with `undefined` values stripped and keys sorted — two param objects
 * that are structurally equal always produce the same string, regardless
 * of key insertion order or `undefined` noise.
 */
export type DomainQueryKey = readonly ['domain', string, OperationId, string];

function stableSerialize(params: BaseParams | undefined): string {
  if (!params) return '{}';
  const sorted: Record<string, unknown> = {};
  for (const key of Object.keys(params).sort()) {
    if (params[key] !== undefined) {
      sorted[key] = params[key];
    }
  }
  return JSON.stringify(sorted);
}

export function domainQueryKey<K extends OperationId>(
  workspaceSlug: string,
  operationId: K,
  params?: BaseParams
): DomainQueryKey {
  return [
    'domain',
    workspaceSlug,
    operationId,
    stableSerialize(params),
  ] as const;
}

export type PaginatedOperationId =
  | 'list_experiments'
  | 'list_library_entries'
  | 'list_library_entry_versions'
  | 'list_library_entry_events'
  | 'list_library_version_bindings'
  | 'list_library_internal_entries'
  | 'list_generation_drafts'
  | 'list_generation_runs'
  | 'list_generation_assets'
  | 'list_scale_enrollments'
  | 'list_scale_advice_threads'
  | 'list_scale_targets';

export function paginatedQueryKey(
  workspaceSlug: string,
  operationId: PaginatedOperationId,
  baseParams?: BaseParams
): DomainQueryKey {
  return domainQueryKey(workspaceSlug, operationId, baseParams);
}

export function matchesOperation(
  key: DomainQueryKey,
  operationId: OperationId
): boolean {
  return key[2] === operationId;
}

export function matchesWorkspace(
  key: DomainQueryKey,
  workspaceSlug: string
): boolean {
  return key[1] === workspaceSlug;
}

export function workspacePrefix(
  workspaceSlug: string
): readonly ['domain', string] {
  return ['domain', workspaceSlug] as const;
}
