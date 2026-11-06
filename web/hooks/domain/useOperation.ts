import { useCallback, useEffect, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { DomainOperationMap } from '@/lib/domain-client/generated/operation-map';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

/** The `200` success payload of `get_operation` (`OperationDetailResponse`,
 * spec §5.6) — never the `304` variant, which carries no body. */
export type OperationData =
  DomainOperationMap['get_operation']['successResponses'][200];

/** `OperationDetailResponse['state']` values that stop polling (spec §5.6).
 * `partially_succeeded` is terminal but still surfaces via
 * `isPartialSuccess` so a caller can distinguish it from a clean
 * `succeeded`. */
const TERMINAL_STATES = new Set<OperationData['state']>([
  'succeeded',
  'failed',
  'cancelled',
  'partially_succeeded',
]);

/** Client-side poll backoff used when the server sends no `Retry-After`
 * (`meta.retryAt`) — grows with each consecutive poll, capped at the last
 * entry. */
const DEFAULT_INTERVALS_MS = [1000, 2000, 3000, 5000, 10000, 15000];

interface UseOperationParams {
  /** `null` while the caller hasn't started (or hasn't yet learned) the
   * Operation to track — polling is disabled until this is set. */
  operationRecordId: string | null;
  /** Describes what this Operation is for (e.g. `"upload"`); not sent to the
   * server, only used to keep call sites self-documenting. */
  purpose: string;
  /** Stable identity of the logical user action that started this Operation
   * (spec §6.10) — keys the sessionStorage recovery entry alongside the
   * Workspace slug, so a component that lost track of `operationRecordId`
   * (e.g. after a full page reload) can look it back up. */
  actionInstanceId: string;
}

interface UseOperationResult {
  operation: OperationData | undefined;
  problem: ProblemDetails | undefined;
  isTerminal: boolean;
  isPartialSuccess: boolean;
}

/**
 * Polls an Operation by `operationRecordId` via `gateway.pollOperation()`
 * until it reaches a terminal state (spec §5.6, §7.8). Sends the previous
 * response's `ETag` back as `If-None-Match` so an unchanged Operation
 * returns a bodyless `304` — the hook keeps its last known `operation` in
 * that case. Uses the server's `Retry-After` (`meta.retryAt`) as the next
 * poll delay when present, otherwise a growing client-side backoff.
 */
export function useOperation({
  operationRecordId,
  actionInstanceId,
}: UseOperationParams): UseOperationResult {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();

  const [operation, setOperation] = useState<OperationData>();
  const [problem, setProblem] = useState<ProblemDetails>();

  const etagRef = useRef<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout>>();
  const pollCountRef = useRef(0);
  const mountedRef = useRef(true);

  const storageKey = `operation:${current.slug}:${actionInstanceId}`;

  // Records which Operation is in flight for this action so a component
  // that remounts without an `operationRecordId` in hand (e.g. after a page
  // reload) can look this up and resume tracking it.
  useEffect(() => {
    if (!operationRecordId) return;
    try {
      sessionStorage.setItem(storageKey, operationRecordId);
    } catch {
      // Storage unavailable (private browsing, quota) — recovery degrades,
      // polling itself is unaffected.
    }
  }, [storageKey, operationRecordId]);

  const scheduleNext = useCallback(
    (retryAtSeconds: number | null | undefined, poll: () => void) => {
      if (!mountedRef.current) return;
      const delayMs =
        retryAtSeconds != null
          ? retryAtSeconds * 1000
          : DEFAULT_INTERVALS_MS[
              Math.min(pollCountRef.current, DEFAULT_INTERVALS_MS.length - 1)
            ];
      pollCountRef.current += 1;
      timerRef.current = setTimeout(poll, delayMs);
    },
    []
  );

  const poll = useCallback(async () => {
    if (!operationRecordId || !mountedRef.current) return;

    try {
      const result = await gateway.pollOperation({
        workspaceSlug: current.slug,
        operationId: operationRecordId,
        ifNoneMatch: etagRef.current ?? undefined,
      });

      if (!mountedRef.current) return;

      if (result.kind === 'not_modified') {
        // No body — the previously stored `operation` (and its ETag) stay
        // as-is; only the poll cadence advances.
        scheduleNext(result.meta.retryAt, poll);
        return;
      }

      if (result.kind === 'problem') {
        setProblem(result.problem);
        // A problem response (e.g. the Operation no longer exists) is
        // terminal for this poll loop — no further scheduling.
        return;
      }

      setOperation(result.data);
      etagRef.current = result.meta.etag;

      if (!TERMINAL_STATES.has(result.data.state)) {
        scheduleNext(result.meta.retryAt, poll);
      }
    } catch {
      if (mountedRef.current) {
        scheduleNext(null, poll);
      }
    }
  }, [operationRecordId, gateway, current.slug, scheduleNext]);

  useEffect(() => {
    mountedRef.current = true;
    // Fresh poll loop — reset accumulated poll state so a new
    // `operationRecordId` (or a `poll` identity change from `current.slug`
    // changing under a Workspace switch, since `poll` closes over it) never
    // inherits a stale ETag, backoff count, previous problem/data, or a
    // scheduled retry that would otherwise keep re-invoking the OLD `poll`
    // closure — and with it the previous Workspace's slug — indefinitely.
    etagRef.current = null;
    pollCountRef.current = 0;
    setOperation(undefined);
    setProblem(undefined);

    if (operationRecordId) {
      poll();
    }

    return () => {
      mountedRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [operationRecordId, poll]);

  const isTerminal = operation ? TERMINAL_STATES.has(operation.state) : false;
  const isPartialSuccess = operation?.state === 'partially_succeeded';

  return { operation, problem, isTerminal, isPartialSuccess };
}
