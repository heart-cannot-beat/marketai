import { useCallback, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { DomainProblemError, errorMessage } from '@/lib/i18n/domain-errors';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';
import { navigateTo } from '@/lib/domain-client/browser-navigation';

export type DownloadState = 'idle' | 'requesting' | 'downloading' | 'error';

export interface UseLibraryReferenceDownloadResult {
  download: (versionId: string) => Promise<void>;
  state: DownloadState;
  error: string | null;
  reset: () => void;
}

export function useLibraryReferenceDownload(): UseLibraryReferenceDownloadResult {
  const { t } = useTranslation('common');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const blobClient = useBlobTransferClient();
  const [state, setState] = useState<DownloadState>('idle');
  const [error, setError] = useState<string | null>(null);

  const download = useCallback(
    async (versionId: string) => {
      setState('requesting');
      setError(null);

      try {
        const result = await gateway.createLibraryReferenceDownloadIntent({
          workspaceSlug: current.slug,
          actionInstanceId: newActionId(),
          versionId,
        });

        if (result.kind === 'reconciliation_required') {
          throw new Error('Download outcome requires reconciliation');
        }

        if (result.kind === 'problem') {
          throw new DomainProblemError(result.problem);
        }

        const downloadUrl = blobClient.validatedDownloadUrl({
          url: result.data.download_url,
          expires_at: result.data.expires_at,
          content_disposition: result.data.content_disposition,
        });

        setState('downloading');

        // Open in new tab — browser handles the download.
        // The signed URL is used once and never persisted.
        navigateTo(downloadUrl.href);

        setState('idle');
      } catch (err) {
        setState('error');
        setError(
          err instanceof DomainProblemError
            ? errorMessage(t, err.problem)
            : (err as Error).message
        );
      }
    },
    [blobClient, current.slug, gateway, t]
  );

  const reset = useCallback(() => {
    setState('idle');
    setError(null);
  }, []);

  return { download, state, error, reset };
}

function newActionId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? `reference-download:${crypto.randomUUID()}`
    : `reference-download:${Math.random().toString(36).slice(2)}`;
}
