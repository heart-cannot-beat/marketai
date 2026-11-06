import { NextRequest } from 'next/server';

/**
 * Pages are served in the signed-in user's saved language, else the
 * NEXT_LOCALE cookie, else the browser's (#252). Requests are built with the
 * same i18n config Next.js hands the middleware, so `nextUrl.locale` and the
 * prefix-free `nextUrl.pathname` are what production sees.
 */
const i18n = { locales: ['en', 'zh-CN'], defaultLocale: 'en' };
const appUrl = 'https://marketai.example';

const request = (
  path: string,
  {
    headers = {},
    method = 'GET',
  }: { headers?: Record<string, string>; method?: string } = {}
) =>
  new NextRequest(`${appUrl}${path}`, {
    method,
    headers,
    nextConfig: { i18n },
  });

describe('middleware locale selection', () => {
  const originalAppUrl = process.env.APP_URL;

  const loadMiddleware = async () => {
    let middleware: (typeof import('../../middleware'))['default'] | undefined;
    await jest.isolateModulesAsync(async () => {
      middleware = (await import('../../middleware')).default;
    });
    return middleware!;
  };

  const sessionReturns = (session: unknown) =>
    jest.spyOn(global, 'fetch').mockImplementation(
      async () =>
        new Response(JSON.stringify(session), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
    );

  beforeEach(() => {
    process.env.APP_URL = appUrl;
  });

  afterEach(() => {
    jest.restoreAllMocks();
    if (originalAppUrl === undefined) {
      delete process.env.APP_URL;
    } else {
      process.env.APP_URL = originalAppUrl;
    }
  });

  it('serves the sign-in page in the browser language when nothing is saved', async () => {
    const middleware = await loadMiddleware();
    const response = await middleware(
      request('/auth/login?callbackUrl=%2Fw%2Facme', {
        headers: { 'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8' },
      })
    );

    expect(response.status).toBe(307);
    expect(response.headers.get('location')).toBe(
      `${appUrl}/zh-CN/auth/login?callbackUrl=%2Fw%2Facme`
    );
    // Following the browser writes nothing: the cookie stays the user's choice.
    expect(response.cookies.get('NEXT_LOCALE')).toBeUndefined();
  });

  it('lets the language cookie win over the browser', async () => {
    const middleware = await loadMiddleware();
    const stay = await middleware(
      request('/auth/login', {
        headers: {
          'accept-language': 'zh-CN',
          cookie: 'NEXT_LOCALE=en',
        },
      })
    );
    expect(stay.headers.get('location')).toBeNull();

    const back = await middleware(
      request('/zh-CN/auth/login', {
        headers: { 'accept-language': 'zh-CN', cookie: 'NEXT_LOCALE=en' },
      })
    );
    expect(back.headers.get('location')).toBe(`${appUrl}/auth/login`);
  });

  it('ignores a cookie naming a language that is not offered', async () => {
    const middleware = await loadMiddleware();
    const response = await middleware(
      request('/zh-CN/invitations/tok_1', {
        headers: { cookie: 'NEXT_LOCALE=fr', 'accept-language': 'en-US' },
      })
    );
    expect(response.headers.get('location')).toBe(
      `${appUrl}/invitations/tok_1`
    );
  });

  it('moves a signed-in page to the saved preference and remembers it in the cookie', async () => {
    sessionReturns({ user: { id: 'user_1', locale: 'zh-CN' } });
    const middleware = await loadMiddleware();
    const response = await middleware(
      request('/w/acme/tests?plan=tp_1', {
        headers: { cookie: 'NEXT_LOCALE=en', 'accept-language': 'en-US' },
      })
    );

    expect(response.headers.get('location')).toBe(
      `${appUrl}/zh-CN/w/acme/tests?plan=tp_1`
    );
    const cookie = response.cookies.get('NEXT_LOCALE');
    expect(cookie?.value).toBe('zh-CN');
    expect(cookie?.path).toBe('/');
    expect(cookie?.secure).toBe(true);
  });

  it('keeps a signed-in page that already matches the preference', async () => {
    sessionReturns({ user: { id: 'user_1', locale: 'zh-CN' } });
    const middleware = await loadMiddleware();
    const response = await middleware(request('/zh-CN/w/acme/products'));
    expect(response.headers.get('location')).toBeNull();
  });

  it('follows the cookie and browser when the saved preference is cleared', async () => {
    sessionReturns({ user: { id: 'user_1', locale: null } });
    const middleware = await loadMiddleware();
    const response = await middleware(
      request('/w/acme/products', {
        headers: { 'accept-language': 'zh-CN,zh;q=0.9' },
      })
    );
    expect(response.headers.get('location')).toBe(
      `${appUrl}/zh-CN/w/acme/products`
    );
    expect(response.cookies.get('NEXT_LOCALE')).toBeUndefined();
  });

  it('sends a signed-out visitor straight to the sign-in page in their language', async () => {
    sessionReturns({});
    const middleware = await loadMiddleware();
    const response = await middleware(
      request('/zh-CN/w/acme/products', {
        headers: { cookie: 'NEXT_LOCALE=zh-CN' },
      })
    );
    const location = new URL(response.headers.get('location')!);
    expect(location.origin).toBe(appUrl);
    expect(location.pathname).toBe('/zh-CN/auth/login');
    expect(location.searchParams.get('callbackUrl')).toBe('/w/acme/products');
  });

  it('never moves API calls, data requests or non-GET requests', async () => {
    sessionReturns({ user: { id: 'user_1', locale: 'zh-CN' } });
    const middleware = await loadMiddleware();

    for (const req of [
      request('/api/users', { method: 'PUT' }),
      request('/api/auth/csrf', { headers: { 'accept-language': 'zh-CN' } }),
      request('/w/acme/tests', { headers: { 'x-nextjs-data': '1' } }),
      request('/w/acme/tests', { method: 'POST' }),
    ]) {
      const response = await middleware(req);
      expect(response.headers.get('location')).toBeNull();
    }
  });
});
