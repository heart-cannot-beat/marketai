import { useEffect, useState } from 'react';
import useSWR from 'swr';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';
import type { DownloadGrant } from '@/lib/domain-client/blob-transfer';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useWorkspace } from './WorkspaceProvider';

export function useMarketingImageSource(assetVersionId: string, open: boolean) {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const blobClient = useBlobTransferClient();
  const identity = current.workspaceId + ':' + assetVersionId;
  const [image, setImage] = useState<{ identity: string; url: string } | null>(
    null
  );
  const [imageFailed, setImageFailed] = useState(false);
  const preview = useSWR(
    open
      ? ['marketing-edit-source', current.workspaceId, assetVersionId]
      : null,
    async () => {
      const result = await gateway.createGenerationAssetVersionDownloadIntent({
        workspaceSlug: current.slug,
        versionId: assetVersionId,
        actionInstanceId: crypto.randomUUID(),
      });
      if (!('data' in result) || !result.data?.download_grant)
        throw new Error('marketing-action-failed');
      return {
        ...result.data.download_grant,
        expires_at: result.data.expires_at,
      } as DownloadGrant;
    }
  );
  useEffect(() => {
    setImage(null);
    setImageFailed(false);
    if (!open || !preview.data) return;
    const controller = new AbortController();
    let localUrl: string | null = null;
    blobClient
      .downloadBlob(preview.data, { signal: controller.signal })
      .then((blob) => {
        if (controller.signal.aborted) return;
        localUrl = URL.createObjectURL(blob);
        setImage({ identity, url: localUrl });
      })
      .catch(() => {
        if (!controller.signal.aborted) setImageFailed(true);
      });
    return () => {
      controller.abort();
      if (localUrl) URL.revokeObjectURL(localUrl);
    };
  }, [preview.data, blobClient, identity, open]);
  return {
    imageUrl: open && image?.identity === identity ? image.url : null,
    imageFailed,
    preview,
    onImageError: () => setImageFailed(true),
  };
}
