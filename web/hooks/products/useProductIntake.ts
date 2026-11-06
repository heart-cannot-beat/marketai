import { useCallback, useEffect, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { DomainGateway } from '@/lib/domain-client/gateway';
import { useUpload } from '@/hooks/domain/useUpload';
import { newActionId } from '@/components/generation/shared/newActionId';
import {
  buildIntakeRequest,
  intakePhase,
  nextPollDelay,
  POLL_GIVE_UP_MS,
  splitPaste,
  suggestedAction,
  type ConfirmRequest,
  type IntakeDetail,
} from '@/lib/products/intake-model';
import {
  emitProductIntakeEvent,
  type IntakeInputKind,
} from '@/lib/products/telemetry';
import { recallIntake, rememberIntake } from '@/lib/products/handoff';
import type { DomainProblem } from '@/lib/i18n/domain-errors';

export type IntakeFlowPhase =
  | 'input'
  | 'uploading'
  | 'submitting'
  | 'working'
  | 'stalled'
  | 'ready'
  | 'failed'
  | 'confirming'
  | 'confirmed';

/** A refusal the page explains by its stable `code` (and the domain
 * problem's `params`, #253); the server's own text is never shown. */
export interface FlowProblem {
  stage: 'upload' | 'intake' | 'confirm';
  code: string;
  problem: DomainProblem | null;
}

export interface PasteInput {
  raw: string;
  files: File[];
}

export interface UseProductIntakeResult {
  phase: IntakeFlowPhase;
  detail: IntakeDetail | null;
  uploadProgress: { done: number; total: number } | null;
  problem: FlowProblem | null;
  /** The last submitted paste, so a failure can be supplemented in place. */
  lastInput: PasteInput | null;
  submit: (input: PasteInput) => Promise<void>;
  confirm: (body: ConfirmRequest) => Promise<IntakeDetail | null>;
  /** Back to an empty paste box; the server-side intake is left as is. */
  startOver: () => void;
  /** Poll again after the page stopped waiting. */
  resumePolling: () => void;
}

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

function inputKinds(
  body: { url?: string | null; text?: string | null },
  images: number
) {
  const kinds: IntakeInputKind[] = [];
  if (body.url) kinds.push('url');
  if (images > 0) kinds.push('images');
  if (body.text) kinds.push('text');
  return kinds;
}

/**
 * The "设置产品" flow behind one paste box (Issue #162): uploads dropped
 * images (`product_intake_image`), starts the intake, polls it with backoff
 * (1 s growing to 5 s, giving up after 5 min with a manual resume), and
 * confirms the edited card. The intake id is remembered per tab so a
 * refresh resumes the same card; the server stays the source of truth.
 */
export function useProductIntake(): UseProductIntakeResult {
  const { current } = useWorkspace();
  const slug = current.slug;
  const gateway = useDomainGateway();
  const { startUpload } = useUpload('product_intake_image');

  const [phase, setPhase] = useState<IntakeFlowPhase>('input');
  const [intakeId, setIntakeId] = useState<string | null>(null);
  const [detail, setDetail] = useState<IntakeDetail | null>(null);
  const [problem, setProblem] = useState<FlowProblem | null>(null);
  const [uploadProgress, setUploadProgress] = useState<{
    done: number;
    total: number;
  } | null>(null);
  const [lastInput, setLastInput] = useState<PasteInput | null>(null);
  const [pollGeneration, setPollGeneration] = useState(0);

  const sessionRef = useRef(0);
  const startedAtRef = useRef<number | null>(null);
  const reportedFailureRef = useRef<string | null>(null);

  // Resume the tab's in-flight intake after a refresh.
  useEffect(() => {
    const remembered = recallIntake(slug);
    if (remembered) {
      setIntakeId(remembered);
      setPhase('working');
    }
    return () => {
      sessionRef.current += 1;
    };
  }, [slug]);

  // Poll the intake until it is ready, confirmed or failed.
  useEffect(() => {
    if (!intakeId || phase !== 'working') return;
    const session = sessionRef.current;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let delay: number | null = null;
    const startedAt = Date.now();

    const tick = async () => {
      const result = await gateway.getProductIntake({
        workspaceSlug: slug,
        intakeId,
      });
      if (cancelled || session !== sessionRef.current) return;
      if (result.kind === 'ok') {
        const next = result.data as IntakeDetail;
        setDetail(next);
        const nextPhase = intakePhase(next.status);
        if (nextPhase === 'ready') {
          setPhase('ready');
          return;
        }
        if (nextPhase === 'confirmed') {
          setPhase('confirmed');
          rememberIntake(slug, null);
          return;
        }
        if (nextPhase === 'failed') {
          setPhase('failed');
          rememberIntake(slug, null);
          if (reportedFailureRef.current !== next.intake_id) {
            reportedFailureRef.current = next.intake_id;
            emitProductIntakeEvent({
              event: 'product_intake_failed',
              stage: 'intake',
              failureCode: next.failure?.code,
              suggestedAction: suggestedAction(next.failure),
              executionMode: next.execution_mode,
            });
          }
          return;
        }
      } else if (result.status === 404) {
        // The remembered intake is gone (another Workspace, or expired).
        rememberIntake(slug, null);
        setIntakeId(null);
        setDetail(null);
        setPhase('input');
        return;
      }
      if (Date.now() - startedAt > POLL_GIVE_UP_MS) {
        setPhase('stalled');
        return;
      }
      delay = nextPollDelay(delay);
      timer = setTimeout(tick, delay);
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [gateway, intakeId, phase, pollGeneration, slug]);

  const submit = useCallback(
    async (input: PasteInput) => {
      const session = ++sessionRef.current;
      const isCancelled = () => session !== sessionRef.current;
      setLastInput(input);
      setProblem(null);
      setDetail(null);
      setIntakeId(null);
      reportedFailureRef.current = null;
      startedAtRef.current = Date.now();

      const parts = splitPaste(input.raw);
      const uploadIntentIds: string[] = [];
      if (input.files.length > 0) {
        setPhase('uploading');
        setUploadProgress({ done: 0, total: input.files.length });
        const ownerId = newActionId();
        for (const file of input.files) {
          const uploaded = await startUpload({
            file,
            ownerKind: 'product_intake',
            ownerId,
            actionInstanceId: newActionId(),
          });
          if (isCancelled()) return;
          if (uploaded.kind !== 'accepted') {
            const code =
              uploaded.kind === 'problem'
                ? uploaded.problem.code || 'upload_failed'
                : uploaded.kind === 'transfer_error'
                  ? 'transfer_error'
                  : 'upload_unconfirmed';
            setProblem({
              stage: 'upload',
              code,
              problem: uploaded.kind === 'problem' ? uploaded.problem : null,
            });
            setPhase('input');
            setUploadProgress(null);
            emitProductIntakeEvent({
              event: 'product_intake_failed',
              stage: 'upload',
              failureCode: code,
            });
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
            setPhase('input');
            setUploadProgress(null);
            emitProductIntakeEvent({
              event: 'product_intake_failed',
              stage: 'upload',
              failureCode: 'upload_rejected',
            });
            return;
          }
          uploadIntentIds.push(uploaded.uploadIntentId);
          setUploadProgress({
            done: uploadIntentIds.length,
            total: input.files.length,
          });
        }
      }

      const body = buildIntakeRequest(parts, uploadIntentIds);
      if (!body) {
        setProblem({ stage: 'intake', code: 'input_required', problem: null });
        setPhase('input');
        return;
      }

      setPhase('submitting');
      const created = await gateway.createProductIntake({
        workspaceSlug: slug,
        actionInstanceId: newActionId(),
        body,
      });
      if (isCancelled()) return;
      setUploadProgress(null);
      if (created.kind === 'accepted') {
        emitProductIntakeEvent({
          event: 'product_intake_started',
          inputKinds: inputKinds(body, uploadIntentIds.length),
          replayed: created.data.replayed,
        });
        rememberIntake(slug, created.data.intake_id);
        setIntakeId(created.data.intake_id);
        setPhase('working');
        return;
      }
      const code =
        created.kind === 'problem'
          ? created.problem.code || 'intake_failed'
          : 'intake_unconfirmed';
      setProblem({
        stage: 'intake',
        code,
        problem: created.kind === 'problem' ? created.problem : null,
      });
      setPhase('input');
      emitProductIntakeEvent({
        event: 'product_intake_failed',
        stage: 'intake',
        failureCode: code,
      });
    },
    [gateway, slug, startUpload]
  );

  const confirm = useCallback(
    async (body: ConfirmRequest): Promise<IntakeDetail | null> => {
      if (!intakeId) return null;
      setPhase('confirming');
      setProblem(null);
      const result = await gateway.confirmProductIntake({
        workspaceSlug: slug,
        actionInstanceId: newActionId(),
        intakeId,
        body,
      });
      if (result.kind === 'ok') {
        const confirmed = result.data as IntakeDetail;
        setDetail(confirmed);
        setPhase('confirmed');
        rememberIntake(slug, null);
        emitProductIntakeEvent({
          event: 'product_intake_confirmed',
          executionMode: confirmed.execution_mode,
          durationMs:
            startedAtRef.current !== null
              ? Date.now() - startedAtRef.current
              : undefined,
        });
        return confirmed;
      }
      const code =
        result.kind === 'problem'
          ? result.problem.code || 'confirm_failed'
          : 'confirm_unconfirmed';
      setProblem({
        stage: 'confirm',
        code,
        problem: result.kind === 'problem' ? result.problem : null,
      });
      setPhase('ready');
      emitProductIntakeEvent({
        event: 'product_intake_failed',
        stage: 'confirm',
        failureCode: code,
      });
      return null;
    },
    [gateway, intakeId, slug]
  );

  const startOver = useCallback(() => {
    sessionRef.current += 1;
    rememberIntake(slug, null);
    setIntakeId(null);
    setDetail(null);
    setProblem(null);
    setUploadProgress(null);
    setPhase('input');
  }, [slug]);

  const resumePolling = useCallback(() => {
    if (!intakeId) return;
    setPhase('working');
    setPollGeneration((value) => value + 1);
  }, [intakeId]);

  return {
    phase,
    detail,
    uploadProgress,
    problem,
    lastInput,
    submit,
    confirm,
    startOver,
    resumePolling,
  };
}
