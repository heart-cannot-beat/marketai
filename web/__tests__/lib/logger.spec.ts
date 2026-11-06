import { REDACTED, redact, redactString, logger } from '@/lib/logger';

describe('Lib - logger redaction', () => {
  it('redacts values whose key names are sensitive', () => {
    const output = redact({
      cookie: 'next-auth.session-token=abc123',
      Authorization: 'Bearer abcdefghijklmnop',
      password: 'hunter2',
      databaseUrl: 'postgresql://u:p@host/db',
      apiKey: 'k-123',
      api_key: 'k-123',
      sessionId: 'sess_1',
      nested: { refresh_token: 'r-1', workspace_id: 'ws_1' },
    }) as Record<string, any>;

    expect(output.cookie).toBe(REDACTED);
    expect(output.Authorization).toBe(REDACTED);
    expect(output.password).toBe(REDACTED);
    expect(output.databaseUrl).toBe(REDACTED);
    expect(output.apiKey).toBe(REDACTED);
    expect(output.api_key).toBe(REDACTED);
    expect(output.sessionId).toBe(REDACTED);
    expect(output.nested.refresh_token).toBe(REDACTED);
    // Non-sensitive identifiers survive so audit lines stay useful.
    expect(output.nested.workspace_id).toBe('ws_1');
  });

  it('redacts sensitive value shapes even under innocent key names', () => {
    const jwt = 'eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlLXZhbHVl';

    expect(redactString(`token is ${jwt}`)).not.toContain(jwt);
    expect(
      redactString('Authorization: Bearer abcdefghijklmnop')
    ).not.toContain('abcdefghijklmnop');
    expect(
      redactString('Cookie: __Secure-next-auth.session-token=zzz; other=1')
    ).not.toContain('zzz');
    expect(
      redactString('postgresql://control:s3cret@localhost:5432/control')
    ).not.toContain('s3cret');
  });

  it('redacts provider credential material (BYOK)', () => {
    const samples = [
      'sk-abcdefghijklmnopqrstuvwxyz',
      'AIzaSyA1234567890abcdefghijklmnopqrstu',
      'EAAG1234567890abcdefghijklmnopqrst',
    ];

    for (const secret of samples) {
      expect(redactString(`provider credential ${secret}`)).not.toContain(
        secret
      );
    }
  });

  it('redacts signed-url query secrets while keeping the parameter name', () => {
    const line = redactString(
      'https://objects.example/w/ws_1/a.png?X-Amz-Signature=deadbeefcafe&expires=1'
    );

    expect(line).toContain('X-Amz-Signature=');
    expect(line).not.toContain('deadbeefcafe');
  });

  it('never emits a raw sensitive field through the logger', () => {
    const spy = jest.spyOn(console, 'log').mockImplementation(() => undefined);

    logger.info('control_plane.test', {
      cookie: 'next-auth.session-token=secret-cookie-value',
      detail: 'Bearer aaaaaaaaaaaaaaaaaaaa',
    });

    const line = spy.mock.calls[0][0] as string;

    expect(line).not.toContain('secret-cookie-value');
    expect(line).not.toContain('aaaaaaaaaaaaaaaaaaaa');
    expect(JSON.parse(line).event).toBe('control_plane.test');

    spy.mockRestore();
  });
});
