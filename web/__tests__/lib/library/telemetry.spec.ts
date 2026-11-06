/**
 * @jest-environment jsdom
 */
import {
  emitLibraryEvent,
  isAllowedOperation,
  isAllowedAction,
  isAllowedOutcome,
  isAllowedReason,
} from '@/lib/library/telemetry';

describe('telemetry bounded labels', () => {
  describe('isAllowedOperation', () => {
    it('accepts known operations', () => {
      expect(isAllowedOperation('list_library_entries')).toBe(true);
      expect(isAllowedOperation('get_library_entry')).toBe(true);
      expect(isAllowedOperation('save_library_draft')).toBe(true);
      expect(isAllowedOperation('publish_library_entry')).toBe(true);
      expect(isAllowedOperation('create_upload_intent')).toBe(true);
    });

    it('rejects unknown operations', () => {
      expect(isAllowedOperation('arbitrary_operation')).toBe(false);
      expect(isAllowedOperation('')).toBe(false);
      expect(isAllowedOperation('user_id:123')).toBe(false);
    });
  });

  describe('isAllowedAction', () => {
    it('accepts known actions', () => {
      expect(isAllowedAction('page_view')).toBe(true);
      expect(isAllowedAction('tab_switch')).toBe(true);
      expect(isAllowedAction('draft_save')).toBe(true);
      expect(isAllowedAction('upload_start')).toBe(true);
      expect(isAllowedAction('template_reapply')).toBe(true);
    });

    it('rejects unknown actions', () => {
      expect(isAllowedAction('fetch_user_profile')).toBe(false);
      expect(isAllowedAction('')).toBe(false);
    });
  });

  describe('isAllowedOutcome', () => {
    it('accepts known outcomes', () => {
      expect(isAllowedOutcome('success')).toBe(true);
      expect(isAllowedOutcome('failure')).toBe(true);
      expect(isAllowedOutcome('denied')).toBe(true);
      expect(isAllowedOutcome('cancelled')).toBe(true);
      expect(isAllowedOutcome('conflict')).toBe(true);
    });

    it('rejects unknown outcomes', () => {
      expect(isAllowedOutcome('timeout')).toBe(false);
      expect(isAllowedOutcome('partial')).toBe(false);
    });
  });

  describe('isAllowedReason', () => {
    it('accepts known reasons', () => {
      expect(isAllowedReason('insufficient_permission')).toBe(true);
      expect(isAllowedReason('conflict_detected')).toBe(true);
      expect(isAllowedReason('gate_rejected')).toBe(true);
    });

    it('rejects unknown reasons', () => {
      expect(isAllowedReason('random_string')).toBe(false);
      expect(isAllowedReason('Error: something happened at line 42')).toBe(
        false
      );
    });
  });
});

describe('emitLibraryEvent', () => {
  let telemetrySpy: jest.Mock;

  beforeEach(() => {
    telemetrySpy = jest.fn();
    (window as any).__LIBRARY_TELEMETRY__ = telemetrySpy;
  });

  afterEach(() => {
    delete (window as any).__LIBRARY_TELEMETRY__;
  });

  it('emits event with valid labels', () => {
    emitLibraryEvent({
      operation: 'save_library_draft',
      action: 'draft_save',
      outcome: 'success',
      durationMs: 150,
    });
    expect(telemetrySpy).toHaveBeenCalledWith({
      operation: 'save_library_draft',
      action: 'draft_save',
      outcome: 'success',
      durationMs: 150,
    });
  });

  it('replaces unknown operation with "unknown"', () => {
    emitLibraryEvent({ operation: 'user_id_leaked_operation' });
    expect(telemetrySpy).toHaveBeenCalledWith({ operation: 'unknown' });
  });

  it('replaces unknown action with "unknown"', () => {
    emitLibraryEvent({ action: 'steal_credentials' });
    expect(telemetrySpy).toHaveBeenCalledWith({ action: 'unknown' });
  });

  it('replaces unknown outcome with "unknown"', () => {
    emitLibraryEvent({ outcome: 'Error: stack trace...' });
    expect(telemetrySpy).toHaveBeenCalledWith({ outcome: 'unknown' });
  });

  it('replaces unknown reason with "unknown"', () => {
    emitLibraryEvent({ reason: 'User email: foo@bar.com leaked' });
    expect(telemetrySpy).toHaveBeenCalledWith({ reason: 'unknown' });
  });

  it('rounds durationMs', () => {
    emitLibraryEvent({ durationMs: 123.456 });
    expect(telemetrySpy).toHaveBeenCalledWith({ durationMs: 123 });
  });

  it('excludes negative durationMs', () => {
    emitLibraryEvent({ durationMs: -5 });
    expect(telemetrySpy).toHaveBeenCalledWith({});
  });

  it('does not throw when telemetry function is not set', () => {
    delete (window as any).__LIBRARY_TELEMETRY__;
    expect(() => emitLibraryEvent({ action: 'page_view' })).not.toThrow();
  });

  it('never includes IDs, URLs, or arbitrary text', () => {
    emitLibraryEvent({
      operation: 'secret_user_id_1234',
      action: 'https://evil.com/exfil',
      outcome: 'Error: connection to postgres://user:pass@host/db failed',
      reason: '/api/v1/entries/abc-123?token=xyz',
    });
    expect(telemetrySpy).toHaveBeenCalledWith({
      operation: 'unknown',
      action: 'unknown',
      outcome: 'unknown',
      reason: 'unknown',
    });
  });
});
