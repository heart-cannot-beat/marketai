/**
 * @jest-environment jsdom
 */
import { renderHook, act } from '@testing-library/react';

const mockBlobClient = {
  upload: jest.fn(),
  validatedDownloadUrl: jest.fn(),
  downloadBlob: jest.fn(),
};

const mockNavigateTo = jest.fn();

jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => mockBlobClient,
}));

jest.mock('@/lib/domain-client/browser-navigation', () => ({
  navigateTo: (...args: unknown[]) => mockNavigateTo(...args),
}));

import { useGrantedDownload } from '@/hooks/domain/useGrantedDownload';

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

describe('useGrantedDownload', () => {
  it('validates the grant URL and navigates to it', async () => {
    const validated = new URL('https://storage.example.com/zip?sig=abc');
    mockBlobClient.validatedDownloadUrl.mockReturnValue(validated);

    const { result } = renderHook(() => useGrantedDownload());

    let outcome: any;
    await act(async () => {
      outcome = await result.current.startDownload(
        makeGrant('https://storage.example.com/zip?sig=abc')
      );
    });

    expect(outcome.kind).toBe('started');
    expect(mockBlobClient.validatedDownloadUrl).toHaveBeenCalledWith(
      expect.objectContaining({
        url: 'https://storage.example.com/zip?sig=abc',
      })
    );
    expect(mockNavigateTo).toHaveBeenCalledWith(validated.href);
  });

  it('returns transfer_error when origin validation fails', async () => {
    mockBlobClient.validatedDownloadUrl.mockImplementation(() => {
      throw new Error('origin blocked');
    });

    const { result } = renderHook(() => useGrantedDownload());

    let outcome: any;
    await act(async () => {
      outcome = await result.current.startDownload(
        makeGrant('https://evil.com/zip')
      );
    });

    expect(outcome.kind).toBe('transfer_error');
    expect(outcome.error.origin).toBe('https://evil.com');
    expect(mockNavigateTo).not.toHaveBeenCalled();
  });

  it('tracks download state through idle → navigating → idle', async () => {
    jest.useFakeTimers();
    const validated = new URL('https://storage.example.com/zip');
    mockBlobClient.validatedDownloadUrl.mockReturnValue(validated);

    const { result } = renderHook(() => useGrantedDownload());
    expect(result.current.downloadState).toBe('idle');

    await act(async () => {
      await result.current.startDownload(
        makeGrant('https://storage.example.com/zip')
      );
    });

    expect(result.current.downloadState).toBe('navigating');

    act(() => {
      jest.advanceTimersByTime(2500);
    });

    expect(result.current.downloadState).toBe('idle');
    jest.useRealTimers();
  });
});
