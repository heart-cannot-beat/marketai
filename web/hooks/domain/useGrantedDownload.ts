import { useCallback, useEffect, useRef, useState } from 'react';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';
import { navigateTo } from '@/lib/domain-client/browser-navigation';
import type { DownloadGrant } from '@/lib/domain-client/blob-transfer';

export type GrantedDownloadState = 'idle' | 'navigating';

export interface DownloadTransferError {
  status: number;
  origin: string;
  message: string;
}

export type GrantedDownloadResult =
  | { kind: 'started' }
  | { kind: 'transfer_error'; error: DownloadTransferError };

export interface UseGrantedDownloadResult {
  startDownload: (grant: DownloadGrant) => Promise<GrantedDownloadResult>;
  downloadState: GrantedDownloadState;
}

function safeOriginOf(url: string): string {
  try {
    return new URL(url).origin;
  } catch {
    return 'unknown';
  }
}

export function useGrantedDownload(): UseGrantedDownloadResult {
  const blobClient = useBlobTransferClient();
  const [downloadState, setDownloadState] =
    useState<GrantedDownloadState>('idle');
  const resetTimer = useRef<ReturnType<typeof setTimeout>>();

  useEffect(
    () => () => {
      if (resetTimer.current) clearTimeout(resetTimer.current);
    },
    []
  );

  const startDownload = useCallback(
    async (grant: DownloadGrant): Promise<GrantedDownloadResult> => {
      if (resetTimer.current) {
        clearTimeout(resetTimer.current);
        resetTimer.current = undefined;
      }

      let validatedUrl: URL;
      try {
        validatedUrl = blobClient.validatedDownloadUrl(grant);
      } catch {
        return {
          kind: 'transfer_error',
          error: {
            status: 0,
            origin: safeOriginOf(grant.url),
            message: 'Storage origin validation failed',
          },
        };
      }

      setDownloadState('navigating');
      navigateTo(validatedUrl.href);

      resetTimer.current = setTimeout(() => {
        setDownloadState('idle');
      }, 2000);

      return { kind: 'started' };
    },
    [blobClient]
  );

  return { startDownload, downloadState };
}
