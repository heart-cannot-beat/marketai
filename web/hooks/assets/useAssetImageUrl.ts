import { useCallback, useEffect, useRef, useState } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

/** `AssetThumbnailLink` -- the signed 512px derivative link a list
 * projection embeds per item (`AssetVersionSummary.thumbnail`,
 * `GenerationRunItemSummary.thumbnail`). */
export type AssetThumbnailLink = components['schemas']['AssetThumbnailLink'];

export type AssetImageSource = 'thumbnail' | 'original';

export interface UseAssetImageUrlOptions {
  /**
   * The signed thumbnail link for this version, when the list projection
   * carried one. The hook fetches it directly -- one small GET, no
   * download-intent round trip -- and falls back to the original through
   * the download-intent route when it is absent or the fetch fails (an
   * expired link after a long-lived page, a transient storage error).
   * Only its presence re-triggers a load: a re-polled list returning a
   * freshly signed URL for the same version keeps the image already shown.
   */
  thumbnail?: AssetThumbnailLink | null;
}

export interface UseAssetImageUrlResult {
  blobUrl: string | null;
  isLoading: boolean;
  problem: ProblemDetails | undefined;
  /** Which bytes `blobUrl` holds; `null` while nothing is loaded. */
  source: AssetImageSource | null;
}

export function useAssetImageUrl(
  versionId: string | undefined,
  options: UseAssetImageUrlOptions = {}
): UseAssetImageUrlResult {
  const command = useDomainCommand(
    'create_generation_asset_version_download_intent'
  );
  const blobClient = useBlobTransferClient();
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [problem, setProblem] = useState<ProblemDetails>();
  const [source, setSource] = useState<AssetImageSource | null>(null);
  const abortRef = useRef<AbortController>();
  const currentUrlRef = useRef<string | null>(null);
  // Read through a ref inside the effect so a re-signed URL for the same
  // version (the Run page re-polls its items every 1-2 s) never re-fetches.
  const thumbnailRef = useRef<AssetThumbnailLink | null | undefined>(
    options.thumbnail
  );
  thumbnailRef.current = options.thumbnail;
  const hasThumbnail = Boolean(options.thumbnail?.url);

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
      setSource(null);
      return;
    }

    revoke();
    setBlobUrl(null);
    setProblem(undefined);
    setSource(null);
    setIsLoading(true);

    const controller = new AbortController();
    abortRef.current = controller;

    const publish = (blob: Blob, loadedFrom: AssetImageSource) => {
      const url = URL.createObjectURL(blob);
      currentUrlRef.current = url;
      setBlobUrl(url);
      setSource(loadedFrom);
    };

    (async () => {
      const thumbnail = thumbnailRef.current;
      if (thumbnail?.url) {
        try {
          const blob = await blobClient.downloadBlob(
            {
              url: thumbnail.url,
              expires_at: thumbnail.expires_at,
              content_disposition: 'inline',
            },
            { signal: controller.signal }
          );
          if (controller.signal.aborted) return;
          publish(blob, 'thumbnail');
          setIsLoading(false);
          return;
        } catch {
          if (controller.signal.aborted) return;
          // Fall through to the original: the thumbnail is a cache of it.
        }
      }

      const actionInstanceId = `asset-image:${versionId}`;
      // `create_generation_asset_version_download_intent` takes the
      // version id as a path param (`{ version_id: string }`), not a
      // request body -- its `requestBody` is `never`
      // (`generated/operation-map.d.ts`), and
      // `callCommandOperation`/`DomainGateway.createGenerationAssetVersionDownloadIntent`
      // read `versionId` off `pathParams`.
      const outcome = await command.execute({
        pathParams: { version_id: versionId },
        actionInstanceId,
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

      // The generated success type for this operation
      // (`app__generation__response_models__DownloadIntentResponse`) has
      // not caught up with the backend response model
      // (`app/generation/response_models.py::DownloadIntentResponse`),
      // which does populate `download_grant` for the asset-version route.
      const grant = (outcome.data as any).download_grant;

      try {
        const blob = await blobClient.downloadBlob(grant, {
          signal: controller.signal,
        });

        if (controller.signal.aborted) return;

        publish(blob, 'original');
      } catch {
        if (!controller.signal.aborted) {
          setProblem({
            type: '',
            title: 'Transfer failed',
            status: 0,
            detail: 'Failed to download asset image',
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
  }, [versionId, hasThumbnail]); // eslint-disable-line react-hooks/exhaustive-deps

  return { blobUrl, isLoading, problem, source };
}
