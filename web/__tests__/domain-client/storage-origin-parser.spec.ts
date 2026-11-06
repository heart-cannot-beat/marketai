import { parseStorageOrigins } from '@/lib/domain-client/storage-origin-parser';

describe('parseStorageOrigins', () => {
  it('parses comma-separated origins', () => {
    const origins = parseStorageOrigins(
      'https://s3.example.com,https://cdn.example.com',
      { production: true }
    );
    expect(origins).toEqual([
      'https://s3.example.com',
      'https://cdn.example.com',
    ]);
  });

  it('trims whitespace', () => {
    const origins = parseStorageOrigins(
      ' https://s3.example.com , https://cdn.example.com ',
      { production: true }
    );
    expect(origins).toEqual([
      'https://s3.example.com',
      'https://cdn.example.com',
    ]);
  });

  it('rejects wildcards in production', () => {
    expect(() => parseStorageOrigins('*', { production: true })).toThrow(
      /wildcard/i
    );
  });

  it('rejects non-HTTPS in production', () => {
    expect(() =>
      parseStorageOrigins('http://store.local:9000', { production: true })
    ).toThrow(/https/i);
  });

  it('allows explicitly configured HTTP loopback for a local acceptance harness', () => {
    expect(
      parseStorageOrigins('http://localhost:9000', {
        production: true,
        allowInsecureLoopback: true,
      })
    ).toEqual(['http://localhost:9000']);
  });

  it('does not let the acceptance override allow a non-loopback HTTP origin', () => {
    expect(() =>
      parseStorageOrigins('http://store.example.com', {
        production: true,
        allowInsecureLoopback: true,
      })
    ).toThrow(/https/i);
  });

  it('allows HTTP loopback in development', () => {
    const origins = parseStorageOrigins('http://localhost:9000', {
      production: false,
    });
    expect(origins).toEqual(['http://localhost:9000']);
  });

  it('rejects non-loopback HTTP in development', () => {
    expect(() =>
      parseStorageOrigins('http://store.example.com', { production: false })
    ).toThrow(/loopback/i);
  });

  it('rejects origins with paths', () => {
    expect(() =>
      parseStorageOrigins('https://s3.example.com/bucket', { production: true })
    ).toThrow(/path/i);
  });

  it('rejects origins with credentials', () => {
    expect(() =>
      parseStorageOrigins('https://user:pass@s3.example.com', {
        production: true,
      })
    ).toThrow(/credential/i);
  });

  it('rejects data: and blob: schemes', () => {
    expect(() => parseStorageOrigins('data:', { production: true })).toThrow();
    expect(() => parseStorageOrigins('blob:', { production: true })).toThrow();
  });

  it('returns empty array for empty string', () => {
    expect(parseStorageOrigins('', { production: false })).toEqual([]);
  });

  it('returns empty array for a whitespace-only string', () => {
    expect(parseStorageOrigins('   ', { production: true })).toEqual([]);
  });

  it('rejects an origin with a trailing slash', () => {
    expect(() =>
      parseStorageOrigins('https://s3.example.com/', { production: true })
    ).toThrow(/trailing slash/i);
  });

  it('rejects origin matching DOMAIN_API_BASE_URL', () => {
    const original = process.env.DOMAIN_API_BASE_URL;
    process.env.DOMAIN_API_BASE_URL = 'http://localhost:8000';
    try {
      expect(() =>
        parseStorageOrigins('http://localhost:8000', { production: false })
      ).toThrow(/FastAPI/i);
    } finally {
      process.env.DOMAIN_API_BASE_URL = original;
    }
  });
});
