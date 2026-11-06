/**
 * @jest-environment jsdom
 */
import { renderHook, waitFor } from '@testing-library/react';

const mockExecute = jest.fn();
const mockBlobClient = {
  upload: jest.fn(),
  validatedDownloadUrl: jest.fn(),
  downloadBlob: jest.fn(),
};

const mockCurrentWorkspace = {
  slug: 'acme',
  workspaceId: 'w1',
  role: 'OWNER',
  membershipId: 'm1',
  displayName: 'Acme',
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: mockCurrentWorkspace }),
}));

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: mockExecute,
    result: undefined,
    problem: undefined,
    isPending: false,
    reset: jest.fn(),
    retry: jest.fn(),
    reconciliation: undefined,
  }),
}));

jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => mockBlobClient,
}));

const mockCreateObjectURL = jest.fn().mockReturnValue('blob:test-url');
const mockRevokeObjectURL = jest.fn();
global.URL.createObjectURL = mockCreateObjectURL;
global.URL.revokeObjectURL = mockRevokeObjectURL;

import { useAssetImageUrl } from '@/hooks/assets/useAssetImageUrl';

// `DownloadGrant` (`@/lib/domain-client/blob-transfer`) is
// `{ url, expires_at, content_disposition }` -- not `{ url, kind, headers }`.
function makeGrant(url: string) {
  return {
    url,
    expires_at: '2099-01-01T00:00:00Z',
    content_disposition: 'attachment',
  };
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe('useAssetImageUrl', () => {
  it('returns null blobUrl initially while loading', () => {
    mockExecute.mockReturnValue(new Promise(() => {}));

    const { result } = renderHook(() => useAssetImageUrl('version-1'));

    expect(result.current.blobUrl).toBeNull();
    expect(result.current.isLoading).toBe(true);
  });

  it('fetches download intent, downloads blob, creates blob URL', async () => {
    const blob = new Blob(['image'], { type: 'image/png' });
    const grant = makeGrant('https://storage.example.com/obj');

    mockExecute.mockResolvedValue({
      kind: 'success',
      data: {
        intent_id: 'i1',
        expires_at: '2026-12-01',
        download_grant: grant,
      },
      meta: { etag: null, retryAt: null, location: null },
    });
    mockBlobClient.downloadBlob.mockResolvedValue(blob);

    const { result } = renderHook(() => useAssetImageUrl('version-1'));

    await waitFor(() => {
      expect(result.current.blobUrl).toBe('blob:test-url');
    });

    expect(result.current.isLoading).toBe(false);
    expect(mockCreateObjectURL).toHaveBeenCalledWith(blob);
  });

  it('revokes the blob URL on unmount', async () => {
    const blob = new Blob(['image']);
    const grant = makeGrant('https://storage.example.com/obj');

    mockExecute.mockResolvedValue({
      kind: 'success',
      data: {
        intent_id: 'i1',
        expires_at: '2026-12-01',
        download_grant: grant,
      },
      meta: { etag: null, retryAt: null, location: null },
    });
    mockBlobClient.downloadBlob.mockResolvedValue(blob);

    const { result, unmount } = renderHook(() => useAssetImageUrl('version-1'));

    await waitFor(() => {
      expect(result.current.blobUrl).toBe('blob:test-url');
    });

    unmount();

    expect(mockRevokeObjectURL).toHaveBeenCalledWith('blob:test-url');
  });

  it('returns problem when download intent fails', async () => {
    mockExecute.mockResolvedValue({
      kind: 'problem',
      problem: {
        code: 'asset_blob_unavailable',
        title: 'Blob unavailable',
        status: 404,
        detail: '',
        instance: '',
        type: '',
        trace_id: '',
        retryable: false,
        field_errors: [],
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() => useAssetImageUrl('version-1'));

    await waitFor(() => {
      expect(result.current.problem).toBeDefined();
    });

    expect(result.current.problem!.code).toBe('asset_blob_unavailable');
    expect(result.current.blobUrl).toBeNull();
  });

  it('aborts the download when version ID changes', async () => {
    const blob = new Blob(['image']);
    const grant = makeGrant('https://storage.example.com/obj');

    mockExecute.mockResolvedValue({
      kind: 'success',
      data: {
        intent_id: 'i1',
        expires_at: '2026-12-01',
        download_grant: grant,
      },
      meta: { etag: null, retryAt: null, location: null },
    });
    mockBlobClient.downloadBlob.mockResolvedValue(blob);

    const { result, rerender } = renderHook(
      ({ versionId }) => useAssetImageUrl(versionId),
      { initialProps: { versionId: 'v1' } }
    );

    await waitFor(() => {
      expect(result.current.blobUrl).toBe('blob:test-url');
    });

    rerender({ versionId: 'v2' });

    expect(mockRevokeObjectURL).toHaveBeenCalledWith('blob:test-url');
  });
});

/** `AssetThumbnailLink` as a list projection embeds it (Issue #159). */
function makeThumbnail(url: string) {
  return {
    url,
    expires_at: '2099-01-01T00:00:00Z',
    mime_type: 'image/jpeg',
    width: 512,
    height: 384,
  };
}

describe('useAssetImageUrl with a thumbnail link', () => {
  it('fetches the signed thumbnail directly, without a download intent', async () => {
    const blob = new Blob(['thumb'], { type: 'image/jpeg' });
    mockBlobClient.downloadBlob.mockResolvedValue(blob);

    const { result } = renderHook(() =>
      useAssetImageUrl('version-1', {
        thumbnail: makeThumbnail('https://storage.example.com/thumb.jpg'),
      })
    );

    await waitFor(() => {
      expect(result.current.blobUrl).toBe('blob:test-url');
    });

    expect(result.current.source).toBe('thumbnail');
    expect(result.current.isLoading).toBe(false);
    expect(result.current.problem).toBeUndefined();
    expect(mockExecute).not.toHaveBeenCalled();
    expect(mockBlobClient.downloadBlob).toHaveBeenCalledTimes(1);
    expect(mockBlobClient.downloadBlob.mock.calls[0][0]).toMatchObject({
      url: 'https://storage.example.com/thumb.jpg',
      expires_at: '2099-01-01T00:00:00Z',
    });
    expect(mockCreateObjectURL).toHaveBeenCalledWith(blob);
  });

  it('falls back to the original through the download intent when the thumbnail fetch fails', async () => {
    const original = new Blob(['original'], { type: 'image/png' });
    mockBlobClient.downloadBlob
      .mockRejectedValueOnce(new Error('Download failed: 403'))
      .mockResolvedValueOnce(original);
    mockExecute.mockResolvedValue({
      kind: 'success',
      data: {
        intent_id: 'i1',
        expires_at: '2026-12-01',
        download_grant: makeGrant('https://storage.example.com/original.png'),
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useAssetImageUrl('version-1', {
        thumbnail: makeThumbnail('https://storage.example.com/expired.jpg'),
      })
    );

    await waitFor(() => {
      expect(result.current.blobUrl).toBe('blob:test-url');
    });

    expect(result.current.source).toBe('original');
    expect(result.current.problem).toBeUndefined();
    expect(mockExecute).toHaveBeenCalledTimes(1);
    expect(mockExecute.mock.calls[0][0]).toMatchObject({
      pathParams: { version_id: 'version-1' },
    });
    expect(mockBlobClient.downloadBlob).toHaveBeenCalledTimes(2);
    expect(mockBlobClient.downloadBlob.mock.calls[1][0]).toMatchObject({
      url: 'https://storage.example.com/original.png',
    });
    expect(mockCreateObjectURL).toHaveBeenCalledWith(original);
  });

  it('uses the download intent when the link is null, like the Lightbox', async () => {
    const original = new Blob(['original']);
    mockBlobClient.downloadBlob.mockResolvedValue(original);
    mockExecute.mockResolvedValue({
      kind: 'success',
      data: {
        intent_id: 'i1',
        expires_at: '2026-12-01',
        download_grant: makeGrant('https://storage.example.com/original.png'),
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result } = renderHook(() =>
      useAssetImageUrl('version-1', { thumbnail: null })
    );

    await waitFor(() => {
      expect(result.current.blobUrl).toBe('blob:test-url');
    });

    expect(result.current.source).toBe('original');
    expect(mockExecute).toHaveBeenCalledTimes(1);
  });

  it('keeps the loaded image when a re-polled page re-signs the same link', async () => {
    const blob = new Blob(['thumb']);
    mockBlobClient.downloadBlob.mockResolvedValue(blob);

    const { result, rerender } = renderHook(
      ({ url }) =>
        useAssetImageUrl('version-1', { thumbnail: makeThumbnail(url) }),
      { initialProps: { url: 'https://storage.example.com/thumb.jpg?sig=1' } }
    );

    await waitFor(() => {
      expect(result.current.blobUrl).toBe('blob:test-url');
    });

    rerender({ url: 'https://storage.example.com/thumb.jpg?sig=2' });

    expect(mockBlobClient.downloadBlob).toHaveBeenCalledTimes(1);
    expect(mockRevokeObjectURL).not.toHaveBeenCalled();
    expect(result.current.blobUrl).toBe('blob:test-url');
  });

  it('loads the thumbnail once a re-polled item gains one', async () => {
    const blob = new Blob(['thumb']);
    mockBlobClient.downloadBlob.mockResolvedValue(blob);
    mockExecute.mockResolvedValue({
      kind: 'success',
      data: {
        intent_id: 'i1',
        expires_at: '2026-12-01',
        download_grant: makeGrant('https://storage.example.com/original.png'),
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    const { result, rerender } = renderHook(
      ({ thumbnail }) => useAssetImageUrl('version-1', { thumbnail }),
      {
        initialProps: {
          thumbnail: null as ReturnType<typeof makeThumbnail> | null,
        },
      }
    );

    await waitFor(() => {
      expect(result.current.source).toBe('original');
    });

    rerender({
      thumbnail: makeThumbnail('https://storage.example.com/thumb.jpg'),
    });

    await waitFor(() => {
      expect(result.current.source).toBe('thumbnail');
    });
    expect(mockExecute).toHaveBeenCalledTimes(1);
    expect(mockBlobClient.downloadBlob).toHaveBeenCalledTimes(2);
  });
});
