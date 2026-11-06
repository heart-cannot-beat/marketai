import { DefaultBrowserBlobTransferClient } from '@/lib/domain-client/blob-transfer';
import type { DownloadGrant } from '@/lib/domain-client/blob-transfer';
import type { StorageOriginPolicy } from '@/lib/domain-client/storage-origin-policy';

function makePolicy(): StorageOriginPolicy {
  return {
    assertAllowed: (url: string) => new URL(url),
    isAllowed: () => true,
  } as unknown as StorageOriginPolicy;
}

function makeGrant(url: string): DownloadGrant {
  return {
    url,
    expires_at: '2099-01-01T00:00:00Z',
    content_disposition: 'attachment',
  };
}

describe('DefaultBrowserBlobTransferClient.downloadBlob', () => {
  it('fetches the grant URL and returns a Blob', async () => {
    const blob = new Blob(['image-bytes'], { type: 'image/png' });
    const mockFetch = jest
      .fn()
      .mockResolvedValue(new Response(blob, { status: 200 }));
    const client = new DefaultBrowserBlobTransferClient(
      makePolicy(),
      mockFetch
    );

    const result = await client.downloadBlob(
      makeGrant('https://storage.example.com/obj')
    );

    expect(result).toBeInstanceOf(Blob);
    expect(mockFetch).toHaveBeenCalledWith(
      'https://storage.example.com/obj',
      expect.objectContaining({
        method: 'GET',
        credentials: 'omit',
        redirect: 'error',
      })
    );
  });

  it('validates the storage origin before fetching', async () => {
    const rejectPolicy = {
      assertAllowed: () => {
        throw new Error('origin not allowed');
      },
      isAllowed: () => false,
    } as unknown as StorageOriginPolicy;
    const mockFetch = jest.fn();
    const client = new DefaultBrowserBlobTransferClient(
      rejectPolicy,
      mockFetch
    );

    await expect(
      client.downloadBlob(makeGrant('https://evil.com/obj'))
    ).rejects.toThrow('origin not allowed');

    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('rejects non-2xx responses with a sanitized error', async () => {
    const mockFetch = jest
      .fn()
      .mockResolvedValue(new Response('Forbidden', { status: 403 }));
    const client = new DefaultBrowserBlobTransferClient(
      makePolicy(),
      mockFetch
    );

    await expect(
      client.downloadBlob(makeGrant('https://storage.example.com/obj'))
    ).rejects.toThrow('Download failed: 403');
  });

  it('passes the AbortSignal through to fetch', async () => {
    const controller = new AbortController();
    const blob = new Blob(['bytes']);
    const mockFetch = jest
      .fn()
      .mockResolvedValue(new Response(blob, { status: 200 }));
    const client = new DefaultBrowserBlobTransferClient(
      makePolicy(),
      mockFetch
    );

    await client.downloadBlob(makeGrant('https://storage.example.com/obj'), {
      signal: controller.signal,
    });

    expect(mockFetch).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({ signal: controller.signal })
    );
  });

  it('uses credentials omit and redirect error', async () => {
    const blob = new Blob(['bytes']);
    const mockFetch = jest
      .fn()
      .mockResolvedValue(new Response(blob, { status: 200 }));
    const client = new DefaultBrowserBlobTransferClient(
      makePolicy(),
      mockFetch
    );

    await client.downloadBlob(makeGrant('https://storage.example.com/obj'));

    const [, init] = mockFetch.mock.calls[0];
    expect(init.credentials).toBe('omit');
    expect(init.redirect).toBe('error');
  });
});
