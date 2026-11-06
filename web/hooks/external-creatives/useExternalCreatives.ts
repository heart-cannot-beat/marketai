import { useCallback, useEffect, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { DomainGateway } from '@/lib/domain-client/gateway';
import { useUpload } from '@/hooks/domain/useUpload';
import { newActionId } from '@/components/generation/shared/newActionId';
import {
  isSettled,
  type ExternalConfirmRequest,
  type ExternalCreative,
  type MadeBy,
} from '@/lib/external-creatives/model';
import type { DomainProblem } from '@/lib/i18n/domain-errors';

export type ExternalFlowPhase = 'idle' | 'uploading' | 'submitting' | 'review';

export interface ExternalProblem {
  stage: 'upload' | 'create' | 'confirm' | 'load';
  code: string;
  /** The domain refusal, rendered by its code (#253). */
  problem: DomainProblem | null;
  /** The creative a confirm problem belongs to. */
  creativeId?: string;
}

export interface SubmitInput {
  productId: string;
  files: File[];
  madeBy: MadeBy;
  permissionConfirmed: boolean;
  note?: string;
}

export interface UseExternalCreativesResult {
  phase: ExternalFlowPhase;
  creatives: ExternalCreative[];
  uploadProgress: { done: number; total: number } | null;
  problem: ExternalProblem | null;
  confirming: string | null;
  submit: (input: SubmitInput) => Promise<void>;
  /** Shows the creatives (of one product, or all) still waiting for a confirmation. */
  openPending: (productId?: string) => Promise<void>;
  confirm: (
    creativeId: string,
    body: ExternalConfirmRequest
  ) => Promise<ExternalCreative | null>;
  reset: () => void;
}

const POLL_MS = 1500;
const OPERATION_POLL_MS = 1000;
const OPERATION_POLL_LIMIT = 120;
const TERMINAL_OPERATION_STATES = new Set([
  'succeeded',
  'failed',
  'cancelled',
  'partially_succeeded',
]);

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForOperation(
  gateway: DomainGateway,
  workspaceSlug: string,
  operationId: string,
  isCancelled: () => boolean
): Promise<string> {
  for (let attempt = 0; attempt < OPERATION_POLL_LIMIT; attempt++) {
    if (isCancelled()) return 'cancelled';
    const result = await gateway.pollOperation({ workspaceSlug, operationId });
    if (result.kind === 'ok') {
      const state = String((result.data as { state?: string }).state ?? '');
      if (TERMINAL_OPERATION_STATES.has(state)) return state;
    }
    await sleep(OPERATION_POLL_MS);
  }
  return 'timeout';
}

function problemOf(
  stage: ExternalProblem['stage'],
  result: { kind: string; problem?: DomainProblem },
  fallback: string
): ExternalProblem {
  if (result.kind === 'problem' && result.problem) {
    return {
      stage,
      code: result.problem.code || fallback,
      problem: result.problem,
    };
  }
  return { stage, code: fallback, problem: null };
}

/**
 * "上传外部素材" (Issue #168): uploads each dropped image through the private
 * upload flow (purpose `external_creative`), admits the batch, then polls
 * every creative until the gate, storage and AI tagging settle, and
 * confirms one card at a time. The server stays the only source of truth;
 * this hook only orders the calls and keeps the latest views.
 */
export function useExternalCreatives(): UseExternalCreativesResult {
  const { current } = useWorkspace();
  const slug = current.slug;
  const gateway = useDomainGateway();
  const { startUpload } = useUpload('external_creative');

  const [phase, setPhase] = useState<ExternalFlowPhase>('idle');
  const [creatives, setCreatives] = useState<ExternalCreative[]>([]);
  const [uploadProgress, setUploadProgress] = useState<{
    done: number;
    total: number;
  } | null>(null);
  const [problem, setProblem] = useState<ExternalProblem | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const sessionRef = useRef(0);

  useEffect(
    () => () => {
      sessionRef.current += 1;
    },
    []
  );

  const replace = useCallback((next: ExternalCreative) => {
    setCreatives((items) =>
      items.map((item) =>
        item.external_creative_id === next.external_creative_id ? next : item
      )
    );
  }, []);

  // Poll the creatives the Worker is still processing.
  const pending = creatives
    .filter((item) => !isSettled(item.status))
    .map((item) => item.external_creative_id)
    .join(',');
  useEffect(() => {
    if (phase !== 'review' || !pending) return;
    let cancelled = false;
    const timer = setTimeout(async () => {
      for (const id of pending.split(',')) {
        const result = await gateway.getExternalCreative({
          workspaceSlug: slug,
          externalCreativeId: id,
        });
        if (cancelled) return;
        if (result.kind === 'ok') replace(result.data as ExternalCreative);
      }
    }, POLL_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [gateway, pending, phase, replace, slug, creatives]);

  const submit = useCallback(
    async (input: SubmitInput) => {
      const session = ++sessionRef.current;
      const isCancelled = () => session !== sessionRef.current;
      setProblem(null);
      setPhase('uploading');
      setUploadProgress({ done: 0, total: input.files.length });
      const uploadIntentIds: string[] = [];
      const ownerId = newActionId();
      for (const file of input.files) {
        const uploaded = await startUpload({
          file,
          ownerKind: 'external_creative',
          ownerId,
          actionInstanceId: newActionId(),
        });
        if (isCancelled()) return;
        if (uploaded.kind !== 'accepted') {
          setProblem(
            uploaded.kind === 'problem'
              ? {
                  stage: 'upload',
                  code: uploaded.problem.code || 'upload_failed',
                  problem: uploaded.problem,
                }
              : {
                  stage: 'upload',
                  code:
                    uploaded.kind === 'transfer_error'
                      ? 'transfer_error'
                      : 'upload_unconfirmed',
                  problem: null,
                }
          );
          setPhase('idle');
          setUploadProgress(null);
          return;
        }
        const state = await waitForOperation(
          gateway,
          slug,
          uploaded.operationRecordId,
          isCancelled
        );
        if (isCancelled()) return;
        if (state !== 'succeeded') {
          setProblem({
            stage: 'upload',
            code: 'upload_rejected',
            problem: null,
          });
          setPhase('idle');
          setUploadProgress(null);
          return;
        }
        uploadIntentIds.push(uploaded.uploadIntentId);
        setUploadProgress({
          done: uploadIntentIds.length,
          total: input.files.length,
        });
      }

      setPhase('submitting');
      const created = await gateway.createExternalCreatives({
        workspaceSlug: slug,
        actionInstanceId: newActionId(),
        body: {
          product_id: input.productId,
          upload_intent_ids: uploadIntentIds,
          rights: {
            made_by: input.madeBy,
            permission_confirmed: input.permissionConfirmed,
            note: input.note?.trim() || null,
          },
        },
      });
      if (isCancelled()) return;
      setUploadProgress(null);
      if (created.kind === 'accepted') {
        setCreatives(
          ((created.data as { items?: ExternalCreative[] }).items ??
            []) as ExternalCreative[]
        );
        setPhase('review');
        return;
      }
      setProblem(problemOf('create', created, 'create_failed'));
      setPhase('idle');
    },
    [gateway, slug, startUpload]
  );

  const openPending = useCallback(
    async (productId?: string) => {
      const session = ++sessionRef.current;
      setProblem(null);
      const results = await Promise.all(
        ['ready', 'tagging_failed'].map((status) =>
          gateway.listExternalCreatives({
            workspaceSlug: slug,
            productId,
            status,
          })
        )
      );
      if (session !== sessionRef.current) return;
      const failed = results.find((result) => result.kind !== 'ok');
      if (failed) {
        setProblem(problemOf('load', failed, 'load_failed'));
        return;
      }
      setCreatives(
        results.flatMap((result) =>
          result.kind === 'ok'
            ? (((result.data as { items?: ExternalCreative[] }).items ??
                []) as ExternalCreative[])
            : []
        )
      );
      setPhase('review');
    },
    [gateway, slug]
  );

  const confirm = useCallback(
    async (creativeId: string, body: ExternalConfirmRequest) => {
      setConfirming(creativeId);
      setProblem(null);
      const result = await gateway.confirmExternalCreative({
        workspaceSlug: slug,
        actionInstanceId: newActionId(),
        externalCreativeId: creativeId,
        body,
      });
      setConfirming(null);
      if (result.kind === 'ok') {
        const next = result.data as ExternalCreative;
        replace(next);
        return next;
      }
      setProblem({
        ...problemOf('confirm', result, 'confirm_failed'),
        creativeId,
      });
      return null;
    },
    [gateway, replace, slug]
  );

  const reset = useCallback(() => {
    sessionRef.current += 1;
    setPhase('idle');
    setCreatives([]);
    setProblem(null);
    setUploadProgress(null);
    setConfirming(null);
  }, []);

  return {
    phase,
    creatives,
    uploadProgress,
    problem,
    confirming,
    submit,
    openPending,
    confirm,
    reset,
  };
}
