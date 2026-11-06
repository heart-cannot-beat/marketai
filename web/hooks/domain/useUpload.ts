import { useCallback, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import {
  useBlobTransferClient,
  useFileDigest,
} from '@/lib/domain-client/upload-deps';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import type { ReconciliationRequired } from '@/lib/domain-client/intent';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type CreateUploadIntentRequest =
  components['schemas']['CreateUploadIntentRequest'];

/**
 * The three-phase private upload flow (spec §9). `startUpload` hides
 * `createUploadIntent` -> `BrowserBlobTransferClient.upload` ->
 * `finalizeUpload` behind a single call — callers never see the intermediate
 * grant or drive the phases themselves.
 */
export type UploadState =
  | 'idle'
  | 'hashing'
  | 'requesting_grant'
  | 'transferring'
  | 'finalizing'
  | 'awaiting_verification';

/**
 * Object-storage transfer failures (Phase 2) never masquerade as
 * `ProblemDetails` — the BFF/FastAPI never saw the request, so there is no
 * `ProblemDetails` to report. Per spec §9.4, this carries only a safe HTTP
 * status, the storage origin and a user-readable message — never the full
 * capability URL, query string, signed fields or upload form fields, all of
 * which could leak a still-valid signed grant into logs or UI state.
 */
export interface TransferError {
  status: number;
  origin: string;
  message: string;
}

export type UploadStartResult =
  | { kind: 'accepted'; uploadIntentId: string; operationRecordId: string }
  | { kind: 'problem'; problem: ProblemDetails }
  | ReconciliationRequired
  | { kind: 'transfer_error'; error: TransferError };

export interface StartUploadParams {
  file: File;
  ownerKind: string;
  ownerId: string;
  /**
   * Stable identity for this logical upload action (spec §6.10, §9.2) — the
   * hook derives two further, phase-scoped identities from it
   * (`upload:{id}:create`, `upload:{id}:finalize`) so `IntentTracker` never
   * conflates the create and finalize legs of the same upload as a single
   * retryable operation.
   */
  actionInstanceId: string;
}

export interface UseUploadResult {
  startUpload: (params: StartUploadParams) => Promise<UploadStartResult>;
  state: UploadState;
  hashProgress: number;
}

const UNKNOWN_ORIGIN = 'unknown';

/** Origin only — never the full grant URL (query/signature/form fields). */
function safeOriginOf(url: string): string {
  try {
    return new URL(url).origin;
  } catch {
    return UNKNOWN_ORIGIN;
  }
}

/**
 * `DefaultBrowserBlobTransferClient` throws a plain `Error` (not a typed
 * exception) on a non-ok storage response, with the status embedded in the
 * message (`Upload failed: 500`) — this recovers it without ever
 * re-surfacing the raw error message (which could echo response detail) to
 * the caller. A rejection with no recoverable status (an origin-policy
 * rejection, a network failure) reports `0`.
 */
function statusFromTransferError(err: unknown): number {
  if (err instanceof Error) {
    const match = /Upload failed: (\d+)/.exec(err.message);
    if (match) {
      return Number(match[1]);
    }
  }
  return 0;
}

/**
 * Orchestrates the private-upload flow (spec §9) behind a single
 * `startUpload()` call. Every gateway call is scoped to
 * `useWorkspace().current.slug` — never `workspaceId` — matching every other
 * domain hook; the BFF re-derives authorization from verified claims
 * server-side regardless of what this hook sends.
 */
export function useUpload(purpose: string): UseUploadResult {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const blobClient = useBlobTransferClient();
  const fileDigest = useFileDigest();

  const [state, setState] = useState<UploadState>('idle');
  const [hashProgress, setHashProgress] = useState(0);

  const startUpload = useCallback(
    async (params: StartUploadParams): Promise<UploadStartResult> => {
      const { file, ownerKind, ownerId, actionInstanceId } = params;
      // Distinct, stable per-phase action identities (spec §9.2) — a
      // concurrent or resumed create and finalize for the same upload never
      // collide in `IntentTracker`.
      const createActionId = `upload:${actionInstanceId}:create`;
      const finalizeActionId = `upload:${actionInstanceId}:finalize`;

      try {
        // Phase 0: hash. Never absorbs the whole file into memory —
        // `FileDigest` streams it (see file-digest.ts).
        setState('hashing');
        setHashProgress(0);
        const checksumSha256B64 = await fileDigest.computeSha256(
          file,
          setHashProgress
        );

        // Phase 1: create the upload intent and receive a storage grant.
        setState('requesting_grant');
        const requestBody: CreateUploadIntentRequest = {
          purpose,
          owner_kind: ownerKind,
          owner_id: ownerId,
          filename: file.name,
          // `file.type` is the client-declared MIME type, not final truth —
          // Worker finalize re-derives it from magic bytes (spec §9.2).
          content_type: file.type || 'application/octet-stream',
          content_length_min: file.size,
          content_length_max: file.size,
          checksum_sha256_b64: checksumSha256B64,
        };
        const intentResult = await gateway.createUploadIntent({
          workspaceSlug: current.slug,
          actionInstanceId: createActionId,
          body: requestBody,
        });

        if (intentResult.kind === 'reconciliation_required') {
          setState('idle');
          return intentResult;
        }
        if (intentResult.kind === 'problem') {
          setState('idle');
          return { kind: 'problem', problem: intentResult.problem };
        }

        const { intent_id: uploadIntentId, upload_grant: grant } =
          intentResult.data;

        // Phase 2: transfer directly to object storage — bypasses Next.js
        // and FastAPI entirely (spec §7.7). A storage failure is normalized
        // to `TransferError`, never a `ProblemDetails` (the BFF never saw
        // this request).
        setState('transferring');
        try {
          await blobClient.upload(grant, file);
        } catch (err) {
          setState('idle');
          return {
            kind: 'transfer_error',
            error: {
              status: statusFromTransferError(err),
              origin: safeOriginOf(grant.url),
              message: 'File transfer to storage failed',
            },
          };
        }

        // Phase 3: finalize — always asynchronous (202 + Operation).
        setState('finalizing');
        const finalizeResult = await gateway.finalizeUpload({
          workspaceSlug: current.slug,
          uploadIntentId,
          actionInstanceId: finalizeActionId,
        });

        if (finalizeResult.kind === 'reconciliation_required') {
          setState('idle');
          return finalizeResult;
        }
        if (finalizeResult.kind === 'problem') {
          setState('idle');
          return { kind: 'problem', problem: finalizeResult.problem };
        }

        // `awaiting_verification` reflects only that finalize was accepted
        // (202) — not that the file has passed verification. The caller
        // tracks the real outcome via `useOperation(operationRecordId)`
        // (spec §9.2, §9.4).
        setState('awaiting_verification');
        return {
          kind: 'accepted',
          uploadIntentId,
          operationRecordId: finalizeResult.data.operation_id,
        };
      } catch (err) {
        // A thrown (rather than returned) error here is an ambiguous outcome
        // from `DomainGateway` itself (a network failure with no Response at
        // all) — `IntentTracker` has already `retain()`-ed the intent as
        // ambiguous (spec §6.10), same convention as `useDomainCommand`.
        // This hook's only job is to not leave `state` stuck mid-phase
        // before re-throwing to the caller.
        setState('idle');
        throw err;
      }
    },
    [current.slug, gateway, blobClient, fileDigest, purpose]
  );

  return { startUpload, state, hashProgress };
}
