import type { CommandOperationId } from '@/hooks/domain/useDomainCommand';

export type LibraryCommandKind =
  | 'create_entry'
  | 'create_internal_entry'
  | 'save_draft'
  | 'validate_draft'
  | 'publish'
  | 'archive'
  | 'bind_reference'
  | 'withdraw'
  | 'fork'
  | 'clone'
  | 'apply_template'
  | 'create_reference_object'
  | 'create_reference_download'
  | 'promote_reference';

export type LibraryCommandDefinition = {
  operationId: CommandOperationId;
  targetField: string | null;
};

const DEFINITIONS: Record<LibraryCommandKind, LibraryCommandDefinition> = {
  create_entry: {
    operationId: 'create_library_entry',
    targetField: null,
  },
  create_internal_entry: {
    operationId: 'create_library_internal_entry',
    targetField: 'root_entry_id',
  },
  save_draft: {
    operationId: 'save_library_draft',
    targetField: 'entry_id',
  },
  validate_draft: {
    operationId: 'validate_library_draft',
    targetField: 'entry_id',
  },
  publish: {
    operationId: 'publish_library_entry',
    targetField: 'entry_id',
  },
  archive: {
    operationId: 'archive_library_entry',
    targetField: 'entry_id',
  },
  bind_reference: {
    operationId: 'bind_library_reference',
    targetField: 'entry_id',
  },
  withdraw: {
    operationId: 'withdraw_library_version',
    targetField: 'version_id',
  },
  fork: {
    operationId: 'fork_library_version',
    targetField: 'version_id',
  },
  clone: {
    operationId: 'clone_library_product',
    targetField: 'version_id',
  },
  apply_template: {
    operationId: 'apply_library_template',
    targetField: 'template_version_id',
  },
  create_reference_object: {
    operationId: 'create_library_reference_object',
    targetField: null,
  },
  create_reference_download: {
    operationId: 'create_library_reference_download_intent',
    targetField: 'version_id',
  },
  // Server-side publication of a released reference object: the version
  // content is gate evidence the server already holds (see
  // service.promote_reference_object).
  promote_reference: {
    operationId: 'promote_library_reference_object',
    targetField: 'object_id',
  },
};

export function getDefinition(
  kind: LibraryCommandKind
): LibraryCommandDefinition {
  return DEFINITIONS[kind];
}

export function stableParamHash(params: Record<string, unknown>): string {
  const sorted = Object.keys(params)
    .filter((k) => params[k] !== undefined)
    .sort();
  return sorted.map((k) => `${k}=${String(params[k])}`).join('&');
}
