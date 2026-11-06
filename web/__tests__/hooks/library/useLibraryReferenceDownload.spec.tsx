/**
 * @jest-environment jsdom
 */
import { act, renderHook } from '@testing-library/react';

const mockCreateDownloadIntent = jest.fn();
const mockValidatedDownloadUrl = jest.fn();
const mockNavigateTo = jest.fn();

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: {
      slug: 'acme',
      workspaceId: 'w1',
      role: 'OWNER',
      membershipId: 'm1',
      displayName: 'Acme',
    },
  }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => ({
    createLibraryReferenceDownloadIntent: mockCreateDownloadIntent,
  }),
}));

jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => ({
    validatedDownloadUrl: mockValidatedDownloadUrl,
  }),
}));

jest.mock('@/lib/domain-client/browser-navigation', () => ({
  navigateTo: (...args: unknown[]) => mockNavigateTo(...args),
}));

import { useLibraryReferenceDownload } from '@/hooks/library/useLibraryReferenceDownload';
import messages from '../../../locales/en/common.json';

const META = { etag: null, retryAt: null, location: null };

function setupSuccess(url = 'https://blob.test/signed-download') {
  mockCreateDownloadIntent.mockResolvedValue({
    kind: 'created',
    status: 201,
    data: {
      download_url: url,
      expires_at: '2026-09-05T08:00:00Z',
      content_disposition: 'attachment; filename="asset.jpg"',
    },
    meta: META,
  });
  mockValidatedDownloadUrl.mockReturnValue(new URL(url));
}

describe('useLibraryReferenceDownload', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('starts in idle state', () => {
    const { result } = renderHook(() => useLibraryReferenceDownload());
    expect(result.current.state).toBe('idle');
    expect(result.current.error).toBeNull();
  });

  it('validates the capability and navigates without persisting it', async () => {
    setupSuccess();
    const { result } = renderHook(() => useLibraryReferenceDownload());

    await act(async () => result.current.download('version-1'));

    expect(mockCreateDownloadIntent).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        versionId: 'version-1',
      })
    );
    expect(mockValidatedDownloadUrl).toHaveBeenCalledWith({
      url: 'https://blob.test/signed-download',
      expires_at: '2026-09-05T08:00:00Z',
      content_disposition: 'attachment; filename="asset.jpg"',
    });
    expect(mockNavigateTo).toHaveBeenCalledWith(
      'https://blob.test/signed-download'
    );
    expect(result.current.state).toBe('idle');
    expect(JSON.stringify(result.current)).not.toMatch(
      /blob\.test|signed-download/
    );
  });

  it('sets error state on intent creation failure', async () => {
    mockCreateDownloadIntent.mockResolvedValue({
      kind: 'problem',
      problem: {
        code: 'library_version_withdrawn',
        detail: 'Unreleased version',
      },
      meta: META,
    });
    const { result } = renderHook(() => useLibraryReferenceDownload());

    await act(async () => result.current.download('version-1'));

    expect(result.current.state).toBe('error');
    // Rendered by its code in the interface language (#253), not the detail.
    expect(result.current.error).toBe(
      messages['error-library_version_withdrawn']
    );
    expect(mockNavigateTo).not.toHaveBeenCalled();
  });

  it('fails closed when storage-origin validation rejects the URL', async () => {
    setupSuccess('https://untrusted.test/download');
    mockValidatedDownloadUrl.mockImplementation(() => {
      throw new Error('Storage origin not allowed');
    });
    const { result } = renderHook(() => useLibraryReferenceDownload());

    await act(async () => result.current.download('version-1'));

    expect(result.current.state).toBe('error');
    expect(result.current.error).toBe('Storage origin not allowed');
    expect(mockNavigateTo).not.toHaveBeenCalled();
  });

  it('surfaces reconciliation and resets error state', async () => {
    mockCreateDownloadIntent.mockResolvedValue({
      kind: 'reconciliation_required',
      actionInstanceId: 'download-1',
      staleSince: 1,
    });
    const { result } = renderHook(() => useLibraryReferenceDownload());

    await act(async () => result.current.download('version-1'));
    expect(result.current.error).toContain('requires reconciliation');

    act(() => result.current.reset());
    expect(result.current.state).toBe('idle');
    expect(result.current.error).toBeNull();
  });
});
