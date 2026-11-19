import { useEffect, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';

export interface UseIdeasPhotoUrlResult {
  blobUrl: string | null;
  failed: boolean;
}

/**
 * Object URL for one photo of ad ideas (#233): its released, gated
 * derivative only (`create_marketing_ideas_photo_download_intent`), for a
 * photo the user added, which no reading found. Same shape as
 * `useIntakeImageUrl`: the signed link is used once to fetch the bytes and
 * is never kept or rendered.
 */
export function useIdeasPhotoUrl(
  projectId: string | undefined,
  photoId: string,
  enabled = true
): UseIdeasPhotoUrlResult {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const blobClient = useBlobTransferClient();
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const made = useRef<string | null>(null);
  const slug = current.slug;

  useEffect(() => {
    const revoke = () => {
      if (made.current) {
        URL.revokeObjectURL(made.current);
        made.current = null;
      }
    };
    revoke();
    setBlobUrl(null);
    setFailed(false);
    if (!projectId || !enabled) return;
    const controller = new AbortController();
    (async () => {
      try {
        const granted = await gateway.createMarketingIdeasPhotoDownloadIntent({
          workspaceSlug: slug,
          actionInstanceId: `ideas-photo-image:${projectId}:${photoId}`,
          projectId,
          photoId,
        });
        if (controller.signal.aborted) return;
        if (granted.kind !== 'created') {
          setFailed(true);
          return;
        }
        const data = granted.data;
        const blob = await blobClient.downloadBlob(
          {
            url: data.download_url,
            expires_at: data.expires_at,
            content_disposition: 'inline',
          },
          { signal: controller.signal }
        );
        if (controller.signal.aborted) return;
        const url = URL.createObjectURL(blob);
        made.current = url;
        setBlobUrl(url);
      } catch {
        if (!controller.signal.aborted) setFailed(true);
      }
    })();
    return () => {
      controller.abort();
      revoke();
    };
  }, [projectId, photoId, enabled, slug]); // eslint-disable-line react-hooks/exhaustive-deps

  return { blobUrl, failed };
}
