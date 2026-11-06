/**
 * @jest-environment jsdom
 */
import { renderHook, act } from '@testing-library/react';
import { useDownload } from '@/hooks/domain/useDownload';

const mockGateway = { createDownloadIntent: jest.fn() };
const mockBlobClient = {
  upload: jest.fn(),
  validatedDownloadUrl: jest.fn(),
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme' } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => mockBlobClient,
}));

// `window.location` is unforgeable in jsdom (matching real browsers) — it
// cannot be deleted, replaced, or have `assign` stubbed directly. The hook
// calls navigation through `navigateTo` (browser-navigation.ts) precisely so
// this can be mocked instead.
const assignMock = jest.fn();
jest.mock('@/lib/domain-client/browser-navigation', () => ({
  navigateTo: (url: string) => assignMock(url),
}));

describe('useDownload', () => {
  beforeEach(() => jest.clearAllMocks());

  it('starts in the idle state with no problem', () => {
    const { result } = renderHook(() => useDownload());
    expect(result.current.downloadState).toBe('idle');
    expect(result.current.problem).toBeUndefined();
  });

  it('completes download flow and triggers navigation with the validated URL', async () => {
    mockGateway.createDownloadIntent.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: {
        intent_id: 'di1',
        expires_at: new Date().toISOString(),
        download_grant: {
          url: 'https://s3.example.com/bucket/file?sig=abc',
          expires_at: new Date().toISOString(),
          content_disposition: 'attachment; filename="asset.png"',
        },
      },
      meta: {},
    });
    mockBlobClient.validatedDownloadUrl.mockReturnValue(
      new URL('https://s3.example.com/bucket/file?sig=abc')
    );

    const { result } = renderHook(() => useDownload());

    let res: Awaited<ReturnType<typeof result.current.requestDownload>>;
    await act(async () => {
      res = await result.current.requestDownload({
        objectRef: {
          owner_kind: 'product',
          owner_id: 'p1',
          committed_blob_ref_key: 'k1',
          committed_blob_ref_version: 'v1',
        },
        filename: 'asset.png',
        actionInstanceId: 'dl-1',
      });
    });

    expect(res!.kind).toBe('started');
    expect(mockGateway.createDownloadIntent).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      actionInstanceId: 'dl-1',
      body: {
        owner_kind: 'product',
        owner_id: 'p1',
        committed_blob_ref_key: 'k1',
        committed_blob_ref_version: 'v1',
        filename: 'asset.png',
      },
    });
    expect(mockBlobClient.validatedDownloadUrl).toHaveBeenCalledWith({
      url: 'https://s3.example.com/bucket/file?sig=abc',
      expires_at: expect.any(String),
      content_disposition: expect.any(String),
    });
    expect(assignMock).toHaveBeenCalledWith(
      'https://s3.example.com/bucket/file?sig=abc'
    );
    expect(result.current.downloadState).toBe('navigating');
  });

  it('never exposes the signed URL through problem/state — only navigation is observable', async () => {
    mockGateway.createDownloadIntent.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: {
        intent_id: 'di2',
        expires_at: new Date().toISOString(),
        download_grant: {
          url: 'https://s3.example.com/secret',
          expires_at: new Date().toISOString(),
          content_disposition: 'attachment',
        },
      },
      meta: {},
    });
    mockBlobClient.validatedDownloadUrl.mockReturnValue(
      new URL('https://s3.example.com/secret')
    );

    const { result } = renderHook(() => useDownload());

    let res: Awaited<ReturnType<typeof result.current.requestDownload>>;
    await act(async () => {
      res = await result.current.requestDownload({
        objectRef: {},
        filename: 'x.png',
        actionInstanceId: 'dl-3',
      });
    });

    expect(JSON.stringify(res!)).not.toContain('secret');
    expect(result.current.problem).toBeUndefined();
  });

  it('returns problem on 403 and sets problem state without navigating', async () => {
    mockGateway.createDownloadIntent.mockResolvedValue({
      kind: 'problem',
      status: 403,
      problem: { type: 'forbidden', title: 'Forbidden', status: 403 },
      meta: {},
    });

    const { result } = renderHook(() => useDownload());

    let res: Awaited<ReturnType<typeof result.current.requestDownload>>;
    await act(async () => {
      res = await result.current.requestDownload({
        objectRef: {},
        filename: 'x.png',
        actionInstanceId: 'dl-2',
      });
    });

    expect(res!.kind).toBe('problem');
    expect(result.current.problem).toEqual({
      type: 'forbidden',
      title: 'Forbidden',
      status: 403,
    });
    expect(result.current.downloadState).toBe('idle');
    expect(assignMock).not.toHaveBeenCalled();
  });

  it('surfaces reconciliation_required without navigating', async () => {
    mockGateway.createDownloadIntent.mockResolvedValue({
      kind: 'reconciliation_required',
      actionInstanceId: 'dl-4',
      staleSince: 12345,
    });

    const { result } = renderHook(() => useDownload());

    let res: Awaited<ReturnType<typeof result.current.requestDownload>>;
    await act(async () => {
      res = await result.current.requestDownload({
        objectRef: {},
        filename: 'x.png',
        actionInstanceId: 'dl-4',
      });
    });

    expect(res!).toEqual({
      kind: 'reconciliation_required',
      actionInstanceId: 'dl-4',
      staleSince: 12345,
    });
    expect(assignMock).not.toHaveBeenCalled();
  });

  it('returns transfer_error and never navigates when the storage origin is rejected', async () => {
    mockGateway.createDownloadIntent.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: {
        intent_id: 'di5',
        expires_at: new Date().toISOString(),
        download_grant: {
          url: 'https://evil.example.com/file',
          expires_at: new Date().toISOString(),
          content_disposition: 'attachment',
        },
      },
      meta: {},
    });
    mockBlobClient.validatedDownloadUrl.mockImplementation(() => {
      throw new Error('Storage origin not allowed: https://evil.example.com');
    });

    const { result } = renderHook(() => useDownload());

    let res: Awaited<ReturnType<typeof result.current.requestDownload>>;
    await act(async () => {
      res = await result.current.requestDownload({
        objectRef: {},
        filename: 'x.png',
        actionInstanceId: 'dl-5',
      });
    });

    expect(res!.kind).toBe('transfer_error');
    if (res!.kind === 'transfer_error') {
      expect(res!.error.origin).toBe('https://evil.example.com');
      expect(res!.error.status).toBe(0);
    }
    expect(assignMock).not.toHaveBeenCalled();
    expect(result.current.downloadState).toBe('idle');
  });
});
