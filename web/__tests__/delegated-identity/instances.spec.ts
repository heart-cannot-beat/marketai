jest.mock('../../lib/prisma', () => ({ prisma: { marker: 'shared-prisma' } }));

const publisherCtor = jest.fn();
jest.mock('../../lib/delegated-identity/jwks-publisher', () => ({
  PrismaJwksPublisher: publisherCtor,
}));

describe('getJwksPublisher', () => {
  const originalMaxAge = process.env.SIGNER_CACHE_MAX_AGE;

  beforeEach(() => {
    jest.clearAllMocks();
    jest.resetModules();
  });

  afterEach(() => {
    if (originalMaxAge === undefined) {
      delete process.env.SIGNER_CACHE_MAX_AGE;
    } else {
      process.env.SIGNER_CACHE_MAX_AGE = originalMaxAge;
    }
  });

  it('builds the publisher once and reuses it across calls', async () => {
    const { getJwksPublisher } =
      await import('../../lib/delegated-identity/instances');

    const first = getJwksPublisher();
    const second = getJwksPublisher();

    expect(first).toBe(second);
    expect(publisherCtor).toHaveBeenCalledTimes(1);
  });

  it('reuses the shared prisma client from @/lib/prisma, not a new one', async () => {
    const { getJwksPublisher } =
      await import('../../lib/delegated-identity/instances');

    getJwksPublisher();

    expect(publisherCtor).toHaveBeenCalledWith(
      { marker: 'shared-prisma' },
      600
    );
  });

  it('reads SIGNER_CACHE_MAX_AGE from the environment', async () => {
    process.env.SIGNER_CACHE_MAX_AGE = '900';

    const { getJwksPublisher } =
      await import('../../lib/delegated-identity/instances');

    getJwksPublisher();

    expect(publisherCtor).toHaveBeenCalledWith(
      { marker: 'shared-prisma' },
      900
    );
  });

  it('defaults max age to 600 when unset', async () => {
    delete process.env.SIGNER_CACHE_MAX_AGE;

    const { getJwksPublisher } =
      await import('../../lib/delegated-identity/instances');

    getJwksPublisher();

    expect(publisherCtor).toHaveBeenCalledWith(
      { marker: 'shared-prisma' },
      600
    );
  });
});
