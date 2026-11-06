import { useEffect, useRef, useState } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';

export interface UseIntakeImageUrlResult {
  blobUrl: string | null;
  isLoading: boolean;
  failed: boolean;
}

/**
 * Object URL for one candidate image of an intake -- the released, gated
 * derivative only (`create_product_intake_image_download_intent`). Same
 * shape as `useReferenceImageUrl`: the signed capability is used once to
 * fetch the bytes straight from storage and is never kept or rendered.
 */
export function useIntakeImageUrl(
  intakeId: string | null,
  imageId: string | null,
  enabled = true
): UseIntakeImageUrlResult {
  const command = useDomainCommand(
    'create_product_intake_image_download_intent'
  );
  const blobClient = useBlobTransferClient();
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const currentUrlRef = useRef<string | null>(null);

  useEffect(() => {
    const revoke = () => {
      if (currentUrlRef.current) {
        URL.revokeObjectURL(currentUrlRef.current);
        currentUrlRef.current = null;
      }
    };
    revoke();
    setBlobUrl(null);
    setFailed(false);
    if (!intakeId || !imageId || !enabled) {
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    const controller = new AbortController();

    (async () => {
      const outcome = await command.execute({
        pathParams: { intake_id: intakeId, image_id: imageId },
        actionInstanceId: `intake-image:${intakeId}:${imageId}`,
      });
      if (controller.signal.aborted) return;
      if (outcome.kind !== 'success') {
        setFailed(true);
        setIsLoading(false);
        return;
      }
      try {
        const blob = await blobClient.downloadBlob(
          {
            url: outcome.data.download_url,
            expires_at: outcome.data.expires_at,
            content_disposition: 'inline',
          },
          { signal: controller.signal }
        );
        if (controller.signal.aborted) return;
        const url = URL.createObjectURL(blob);
        currentUrlRef.current = url;
        setBlobUrl(url);
      } catch {
        if (!controller.signal.aborted) setFailed(true);
      } finally {
        if (!controller.signal.aborted) setIsLoading(false);
      }
    })();

    return () => {
      controller.abort();
      revoke();
    };
  }, [intakeId, imageId, enabled]); // eslint-disable-line react-hooks/exhaustive-deps

  return { blobUrl, isLoading, failed };
}
