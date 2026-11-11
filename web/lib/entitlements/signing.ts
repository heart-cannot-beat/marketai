import crypto from 'node:crypto';
import type { ProtectedHeader, SignedEnvelope, SnapshotPayload } from './types';

/**
 * The exact bytes the signature is computed over: the protected header
 * and the complete payload, canonicalized with deterministic key ordering
 * via `JSON.stringify` with `sort_keys` / sorted replacer equivalent.
 *
 * The Python verifier (`projection.py`) uses `json.dumps(sort_keys=True,
 * separators=(",", ":"))`, so we must produce identical bytes.  We use a
 * `replacer` that sorts object keys and `JSON.stringify` with no space
 * after separators to match Python's `separators=(",", ":")`.
 */
function canonicalBytes(
  protectedHeader: ProtectedHeader,
  payload: SnapshotPayload
): Buffer {
  const obj = { protected_header: protectedHeader, payload };
  const canonical = JSON.stringify(obj, (_key, value) => {
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      const sorted: Record<string, unknown> = {};
      for (const k of Object.keys(value).sort()) {
        sorted[k] = value[k];
      }
      return sorted;
    }
    return value;
  });
  return Buffer.from(canonical, 'utf-8');
}

/**
 * Builds and HMAC-SHA256-signs a `WorkspaceProjectionEnvelope` around
 * `payload`. `key` is the raw signing key for `keyId` -- callers resolve
 * which key to use (current `ENTITLEMENT_SNAPSHOT_SIGNING_KEY`) before
 * calling this; signing itself is key-rotation-agnostic.
 *
 * The envelope uses the nested `{protected_header, payload, signature}`
 * format that the domain projection module expects.
 */
export function signEnvelope(
  payload: SnapshotPayload,
  keyId: string,
  key: Buffer
): SignedEnvelope {
  // A string would be accepted by createHmac as the key's own text: an
  // encoded key passed through untyped input would sign with the wrong
  // bytes, and only the receiving verifier would notice.
  if (!Buffer.isBuffer(key)) {
    throw new TypeError('signing key must be bytes');
  }
  if (key.length === 0) {
    throw new Error('signing key must not be empty');
  }
  const protectedHeader: ProtectedHeader = {
    schema_version: 2,
    signature_key_id: keyId,
    signature_algorithm: 'hmac-sha256',
  };
  const mac = crypto
    .createHmac('sha256', key)
    .update(canonicalBytes(protectedHeader, payload))
    .digest('hex');
  return { protected_header: protectedHeader, payload, signature: mac };
}

/**
 * Verifies `envelope`'s signature against the key named by
 * `envelope.protected_header.signature_key_id`, looked up in `keyRing`
 * (supports rotation: multiple still-valid keys can be live at once).
 * Throws on any of: unknown key ID, tampered payload, or tampered
 * protected header -- callers must not treat a caught error as
 * "verified false"; the envelope is unusable and should be rejected
 * outright. Returns the verified payload on success.
 */
export function verifyEnvelope(
  envelope: SignedEnvelope,
  keyRing: Map<string, Buffer>
): SnapshotPayload {
  const keyId = envelope.protected_header.signature_key_id;
  const key = keyRing.get(keyId);
  if (!key) {
    throw new Error(`Unknown signing key: ${keyId}`);
  }
  const expected = crypto
    .createHmac('sha256', key)
    .update(canonicalBytes(envelope.protected_header, envelope.payload))
    .digest('hex');

  const signatureBuf = Buffer.from(envelope.signature, 'hex');
  const expectedBuf = Buffer.from(expected, 'hex');
  if (
    signatureBuf.length !== expectedBuf.length ||
    !crypto.timingSafeEqual(signatureBuf, expectedBuf)
  ) {
    throw new Error('Envelope signature verification failed');
  }
  return envelope.payload;
}
