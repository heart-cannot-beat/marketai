import { NextRequest } from 'next/server';

describe('authentication middleware behind a reverse proxy', () => {
  const originalAppUrl = process.env.APP_URL;

  const loadMiddleware = async () => {
    let middleware: (typeof import('../../middleware'))['default'] | undefined;
    await jest.isolateModulesAsync(async () => {
      middleware = (await import('../../middleware')).default;
    });
    return middleware!;
  };

  afterEach(() => {
    jest.restoreAllMocks();
    if (originalAppUrl === undefined) {
      delete process.env.APP_URL;
    } else {
      process.env.APP_URL = originalAppUrl;
    }
  });

  it('accepts a live session when the reverse proxy reports its internal TLS origin', async () => {
    const appUrl = 'https://marketai.example';
    process.env.APP_URL = appUrl;
    const sessionUrl = `${appUrl}/api/auth/session`;
    const fetchMock = jest
      .spyOn(global, 'fetch')
      .mockImplementation(async (input) => {
        if (String(input) !== sessionUrl) {
          throw new Error(
            'internal proxy origin is not reachable from middleware'
          );
        }

        return new Response(JSON.stringify({ user: { id: 'user-1' } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });

    const middleware = await loadMiddleware();
    const response = await middleware(
      new NextRequest('https://localhost:4002/dashboard', {
        headers: { cookie: 'next-auth.session-token=synthetic-test-cookie' },
      })
    );

    expect(response.headers.get('location')).toBeNull();
    const [requestedUrl, requestInit] = fetchMock.mock.calls[0];
    expect(String(requestedUrl)).toBe(sessionUrl);
    expect((requestInit?.headers as Record<string, string>).cookie).toBe(
      'next-auth.session-token=synthetic-test-cookie'
    );
  });
});
