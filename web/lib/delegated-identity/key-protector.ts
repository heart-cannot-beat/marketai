import crypto from 'node:crypto';

/**
 * Ciphertext, IV and auth tag produced by {@link AesGcmPrivateKeyProtector.protect}.
 * All three fields are required to decrypt; they are stored together (e.g.
 * base64-encoded) in `DelegatedSigningKey.protectedPrivateKey`.
 */
export type ProtectedKey = {
  readonly ciphertext: Buffer;
  readonly iv: Buffer;
  readonly authTag: Buffer;
};

/**
 * Identifies the key record a `ProtectedKey` belongs to. Serialized as
 * Additional Authenticated Data (AAD) on encrypt/decrypt so ciphertext
 * cannot be copied from one `DelegatedSigningKey` row to another (or
 * decrypted after the row's binding metadata changes) without GCM
 * authentication failing.
 */
export type KeyBinding = {
  readonly kid: string;
  readonly algorithm: string;
  readonly publicJwkThumbprint: string;
};

/**
 * Thrown for any failure during `unprotect`: wrong encryption key, wrong
 * binding (AAD mismatch), or tampered/corrupted ciphertext or auth tag.
 * Node's `crypto` module does not distinguish between these cases at the
 * API level, and the distinction is not safe to expose to callers anyway
 * (it would be an oracle for tampering attempts), so they are collapsed
 * into a single error type.
 */
export class KeyProtectionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'KeyProtectionError';
  }
}

const ALGORITHM = 'aes-256-gcm';
const IV_LENGTH_BYTES = 12;
const KEY_LENGTH_BYTES = 32;

/**
 * Canonicalize a `KeyBinding` into the exact byte sequence used as AAD.
 * Keys are sorted so the AAD is deterministic regardless of the order
 * properties were set on the object literal.
 */
function canonicalJson(binding: KeyBinding): Buffer {
  const sorted = JSON.stringify(binding, Object.keys(binding).sort());
  return Buffer.from(sorted, 'utf-8');
}

/**
 * Encrypts/decrypts ES256 private key material at rest using AES-256-GCM,
 * with a `KeyBinding` (key id, algorithm, public JWK thumbprint) bound in
 * as Additional Authenticated Data. Binding the key id prevents ciphertext
 * for one `DelegatedSigningKey` row from being substituted into another
 * row's `protectedPrivateKey` column — decryption fails closed instead of
 * silently returning the wrong key.
 */
export class AesGcmPrivateKeyProtector {
  private readonly encryptionKey: Buffer;

  constructor(encryptionKey: Buffer) {
    if (encryptionKey.length !== KEY_LENGTH_BYTES) {
      throw new Error(
        `Encryption key must be exactly ${KEY_LENGTH_BYTES} bytes, got ${encryptionKey.length}`
      );
    }
    this.encryptionKey = encryptionKey;
  }

  protect(privateKeyPem: Buffer, binding: KeyBinding): ProtectedKey {
    const iv = crypto.randomBytes(IV_LENGTH_BYTES);
    const aad = canonicalJson(binding);
    const cipher = crypto.createCipheriv(ALGORITHM, this.encryptionKey, iv);
    cipher.setAAD(aad);
    const ciphertext = Buffer.concat([
      cipher.update(privateKeyPem),
      cipher.final(),
    ]);
    const authTag = cipher.getAuthTag();
    return { ciphertext, iv, authTag };
  }

  unprotect(protectedKey: ProtectedKey, binding: KeyBinding): Buffer {
    const aad = canonicalJson(binding);
    try {
      const decipher = crypto.createDecipheriv(
        ALGORITHM,
        this.encryptionKey,
        protectedKey.iv
      );
      decipher.setAAD(aad);
      decipher.setAuthTag(protectedKey.authTag);
      return Buffer.concat([
        decipher.update(protectedKey.ciphertext),
        decipher.final(),
      ]);
    } catch (err) {
      throw new KeyProtectionError(
        `Decryption failed: ${err instanceof Error ? err.message : 'unknown error'}`
      );
    }
  }
}
