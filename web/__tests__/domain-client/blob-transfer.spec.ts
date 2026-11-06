import { StorageOriginPolicyImpl } from '../../lib/domain-client/storage-origin-policy';
import {
  DefaultBrowserBlobTransferClient,
  type UploadGrantResponse,
  type DownloadGrant,
} from '../../lib/domain-client/blob-transfer';

describe('StorageOriginPolicy', () => {
  it('accepts an allowlisted origin', () => {
    const policy = new StorageOriginPolicyImpl(['http://localhost:9000']);
    const url = policy.assertAllowed(
      'http://localhost:9000/_local_blob/sig/key'
    );
    expect(url.hostname).toBe('localhost');
  });

  it('rejects a non-allowlisted origin', () => {
    const policy = new StorageOriginPolicyImpl(['http://localhost:9000']);
    expect(() => policy.assertAllowed('http://evil.com/path')).toThrow();
  });

  it('rejects credentials in URL', () => {
    const policy = new StorageOriginPolicyImpl(['http://localhost:9000']);
    expect(() =>
      policy.assertAllowed('http://user:pass@localhost:9000/path')
    ).toThrow();
  });

  it('rejects a URL fragment', () => {
    const policy = new StorageOriginPolicyImpl(['http://localhost:9000']);
    expect(() =>
      policy.assertAllowed('http://localhost:9000/path#frag')
    ).toThrow();
  });

  it('rejects a mismatched port on an otherwise-allowlisted host', () => {
    const policy = new StorageOriginPolicyImpl(['http://localhost:9000']);
    expect(() => policy.assertAllowed('http://localhost:9001/path')).toThrow();
  });
});

describe('DefaultBrowserBlobTransferClient', () => {
  const originPolicy = new StorageOriginPolicyImpl(['http://localhost:9000']);

  it('uploads via presigned POST with every signed field plus the file', async () => {
    const fetchMock = jest.fn().mockResolvedValue({ ok: true, status: 204 });
    const client = new DefaultBrowserBlobTransferClient(
      originPolicy,
      fetchMock as unknown as typeof fetch
    );
    const grant: UploadGrantResponse = {
      kind: 'presigned_post',
      url: 'http://localhost:9000/bucket',
      fields: { key: 'staging/abc.dat', policy: 'xyz' },
      headers: null,
      expires_at: new Date().toISOString(),
    };
    const file = new Blob(['hello'], { type: 'text/plain' });

    await client.upload(grant, file);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [calledUrl, init] = fetchMock.mock.calls[0];
    expect(calledUrl).toBe('http://localhost:9000/bucket');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('omit');
    expect(init.redirect).toBe('error');
    expect(init.body).toBeInstanceOf(FormData);
  });

  it('uploads via signed PUT forwarding exactly the signed headers', async () => {
    const fetchMock = jest.fn().mockResolvedValue({ ok: true, status: 200 });
    const client = new DefaultBrowserBlobTransferClient(
      originPolicy,
      fetchMock as unknown as typeof fetch
    );
    const grant: UploadGrantResponse = {
      kind: 'signed_put',
      url: 'http://localhost:9000/_local_blob/sig/key',
      fields: null,
      headers: { 'x-marketai-signature': 'abc123' },
      expires_at: new Date().toISOString(),
    };
    const file = new Blob(['hello']);

    await client.upload(grant, file);

    expect(fetchMock).toHaveBeenCalledWith(
      'http://localhost:9000/_local_blob/sig/key',
      expect.objectContaining({
        method: 'PUT',
        body: file,
        headers: { 'x-marketai-signature': 'abc123' },
        credentials: 'omit',
        redirect: 'error',
      })
    );
  });

  it('invokes an injected browser fetch with the global receiver', async () => {
    const brandedFetch = jest.fn(function (this: unknown) {
      if (this !== globalThis) {
        throw new TypeError("Failed to execute 'fetch': Illegal invocation");
      }
      return Promise.resolve({ ok: true, status: 200 } as Response);
    });
    const client = new DefaultBrowserBlobTransferClient(
      originPolicy,
      brandedFetch as unknown as typeof fetch
    );
    const grant: UploadGrantResponse = {
      kind: 'signed_put',
      url: 'http://localhost:9000/_local_blob/sig/key',
      fields: null,
      headers: {},
      expires_at: new Date().toISOString(),
    };

    await expect(
      client.upload(grant, new Blob(['x']))
    ).resolves.toBeUndefined();
    expect(brandedFetch).toHaveBeenCalledTimes(1);
  });

  it('rejects an upload grant pointing at a non-allowlisted origin without calling fetch', async () => {
    const fetchMock = jest.fn();
    const client = new DefaultBrowserBlobTransferClient(
      originPolicy,
      fetchMock as unknown as typeof fetch
    );
    const grant: UploadGrantResponse = {
      kind: 'signed_put',
      url: 'http://evil.com/key',
      fields: null,
      headers: {},
      expires_at: new Date().toISOString(),
    };

    await expect(client.upload(grant, new Blob(['x']))).rejects.toThrow(
      'Storage origin not allowed'
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('throws when the storage response is not ok', async () => {
    const fetchMock = jest.fn().mockResolvedValue({ ok: false, status: 500 });
    const client = new DefaultBrowserBlobTransferClient(
      originPolicy,
      fetchMock as unknown as typeof fetch
    );
    const grant: UploadGrantResponse = {
      kind: 'signed_put',
      url: 'http://localhost:9000/key',
      fields: null,
      headers: {},
      expires_at: new Date().toISOString(),
    };

    await expect(client.upload(grant, new Blob(['x']))).rejects.toThrow(
      'Upload failed: 500'
    );
  });

  it('validatedDownloadUrl checks the origin and returns a URL', () => {
    const client = new DefaultBrowserBlobTransferClient(originPolicy);
    const grant: DownloadGrant = {
      url: 'http://localhost:9000/_local_blob/sig/committed-key',
      expires_at: new Date().toISOString(),
      content_disposition: 'attachment; filename="report.pdf"',
    };

    const url = client.validatedDownloadUrl(grant);
    expect(url.hostname).toBe('localhost');
  });

  it('validatedDownloadUrl rejects a non-allowlisted download origin', () => {
    const client = new DefaultBrowserBlobTransferClient(originPolicy);
    const grant: DownloadGrant = {
      url: 'http://evil.com/key',
      expires_at: new Date().toISOString(),
      content_disposition: 'attachment',
    };

    expect(() => client.validatedDownloadUrl(grant)).toThrow();
  });
});
