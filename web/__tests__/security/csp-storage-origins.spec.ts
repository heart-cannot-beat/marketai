import { generateCSP } from '../../middleware';

jest.mock('@/lib/domain-client/storage-origin-parser', () => ({
  parseStorageOrigins: jest.fn().mockReturnValue(['https://s3.example.com']),
}));

describe('CSP with storage origins', () => {
  it('includes storage origins in connect-src', () => {
    const csp = generateCSP('test-nonce');
    expect(csp).toContain("connect-src 'self' https://s3.example.com");
  });

  it('never includes DOMAIN_API_BASE_URL in connect-src', () => {
    const original = process.env.DOMAIN_API_BASE_URL;
    process.env.DOMAIN_API_BASE_URL = 'http://localhost:8000';
    try {
      const csp = generateCSP('test-nonce');
      expect(csp).not.toContain('localhost:8000');
    } finally {
      process.env.DOMAIN_API_BASE_URL = original;
    }
  });
});

describe('CSP fails closed on a misconfigured storage origins env var', () => {
  it('omits storage origins from connect-src rather than throwing', () => {
    jest.resetModules();
    jest.doMock('@/lib/domain-client/storage-origin-parser', () => ({
      parseStorageOrigins: jest.fn().mockImplementation(() => {
        throw new Error('Wildcard origins are not allowed');
      }),
    }));

    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const { generateCSP: generateCSPWithBadEnv } = require('../../middleware');
    const consoleError = jest
      .spyOn(console, 'error')
      .mockImplementation(() => {});

    expect(() => generateCSPWithBadEnv('test-nonce')).not.toThrow();
    const csp = generateCSPWithBadEnv('test-nonce');
    expect(csp).toContain("connect-src 'self'");
    expect(csp).not.toContain('s3.example.com');

    consoleError.mockRestore();
    jest.dontMock('@/lib/domain-client/storage-origin-parser');
    jest.resetModules();
  });
});
