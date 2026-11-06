import { DefaultDomainGateway } from '@/lib/domain-client/gateway';
import { IntentTracker } from '@/lib/domain-client/intent';
import { CsrfTokenManager } from '@/lib/domain-client/csrf';

test('default browser fetch keeps its global receiver when used as a gateway member', async () => {
  const previous = globalThis.fetch;
  const fetchMock = jest.fn(function (this: unknown) {
    if (this !== globalThis) throw new TypeError('Illegal invocation');
    return Promise.resolve(
      new Response(JSON.stringify({ mock_available: true }), { status: 200 })
    );
  });
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  try {
    const gateway = new DefaultDomainGateway({
      csrfTokenManager: new CsrfTokenManager({ ttlMs: 1000 }),
      intentTracker: new IntentTracker({
        storage: {
          length: 0,
          clear: () => {},
          getItem: () => null,
          key: () => null,
          removeItem: () => {},
          setItem: () => {},
        },
      }),
    });
    const result = await gateway.getMarketingCapabilities({
      workspaceSlug: 'alpha',
    });
    expect(result.kind).toBe('ok');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/domain/v1/w/alpha/marketing/capabilities',
      expect.objectContaining({ method: 'GET' })
    );
  } finally {
    globalThis.fetch = previous;
  }
});
