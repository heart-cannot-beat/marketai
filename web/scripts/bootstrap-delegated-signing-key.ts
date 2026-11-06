/**
 * Operator-only initial delegated-signing-key bootstrap.
 *
 * A fresh control-plane database cannot publish a usable JWKS until it has an
 * ACTIVE key. This command creates that first key once; subsequent runs are
 * no-ops. The private key is generated at runtime and encrypted before it is
 * stored, so no signing material is baked into an image or migration.
 */
import { loadRuntimeEnv, projectRoot } from './loadEnv';

const main = async () => {
  loadRuntimeEnv(projectRoot(__dirname));

  const [{ prisma }, { ensureActiveDelegatedSigningKey }] = await Promise.all([
    import('../lib/prisma'),
    import('../lib/delegated-identity/key-bootstrap'),
  ]);
  const encodedEncryptionKey =
    process.env.DELEGATED_SIGNING_KEY_ENCRYPTION_KEY ?? '';

  try {
    if (!encodedEncryptionKey) {
      throw new Error('DELEGATED_SIGNING_KEY_ENCRYPTION_KEY is not set');
    }

    const result = await ensureActiveDelegatedSigningKey(
      prisma,
      Buffer.from(encodedEncryptionKey, 'base64')
    );
    process.stdout.write(
      result.created
        ? `Created ACTIVE delegated signing key ${result.kid}\n`
        : `ACTIVE delegated signing key ${result.kid} already exists\n`
    );
  } catch (error) {
    process.stderr.write(`${(error as Error).message}\n`);
    await prisma.$disconnect();
    process.exit(1);
  }

  await prisma.$disconnect();
};

void main();
