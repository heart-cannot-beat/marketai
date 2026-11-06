import { useCallback, useEffect, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';
import { navigateTo } from '@/lib/domain-client/browser-navigation';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import type { ReconciliationRequired } from '@/lib/domain-client/intent';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type CreateDownloadIntentRequest =
  components['schemas']['CreateDownloadIntentRequest'];

/**
 * One-shot download flow (spec §7.12): `createDownloadIntent` ->
 * `BrowserBlobTransferClient.validatedDownloadUrl` -> browser navigation.
 * Deliberately not built on SWR — a download is a user-initiated action, not
 * cached data, and the signed URL it produces is a short-term bearer
 * capability that is never cached, recorded, or exposed to the caller (spec
 * §7.14): `requestDownload` reports only that navigation started, never the
 * URL itself.
 */
export type DownloadState = 'idle' | 'requesting' | 'navigating';

/**
 * Mirrors `TransferError` in `useUpload.ts` (spec §9.4): a storage-layer
 * failure that the BFF/FastAPI never saw, so there is no `ProblemDetails` to
 * report — only a safe status, the storage origin, and a user-readable
 * message. For a download this also covers `StorageOriginPolicy` rejecting
 * the grant's origin before it is ever handed to a navigation.
 */
export interface DownloadTransferError {
  status: number;
  origin: string;
  message: string;
}

export type DownloadStartResult =
  | { kind: 'started' }
  | { kind: 'problem'; problem: ProblemDetails }
  | ReconciliationRequired
  | { kind: 'transfer_error'; error: DownloadTransferError };

export interface RequestDownloadParams {
  /**
   * The owner/blob-ref fields `CreateDownloadIntentRequest` needs
   * (`owner_kind`, `owner_id`, `committed_blob_ref_key`,
   * `committed_blob_ref_version`) — kept as a loosely-typed bag here so this
   * hook does not hard-code the shape of every caller's asset reference;
   * merged with `filename` into the typed request body before it is sent.
   */
  objectRef: Record<string, unknown>;
  filename: string;
  /** Stable identity for this logical download action (spec §6.10) — passed straight through to `IntentTracker` via `DomainGateway.createDownloadIntent`. */
  actionInstanceId: string;
}

export interface UseDownloadResult {
  requestDownload: (
    params: RequestDownloadParams
  ) => Promise<DownloadStartResult>;
  downloadState: DownloadState;
  problem: ProblemDetails | undefined;
}

const UNKNOWN_ORIGIN = 'unknown';

/** Origin only — never the full signed URL (query/signature). Mirrors `safeOriginOf` in `useUpload.ts`. */
function safeOriginOf(url: string): string {
  try {
    return new URL(url).origin;
  } catch {
    return UNKNOWN_ORIGIN;
  }
}

/**
 * Scoped to `useWorkspace().current.slug` — never `workspaceId` — matching
 * every other domain hook; the BFF re-derives authorization from verified
 * claims server-side regardless of what this hook sends.
 */
export function useDownload(): UseDownloadResult {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const blobClient = useBlobTransferClient();

  const [downloadState, setDownloadState] = useState<DownloadState>('idle');
  const [problem, setProblem] = useState<ProblemDetails>();

  // Guards the post-navigation idle reset from firing after unmount, and
  // lets a second call cancel a pending reset from a previous one.
  const resetTimer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(
    () => () => {
      if (resetTimer.current) clearTimeout(resetTimer.current);
    },
    []
  );

  const requestDownload = useCallback(
    async (params: RequestDownloadParams): Promise<DownloadStartResult> => {
      const { objectRef, filename, actionInstanceId } = params;
      setProblem(undefined);

      if (resetTimer.current) {
        clearTimeout(resetTimer.current);
        resetTimer.current = undefined;
      }

      setDownloadState('requesting');

      const body = {
        ...objectRef,
        filename,
      } as unknown as CreateDownloadIntentRequest;

      const result = await gateway.createDownloadIntent({
        workspaceSlug: current.slug,
        actionInstanceId,
        body,
      });

      if (result.kind === 'reconciliation_required') {
        setDownloadState('idle');
        return result;
      }

      if (result.kind === 'problem') {
        setDownloadState('idle');
        setProblem(result.problem);
        return { kind: 'problem', problem: result.problem };
      }

      const grant = result.data.download_grant;

      let validatedUrl: URL;
      try {
        validatedUrl = blobClient.validatedDownloadUrl(grant);
      } catch {
        // Never re-surface the thrown error's raw message (spec §7.14, same
        // convention as `useUpload`'s `TransferError`) — only a safe status,
        // the storage origin, and a fixed, user-readable message. The URL
        // itself is never included even indirectly.
        setDownloadState('idle');
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

      // The browser is expected to navigate away; this only resets state for
      // the case where it doesn't (e.g. a same-tab download that keeps the
      // page alive).
      resetTimer.current = setTimeout(() => {
        setDownloadState('idle');
      }, 2000);

      return { kind: 'started' };
    },
    [current.slug, gateway, blobClient]
  );

  return { requestDownload, downloadState, problem };
}
