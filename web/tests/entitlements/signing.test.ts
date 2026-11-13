import { signEnvelope, verifyEnvelope } from '@/lib/entitlements/signing';
import type { SnapshotPayload } from '@/lib/entitlements/types';

const TEST_KEY_ID = 'esk_test_1';
const TEST_KEY = Buffer.from('a]'.repeat(16), 'utf-8').subarray(0, 32);

function makePayload(overrides?: Partial<SnapshotPayload>): SnapshotPayload {
  return {
    snapshot_id: 'es_test_1',
    workspace_id: 'ws_1',
    workspace_name: 'Test',
    authorization_epoch: 1,
    entitlement_version: 1,
    lease_sequence: 1,
    projection_version: 1,
    suspended: false,
    lifecycle: 'active',
    entitlement_snapshot: {
      plan_version_id: 'pv_1',
      source_refs: [],
      period_start: '2026-08-01T00:00:00Z',
      period_end: '2026-09-01T00:00:00Z',
      period_anchor: '2026-08-01T00:00:00Z',
      resolved_entitlements: { 'module.generate.enabled': true },
      restriction_summary: [],
      semantic_hash: 'abc123',
      entitlement_registry_version: 1,
      resolution_policy_version: 1,
      platform_limits_version: 1,
      issued_at: '2026-08-31T10:00:00Z',
      valid_until: '2026-09-01T00:00:00Z',
      audit_event_id: 'evt_1',
    },
    ...overrides,
  };
}

describe('signEnvelope', () => {
  it('produces a valid signed envelope with nested protected_header', () => {
    const payload = makePayload();
    const envelope = signEnvelope(payload, TEST_KEY_ID, TEST_KEY);
    expect(envelope.protected_header.schema_version).toBe(2);
    expect(envelope.protected_header.signature_key_id).toBe(TEST_KEY_ID);
    expect(envelope.protected_header.signature_algorithm).toBe('hmac-sha256');
    expect(typeof envelope.signature).toBe('string');
    expect(envelope.signature.length).toBeGreaterThan(0);
  });

  it('round-trips through verify', () => {
    const payload = makePayload();
    const envelope = signEnvelope(payload, TEST_KEY_ID, TEST_KEY);
    const keyRing = new Map([[TEST_KEY_ID, TEST_KEY]]);
    const verified = verifyEnvelope(envelope, keyRing);
    expect(verified.workspace_id).toBe('ws_1');
  });

  it('refuses a key that is not bytes instead of signing with its text', () => {
    // An encoded key passed through untyped input (#126) would otherwise
    // be used as the HMAC key's own text, and every verifier rejects the
    // envelope.
    const encoded = TEST_KEY.toString('base64') as unknown as Buffer;
    expect(() => signEnvelope(makePayload(), TEST_KEY_ID, encoded)).toThrow(
      'signing key must be bytes'
    );
  });
});

describe('verifyEnvelope', () => {
  it('rejects tampered payload', () => {
    const payload = makePayload();
    const envelope = signEnvelope(payload, TEST_KEY_ID, TEST_KEY);
    envelope.payload.workspace_id = 'ws_evil';
    const keyRing = new Map([[TEST_KEY_ID, TEST_KEY]]);
    expect(() => verifyEnvelope(envelope, keyRing)).toThrow();
  });

  it('rejects unknown key ID', () => {
    const payload = makePayload();
    const envelope = signEnvelope(payload, TEST_KEY_ID, TEST_KEY);
    const emptyRing = new Map<string, Buffer>();
    expect(() => verifyEnvelope(envelope, emptyRing)).toThrow();
  });

  it('rejects tampered protected header fields', () => {
    const payload = makePayload();
    const envelope = signEnvelope(payload, TEST_KEY_ID, TEST_KEY);
    (envelope.protected_header as any).schema_version = 99;
    const keyRing = new Map([[TEST_KEY_ID, TEST_KEY]]);
    expect(() => verifyEnvelope(envelope, keyRing)).toThrow();
  });
});
