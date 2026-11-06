import { useCallback, useEffect, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { newActionId } from '@/components/generation/shared/newActionId';
import { nextPollDelay } from '@/lib/products/intake-model';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import {
  planNeedsPolling,
  REFETCH_PROBLEM_CODES,
  type RunStage,
  type TestPlanDetail,
  type TestPlanReviseRequest,
} from '@/lib/test-plans/plan-model';
import { emitTestPlanEvent } from '@/lib/test-plans/telemetry';

/** Drafting and generation can outlast product intake; past this the page
 * stops polling and offers a manual resume. */
export const PLAN_POLL_GIVE_UP_MS = 10 * 60_000;

export type PlanAction = 'revise' | 'run-sample' | 'run-full';

export interface PlanActionProblem {
  action: PlanAction;
  code: string;
  status: number;
}

export interface UseTestPlanResult {
  plan: TestPlanDetail | null;
  /** The plan could not be read (404 for a foreign / unknown plan). */
  loadProblem: ProblemDetails | undefined;
  isLoading: boolean;
  /** Polling stopped after `PLAN_POLL_GIVE_UP_MS`; `resumePolling` restarts. */
  stalled: boolean;
  pending: PlanAction | null;
  actionProblem: PlanActionProblem | null;
  clearActionProblem: () => void;
  revise: (
    body: TestPlanReviseRequest,
    changes: string[]
  ) => Promise<TestPlanDetail | null>;
  runStage: (stage: RunStage) => Promise<boolean>;
  refresh: () => void;
  resumePolling: () => void;
}

function problemFrom(
  result: { kind: string; problem?: ProblemDetails; status?: number },
  fallback: string
): { code: string; status: number } {
  if (result.kind === 'problem' && result.problem) {
    return {
      code: result.problem.code || fallback,
      status: result.problem.status || result.status || 0,
    };
  }
  // `reconciliation_required`: the outcome is unknown; the refetch shows
  // whatever the server actually did.
  return { code: 'command_unconfirmed', status: 0 };
}

/**
 * One test plan (`get_test_plan`), polled with backoff (1 s growing to
 * 5 s) while it is drafting or a stage Run is in flight, plus its two
 * commands: `revise` (If-Match = the plan's revision) and `runStage`.
 * A 409/428 that means "you looked at an older plan" reloads it.
 */
export function useTestPlan(planId: string | null): UseTestPlanResult {
  const { current } = useWorkspace();
  const slug = current.slug;
  const gateway = useDomainGateway();

  const [plan, setPlan] = useState<TestPlanDetail | null>(null);
  const [loadProblem, setLoadProblem] = useState<ProblemDetails>();
  const [isLoading, setIsLoading] = useState(Boolean(planId));
  const [stalled, setStalled] = useState(false);
  const [pending, setPending] = useState<PlanAction | null>(null);
  const [actionProblem, setActionProblem] = useState<PlanActionProblem | null>(
    null
  );
  const [pollKey, setPollKey] = useState(0);
  const planRef = useRef<TestPlanDetail | null>(null);
  planRef.current = plan;
  const reportedFailureRef = useRef<string | null>(null);

  useEffect(() => {
    if (!planId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let delay: number | null = null;
    const startedAt = Date.now();
    setStalled(false);

    const tick = async () => {
      const result = await gateway.getTestPlan({ workspaceSlug: slug, planId });
      if (cancelled) return;
      setIsLoading(false);
      if (result.kind === 'ok') {
        const next = result.data as TestPlanDetail;
        setPlan(next);
        setLoadProblem(undefined);
        if (
          next.status === 'failed' &&
          reportedFailureRef.current !== next.plan_id
        ) {
          reportedFailureRef.current = next.plan_id;
          emitTestPlanEvent({
            event: 'test_plan_failed',
            step: 'drafting',
            failureCode: next.failure?.code,
          });
        }
        if (!planNeedsPolling(next)) return;
      } else {
        setLoadProblem(result.problem);
        // Unknown or foreign plan: nothing to wait for.
        if (result.status === 404 || result.status === 403) return;
      }
      if (Date.now() - startedAt > PLAN_POLL_GIVE_UP_MS) {
        setStalled(true);
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
  }, [gateway, planId, pollKey, slug]);

  const refresh = useCallback(() => setPollKey((key) => key + 1), []);

  const revise = useCallback(
    async (
      body: TestPlanReviseRequest,
      changes: string[]
    ): Promise<TestPlanDetail | null> => {
      const base = planRef.current;
      if (!planId || !base) return null;
      setPending('revise');
      setActionProblem(null);
      try {
        const result = await gateway.reviseTestPlan({
          workspaceSlug: slug,
          actionInstanceId: newActionId(),
          planId,
          body,
          ifMatch: base.revision,
        });
        if (result.kind === 'ok') {
          const next = result.data as TestPlanDetail;
          setPlan(next);
          emitTestPlanEvent({ event: 'test_plan_revised', changes });
          if (planNeedsPolling(next)) refresh();
          return next;
        }
        const problem = problemFrom(result as any, 'revise_failed');
        setActionProblem({ action: 'revise', ...problem });
        emitTestPlanEvent({
          event: 'test_plan_failed',
          step: 'revise',
          failureCode: problem.code,
        });
        if (
          REFETCH_PROBLEM_CODES.has(problem.code) ||
          problem.status === 409 ||
          problem.status === 428 ||
          problem.code === 'command_unconfirmed'
        ) {
          refresh();
        }
        return null;
      } finally {
        setPending(null);
      }
    },
    [gateway, planId, refresh, slug]
  );

  const runStage = useCallback(
    async (stage: RunStage): Promise<boolean> => {
      if (!planId) return false;
      const action: PlanAction = stage === 'sample' ? 'run-sample' : 'run-full';
      setPending(action);
      setActionProblem(null);
      try {
        const result = await gateway.createTestPlanRun({
          workspaceSlug: slug,
          actionInstanceId: newActionId(),
          planId,
          body: { stage },
        });
        if (result.kind === 'accepted') {
          emitTestPlanEvent({
            event: 'test_plan_run_started',
            stage,
            executionMode: planRef.current?.defaults?.execution_mode,
            replayed: Boolean((result.data as { replayed?: boolean }).replayed),
          });
          refresh();
          return true;
        }
        const problem = problemFrom(result as any, 'run_failed');
        setActionProblem({ action, ...problem });
        emitTestPlanEvent({
          event: 'test_plan_failed',
          step: 'run',
          stage,
          failureCode: problem.code,
        });
        refresh();
        return false;
      } finally {
        setPending(null);
      }
    },
    [gateway, planId, refresh, slug]
  );

  return {
    plan,
    loadProblem,
    isLoading,
    stalled,
    pending,
    actionProblem,
    clearActionProblem: () => setActionProblem(null),
    revise,
    runStage,
    refresh,
    resumePolling: refresh,
  };
}
