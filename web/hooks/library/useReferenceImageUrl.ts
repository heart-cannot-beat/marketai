import { useCallback, useEffect, useRef, useState } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

export interface UseReferenceImageUrlResult {
  blobUrl: string | null;
  isLoading: boolean;
  problem: ProblemDetails | undefined;
}

/**
 * Object URL for a published reference's controlled derivative -- the same
 * shape as `useAssetImageUrl`: a download intent yields a short-lived signed
 * capability for the storage origin, `BrowserBlobTransferClient` fetches the
 * bytes directly (never through Next.js), and the signed URL is never kept.
 */
export function useReferenceImageUrl(
  versionId: string | undefined
): UseReferenceImageUrlResult {
  const command = useDomainCommand('create_library_reference_download_intent');
  const blobClient = useBlobTransferClient();
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [problem, setProblem] = useState<ProblemDetails>();
  const currentUrlRef = useRef<string | null>(null);

  const revoke = useCallback(() => {
    if (currentUrlRef.current) {
      URL.revokeObjectURL(currentUrlRef.current);
      currentUrlRef.current = null;
    }
  }, []);

  useEffect(() => {
    if (!versionId) {
      revoke();
      setBlobUrl(null);
      setIsLoading(false);
      setProblem(undefined);
      return;
    }

    revoke();
    setBlobUrl(null);
    setProblem(undefined);
    setIsLoading(true);

    const controller = new AbortController();

    (async () => {
      const outcome = await command.execute({
        pathParams: { version_id: versionId },
        actionInstanceId: `reference-image:${versionId}`,
      });

      if (controller.signal.aborted) return;

      if (outcome.kind === 'problem') {
        setProblem(outcome.problem);
        setIsLoading(false);
        return;
      }

      if (outcome.kind === 'reconciliation') {
        setIsLoading(false);
        return;
      }

      const grant = outcome.data;

      try {
        const blob = await blobClient.downloadBlob(
          {
            url: grant.download_url,
            expires_at: grant.expires_at,
            content_disposition: grant.content_disposition,
          },
          { signal: controller.signal }
        );

        if (controller.signal.aborted) return;

        const url = URL.createObjectURL(blob);
        currentUrlRef.current = url;
        setBlobUrl(url);
      } catch {
        if (!controller.signal.aborted) {
          setProblem({
            type: '',
            title: 'Transfer failed',
            status: 0,
            detail: 'Failed to download reference image',
            instance: '',
            code: 'transfer_error',
            trace_id: '',
            retryable: true,
            field_errors: [],
          });
        }
      } finally {
        if (!controller.signal.aborted) setIsLoading(false);
      }
    })();

    return () => {
      controller.abort();
      revoke();
    };
  }, [versionId]); // eslint-disable-line react-hooks/exhaustive-deps

  return { blobUrl, isLoading, problem };
}
