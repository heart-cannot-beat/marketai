import { useCallback, useEffect, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useUpload } from '@/hooks/domain/useUpload';
import { useLibraryCache } from './useLibraryCache';
import {
  DomainProblemError,
  type DomainProblem,
} from '@/lib/i18n/domain-errors';

export type UploadPhase =
  | 'idle'
  | 'hashing'
  | 'creating_intent'
  | 'transferring'
  | 'finalizing'
  | 'processing'
  | 'creating_reference'
  | 'reference_processing'
  | 'promoting'
  | 'released'
  | 'rejected'
  | 'failed';

/**
 * Stable gate failure codes the server records on the reference object
 * (`app/library/references.py`). The UI keys its copy on these -- never on
 * the Chinese `detail` text (integration contract §9).
 */
export const CONTENT_SAFETY_UNAVAILABLE = 'content_safety_unavailable';
export const REFERENCE_GATE_REJECTED = 'reference_gate_rejected';

export interface UploadProgress {
  phase: UploadPhase;
  fileName: string;
  bytesTransferred: number;
  totalBytes: number;
  referenceObjectId: string | null;
  /** The published ReferenceAssetVersion the server promoted the object to. */
  referenceVersionId: string | null;
  referenceEntryId: string | null;
  /** The product/person Draft the version was bound to, when requested. */
  boundEntryId: string | null;
  /** Stable server failure code (`content_safety_unavailable`, ...). */
  failureCode: string | null;
  /** Why it failed, rendered by its code (`errorMessage`, #253). */
  problem: DomainProblem | null;
}

export interface ReferenceRightsDeclaration {
  scope: string;
  /** ISO date (YYYY-MM-DD). */
  valid_to: string;
  evidence?: string;
}

export interface ReferenceUploadOptions {
  /** The Library entry the upload zone lives in (owner of the upload intent). */
  contextEntryId: string;
  ownerKind?: 'library_entry' | 'marketing_project';
  /** Bind the promoted version to `contextEntryId`'s Draft (products/persons). */
  bindToContext: boolean;
  /** The uploader's rights declaration, carried into the published version. */
  rights: ReferenceRightsDeclaration;
  displayName?: string;
}

const IDLE_PROGRESS: UploadProgress = {
  phase: 'idle',
  fileName: '',
  bytesTransferred: 0,
  totalBytes: 0,
  referenceObjectId: null,
  referenceVersionId: null,
  referenceEntryId: null,
  boundEntryId: null,
  failureCode: null,
  problem: null,
};

const POLL_INTERVAL_MS = 2000;
const MAX_POLLS = 60;

type OperationSnapshot = {
  state: string;
  resourceId: string | null;
};

export interface UseLibraryReferenceUploadResult {
  upload: (file: File, options: ReferenceUploadOptions) => Promise<void>;
  reset: () => void;
  progress: UploadProgress;
}

