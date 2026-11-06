import { CsrfTokenManager } from '../../lib/domain-client/csrf';

class MemoryTransport {
  calls = 0;
  token = 'csrf_token_abc';
  shouldFail = false;

  async fetch(): Promise<{
    ok: boolean;
    status: number;
    json: () => Promise<{ token: string }>;
  }> {
    this.calls++;
    if (this.shouldFail) {
      return { ok: false, status: 401, json: async () => ({ token: '' }) };
    }
    return { ok: true, status: 200, json: async () => ({ token: this.token }) };
  }
}

describe('CsrfTokenManager', () => {
  it('caches token until TTL expires', async () => {
    const transport = new MemoryTransport();
    const manager = new CsrfTokenManager({
      ttlMs: 60_000,
      fetchFn: () => transport.fetch(),
    });
    const t1 = await manager.getToken();
    const t2 = await manager.getToken();
    expect(t1).toBe('csrf_token_abc');
    expect(t2).toBe('csrf_token_abc');
    expect(transport.calls).toBe(1);
  });

  it('re-fetches once the TTL has elapsed', async () => {
    const transport = new MemoryTransport();
    const manager = new CsrfTokenManager({
      ttlMs: 10,
      fetchFn: () => transport.fetch(),
    });

    await manager.getToken();
    expect(transport.calls).toBe(1);

    await new Promise((resolve) => setTimeout(resolve, 20));

    await manager.getToken();
    expect(transport.calls).toBe(2);
  });

  it('single-flights concurrent requests', async () => {
    const transport = new MemoryTransport();
    const manager = new CsrfTokenManager({
      ttlMs: 60_000,
      fetchFn: () => transport.fetch(),
    });
    const [t1, t2] = await Promise.all([
      manager.getToken(),
      manager.getToken(),
    ]);
    expect(t1).toBe(t2);
    expect(transport.calls).toBe(1);
  });

  it('clear() forces a re-fetch', async () => {
    const transport = new MemoryTransport();
    const manager = new CsrfTokenManager({
      ttlMs: 60_000,
      fetchFn: () => transport.fetch(),
    });
    await manager.getToken();
    expect(transport.calls).toBe(1);
    manager.clear();
    await manager.getToken();
    expect(transport.calls).toBe(2);
  });

  it('throws when fetch fails', async () => {
    const transport = new MemoryTransport();
    transport.shouldFail = true;
    const manager = new CsrfTokenManager({
      ttlMs: 60_000,
      fetchFn: () => transport.fetch(),
    });
    await expect(manager.getToken()).rejects.toThrow('CSRF token fetch failed');
  });

  it('a failed fetch does not poison the cache for the next call', async () => {
    const transport = new MemoryTransport();
    transport.shouldFail = true;
    const manager = new CsrfTokenManager({
      ttlMs: 60_000,
      fetchFn: () => transport.fetch(),
    });

    await expect(manager.getToken()).rejects.toThrow('CSRF token fetch failed');

    transport.shouldFail = false;
    const token = await manager.getToken();
    expect(token).toBe('csrf_token_abc');
    expect(transport.calls).toBe(2);
  });

  it('defaults to a same-origin fetch of /api/csrf when no fetchFn is given', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ token: 'default_token' }),
    });
    (global as any).fetch = fetchMock;

    const manager = new CsrfTokenManager({ ttlMs: 60_000 });
    const token = await manager.getToken();

    expect(token).toBe('default_token');
    expect(fetchMock).toHaveBeenCalledWith('/api/csrf', {
      method: 'GET',
      credentials: 'same-origin',
    });

    delete (global as any).fetch;
  });
});
