import { useCallback, useEffect, useRef, useState } from 'react';
import { useSWRConfig } from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useUpload } from '@/hooks/domain/useUpload';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';
import type { PhotoRole } from '@/lib/ideas/photos';
import { ideasProjectKey, type Loaded } from './useIdeasProject';

/**
 * The user's own product photo (#233, design §5.2): uploaded like any
 * reference, through the same upload gate, then added to the product.
 *
 * 1. The file goes to storage through an upload intent of purpose
 *    `reference_asset` (`useUpload`), checked by the platform.
 * 2. `create_library_reference_object` hands it to the reference upload
 *    gate in the Worker; the page waits for that Operation. A rejected
 *    image says why with the gate's own failure code.
 * 3. `add_marketing_ideas_photo` adds the released image as a photo of the
 *    product; its answer is the ideas, written into the ideas page's cache
 *    so every screen shows the new photo at once.
 *
 * No rights are declared on the way: checking the photo afterwards declares
 * them, as for a photo the reading found.
 */

export type PhotoUploadPhase =
  | 'idle'
  | 'uploading'
  | 'checking'
  | 'adding'
  | 'added'
  /** The upload gate did not pass the image (`failureCode` says why). */
  | 'rejected'
  | 'failed';

/** Why it failed when no answer said why: the page words these itself. */
export type PhotoUploadTrouble = 'transfer' | 'timeout' | 'uncertain';

export interface PhotoUploadState {
  phase: PhotoUploadPhase;
  fileName: string;
  /** The gate's stable failure code (`reference_gate_rejected`, ...). */
  failureCode: string | null;
  /** A refusal, rendered by its code. */
  problem: ProblemDetails | null;
  trouble: PhotoUploadTrouble | null;
}

const IDLE: PhotoUploadState = {
  phase: 'idle',
  fileName: '',
  failureCode: null,
  problem: null,
  trouble: null,
};

const POLL_INTERVAL_MS = 2000;
const MAX_POLLS = 90;
const FINISHED = new Set(['succeeded', 'failed', 'cancelled', 'uncertain']);

class Stopped extends Error {}

export interface UseIdeasPhotoUploadResult {
  state: PhotoUploadState;
  upload: (file: File, role: PhotoRole) => Promise<void>;
  reset: () => void;
}

export function useIdeasPhotoUpload(
  projectId: string
): UseIdeasPhotoUploadResult {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const transfer = useUpload('reference_asset');
  const { mutate } = useSWRConfig();
  const [state, setState] = useState<PhotoUploadState>(IDLE);
  const live = useRef<AbortController | null>(null);
  const slug = current.slug;
  const key = ideasProjectKey(current.workspaceId, projectId);

  useEffect(() => () => live.current?.abort(), []);

  const wait = useCallback(
    async (operationId: string, signal: AbortSignal) => {
      for (let poll = 0; poll < MAX_POLLS; poll++) {
        if (signal.aborted) throw new Stopped();
        const result = await gateway.pollOperation({
          workspaceSlug: slug,
          operationId,
        });
        if (result.kind === 'ok') {
          const data = result.data as {
            state?: string;
            result?: { resource_id?: string | null } | null;
          };
          const operationState = String(data.state ?? '');
          if (FINISHED.has(operationState))
            return {
              state: operationState,
              resourceId: data.result?.resource_id ?? null,
            };
        } else if (result.kind === 'problem') {
          throw Object.assign(new Error('problem'), {
            problem: (result as { problem: ProblemDetails }).problem,
          });
        }
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
      }
      throw Object.assign(new Error('timeout'), { trouble: 'timeout' });
    },
    [gateway, slug]
  );

  const upload = useCallback(
    async (file: File, role: PhotoRole) => {
      live.current?.abort();
      const controller = new AbortController();
      live.current = controller;
      const { signal } = controller;
      const action = crypto.randomUUID();
      const set = (next: Partial<PhotoUploadState>) => {
        if (!signal.aborted) setState((before) => ({ ...before, ...next }));
      };
      setState({ ...IDLE, phase: 'uploading', fileName: file.name });
      try {
        const sent = await transfer.startUpload({
          file,
          ownerKind: 'marketing_project',
          ownerId: projectId,
          actionInstanceId: `ideas-photo:${action}`,
        });
        if (sent.kind === 'problem') {
          set({ phase: 'failed', problem: sent.problem });
          return;
        }
        if (sent.kind !== 'accepted') {
          set({ phase: 'failed', trouble: 'transfer' });
          return;
        }
        const received = await wait(sent.operationRecordId, signal);
        if (received.state !== 'succeeded') {
          set({ phase: 'failed', trouble: 'transfer' });
          return;
        }
        set({ phase: 'checking' });
        const created = await gateway.createLibraryReferenceObject({
          workspaceSlug: slug,
          actionInstanceId: `ideas-photo-gate:${action}`,
          body: {
            upload_intent_id: sent.uploadIntentId,
            filename: file.name,
            declared_mime: file.type || 'application/octet-stream',
          },
        });
        if (created.kind === 'problem') {
          set({ phase: 'failed', problem: created.problem });
          return;
        }
        if (created.kind !== 'accepted') {
          set({ phase: 'failed', trouble: 'uncertain' });
          return;
        }
        const gate = await wait(created.data.operation_id, signal);
        if (gate.state !== 'succeeded' || !gate.resourceId) {
          let code: string | null = null;
          if (gate.resourceId) {
            const object = await gateway.getLibraryReferenceObject({
              workspaceSlug: slug,
              objectId: gate.resourceId,
            });
            if (object.kind === 'ok')
              code =
                (object.data as { failure_code?: string | null })
                  .failure_code ?? null;
          }
          set({
            phase: 'rejected',
            failureCode: code ?? 'reference_gate_rejected',
          });
          return;
        }
        set({ phase: 'adding' });
        const added = await gateway.addMarketingIdeasPhoto({
          workspaceSlug: slug,
          actionInstanceId: `ideas-photo-add:${action}`,
          projectId,
          body: { reference_object_id: gate.resourceId, role },
        });
        if (added.kind === 'created') {
          const loaded: Loaded = { detail: added.data as IdeasDetail };
          await mutate(key, loaded, { revalidate: false });
          set({ phase: 'added' });
          return;
        }
        if (added.kind === 'problem') {
          const problem = added.problem;
          if (problem.code === 'photo_upload_rejected') {
            const params = (problem as { params?: { failure_code?: string } })
              .params;
            set({
              phase: 'rejected',
              failureCode: params?.failure_code ?? 'reference_gate_rejected',
            });
            return;
          }
          set({ phase: 'failed', problem });
          return;
        }
        set({ phase: 'failed', trouble: 'uncertain' });
      } catch (error) {
        if (error instanceof Stopped || signal.aborted) return;
        const known = error as {
          problem?: ProblemDetails;
          trouble?: PhotoUploadTrouble;
        };
        set({
          phase: 'failed',
          problem: known.problem ?? null,
          trouble: known.problem ? null : (known.trouble ?? 'uncertain'),
        });
      }
    },
    [gateway, key, mutate, projectId, slug, transfer, wait]
  );

  const reset = useCallback(() => {
    live.current?.abort();
    live.current = null;
    setState(IDLE);
  }, []);

  return { state, upload, reset };
}
