const ALLOWED_OPERATIONS = new Set([
  'list_library_entries',
  'get_library_entry',
  'get_library_draft',
  'save_library_draft',
  'validate_library_draft',
  'publish_library_entry',
  'withdraw_library_version',
  'archive_library_entry',
  'create_entry',
  'fork_library_version',
  'clone_library_product',
  'apply_library_template',
  'bind_library_reference',
  'get_library_version_impact',
  'get_library_template_diff',
  'check_data_source_connection',
  'import_manual_facts',
  'create_upload_intent',
  'finalize_upload',
  'get_operation',
  'create_library_reference_object',
  'create_library_reference_download_intent',
  'list_library_versions',
  'list_library_events',
  'list_library_bindings',
  'list_library_internal_entries',
]);

const ALLOWED_ACTIONS = new Set([
  'page_view',
  'tab_switch',
  'category_switch',
  'entry_select',
  'draft_save',
  'draft_validate',
  'publish',
  'withdraw',
  'archive',
  'fork',
  'clone',
  'apply_template',
  'bind_reference',
  'upload_start',
  'upload_complete',
  'upload_rejected',
  'upload_failed',
  'download',
  'connection_check',
  'import_facts',
  'template_reapply',
  'create_entry',
  'create_internal_entry',
  'conflict_resolve',
  'reconciliation_retry',
]);

type Outcome = 'success' | 'failure' | 'denied' | 'cancelled' | 'conflict';

const ALLOWED_OUTCOMES = new Set<Outcome>([
  'success',
  'failure',
  'denied',
  'cancelled',
  'conflict',
]);

const ALLOWED_REASONS = new Set([
  'insufficient_permission',
  'entry_archived',
  'no_published_version',
  'draft_not_validated',
  'version_withdrawn',
  'version_superseded',
  'validation_errors',
  'network_error',
  'server_error',
  'conflict_detected',
  'gate_rejected',
  'user_cancelled',
]);

export interface LibraryTelemetryEvent {
  operation?: string;
  action?: string;
  outcome?: string;
  reason?: string;
  durationMs?: number;
}

export function emitLibraryEvent(event: LibraryTelemetryEvent): void {
  const sanitized: LibraryTelemetryEvent = {};

  if (event.operation) {
    sanitized.operation = ALLOWED_OPERATIONS.has(event.operation)
      ? event.operation
      : 'unknown';
  }
  if (event.action) {
    sanitized.action = ALLOWED_ACTIONS.has(event.action)
      ? event.action
      : 'unknown';
  }
  if (event.outcome) {
    sanitized.outcome = ALLOWED_OUTCOMES.has(event.outcome as Outcome)
      ? event.outcome
      : 'unknown';
  }
  if (event.reason) {
    sanitized.reason = ALLOWED_REASONS.has(event.reason)
      ? event.reason
      : 'unknown';
  }
  if (typeof event.durationMs === 'number' && event.durationMs >= 0) {
    sanitized.durationMs = Math.round(event.durationMs);
  }

  if (
    typeof window !== 'undefined' &&
    typeof (window as any).__LIBRARY_TELEMETRY__ === 'function'
  ) {
    (window as any).__LIBRARY_TELEMETRY__(sanitized);
  }
}

export function isAllowedOperation(op: string): boolean {
  return ALLOWED_OPERATIONS.has(op);
}

export function isAllowedAction(action: string): boolean {
  return ALLOWED_ACTIONS.has(action);
}

export function isAllowedOutcome(outcome: string): boolean {
  return ALLOWED_OUTCOMES.has(outcome as Outcome);
}

export function isAllowedReason(reason: string): boolean {
  return ALLOWED_REASONS.has(reason);
}