export function useLibraryReferenceUpload(): UseLibraryReferenceUploadResult {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const domainUpload = useUpload('reference_asset');
  const { invalidate } = useLibraryCache();
  const [progress, setProgress] = useState<UploadProgress>(IDLE_PROGRESS);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const phaseByUploadState = {
      hashing: 'hashing',
      requesting_grant: 'creating_intent',
      transferring: 'transferring',
      finalizing: 'finalizing',
      awaiting_verification: 'processing',
    } as const;
    const phase =
      phaseByUploadState[domainUpload.state as keyof typeof phaseByUploadState];
    if (phase) {
      setProgress((currentProgress) => ({ ...currentProgress, phase }));
    }
  }, [domainUpload.state]);

  const pollOperation = useCallback(
    async (
      operationId: string,
      signal: AbortSignal
    ): Promise<OperationSnapshot> => {
      for (let i = 0; i < MAX_POLLS; i++) {
        if (signal.aborted) throw new Error('aborted');

        const result = await gateway.pollOperation({
          workspaceSlug: current.slug,
          operationId,
        });

        if (result.kind === 'not_modified') {
          await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
          continue;
        }

        if (result.kind === 'problem') {
          throw new DomainProblemError(result.problem);
        }

        const data = result.data as Record<string, unknown>;
        const state = String(data.state ?? '');
        const resultRef = data.result as Record<string, unknown> | null;
        const snapshot: OperationSnapshot = {
          state,
          resourceId:
            resultRef && resultRef.resource_id
              ? String(resultRef.resource_id)
              : null,
        };

        if (
          state === 'succeeded' ||
          state === 'failed' ||
          state === 'cancelled' ||
          state === 'uncertain'
        ) {
          return snapshot;
        }

        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
      }
      throw new Error('Operation polling timed out');
    },
    [gateway, current.slug]
  );

  /**
   * After a failed gate Operation the object itself carries the stable
   * failure code; read it so the UI can say "content safety service not
   * configured" instead of a bare "operation failed".
   */
  const describeGateFailure = useCallback(
    async (objectId: string | null): Promise<{ code: string | null }> => {
      if (!objectId) return { code: null };
      const result = await gateway.getLibraryReferenceObject({
        workspaceSlug: current.slug,
        objectId,
      });
      if (result.kind !== 'ok') return { code: null };
      const data = result.data as Record<string, unknown>;
      return { code: data.failure_code ? String(data.failure_code) : null };
    },
    [gateway, current.slug]
  );

  const upload = useCallback(
    async (file: File, options: ReferenceUploadOptions) => {
      const controller = new AbortController();
      abortRef.current = controller;
      const { signal } = controller;

      try {
        setProgress({
          ...IDLE_PROGRESS,
          phase: 'hashing',
          fileName: file.name,
          totalBytes: file.size,
        });

        const uploadResult = await domainUpload.startUpload({
          file,
          ownerKind: options.ownerKind ?? 'library_entry',
          ownerId: options.contextEntryId,
          actionInstanceId: newActionId('reference-upload'),
        });
        if (uploadResult.kind === 'problem') {
          throw new DomainProblemError(uploadResult.problem);
        }
        if (uploadResult.kind === 'transfer_error') {
          throw new Error(uploadResult.error.message);
        }
        if (uploadResult.kind === 'reconciliation_required') {
          throw new Error('Upload outcome requires reconciliation');
        }

        const intentId = uploadResult.uploadIntentId;
        const processingOperationId = uploadResult.operationRecordId;
        setProgress((p) => ({ ...p, bytesTransferred: file.size }));

        // Step 4: Poll processing operation
        setProgress((p) => ({ ...p, phase: 'processing' }));
        const verification = await pollOperation(processingOperationId, signal);
        if (verification.state !== 'succeeded') {
          throw new Error('Upload verification did not complete successfully');
        }

        // Step 5: Create reference object. The content-safety execution
        // mode is decided by the server (never sent from here); the rights
        // declaration rides along so the server can promote without raw JSON.
        setProgress((p) => ({ ...p, phase: 'creating_reference' }));

        const refResult = await gateway.createLibraryReferenceObject({
          workspaceSlug: current.slug,
          actionInstanceId: newActionId('reference-create'),
          body: {
            upload_intent_id: intentId,
            filename: file.name,
            declared_mime: file.type || 'application/octet-stream',
            rights: {
              scope: options.rights.scope,
              valid_to: options.rights.valid_to,
              ...(options.rights.evidence
                ? { evidence: options.rights.evidence }
                : {}),
            },
          },
        });

        if (refResult.kind === 'reconciliation_required') {
          throw new Error('Reference creation outcome requires reconciliation');
        }

        if (refResult.kind === 'problem') {
          throw new DomainProblemError(refResult.problem);
        }

        const refOperationId = refResult.data.operation_id;

        // Step 6: Poll reference processing (the Worker gate)
        setProgress((p) => ({
          ...p,
          phase: 'reference_processing',
        }));

        const gate = await pollOperation(refOperationId, signal);
        const referenceObjectId = gate.resourceId;

        if (gate.state !== 'succeeded') {
          const failure = await describeGateFailure(referenceObjectId);
          if (signal.aborted) return;
          const isContentRejection = failure.code === REFERENCE_GATE_REJECTED;
          setProgress((p) => ({
            ...p,
            phase: isContentRejection ? 'rejected' : 'failed',
            referenceObjectId,
            failureCode: failure.code,
            problem: { code: failure.code ?? 'reference_gate_failed' },
          }));
          return;
        }

        if (!referenceObjectId) {
          throw new Error('Reference gate finished without an object id');
        }

        // Step 7: Promote -- the server publishes the reference_asset
        // version from its own gate evidence and, for a product/person
        // context, appends the binding to that entry's Draft.
        setProgress((p) => ({ ...p, phase: 'promoting', referenceObjectId }));

        const promoted = await gateway.promoteLibraryReferenceObject({
          workspaceSlug: current.slug,
          actionInstanceId: newActionId('reference-promote'),
          objectId: referenceObjectId,
          body: {
            ...(options.displayName
              ? { display_name: options.displayName }
              : {}),
            ...(options.bindToContext
              ? { bind: { entry_id: options.contextEntryId } }
              : {}),
          },
        });

        if (promoted.kind === 'reconciliation_required') {
          throw new Error(
            'Reference promotion outcome requires reconciliation'
          );
        }
        if (promoted.kind === 'problem') {
          throw new DomainProblemError(promoted.problem);
        }

        const promotion = promoted.data as Record<string, unknown>;
        const binding = promotion.binding as Record<string, unknown> | null;

        setProgress((p) => ({
          ...p,
          phase: 'released',
          referenceObjectId,
          referenceVersionId: String(promotion.version_id ?? ''),
          referenceEntryId: String(promotion.entry_id ?? ''),
          boundEntryId: binding ? String(binding.entry_id ?? '') : null,
        }));
        await invalidate({
          resources: ['entry', 'entryList', 'bindings', 'events'],
        });
      } catch (err) {
        if ((err as Error).message === 'aborted') return;

        setProgress((p) => ({
          ...p,
          phase: 'failed',
          problem:
            err instanceof DomainProblemError
              ? err.problem
              : { code: 'unknown' },
        }));
      }
    },
    [
      current.slug,
      domainUpload,
      gateway,
      pollOperation,
      describeGateFailure,
      invalidate,
    ]
  );

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setProgress(IDLE_PROGRESS);
  }, []);

  return { upload, reset, progress };
}

function newActionId(prefix: string): string {
  const suffix =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2);
  return `${prefix}:${suffix}`;
}
