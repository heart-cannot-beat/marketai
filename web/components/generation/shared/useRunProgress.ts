/**
 * React hook wrapper around `PollingRunProgressSource` (Task 8, Generation
 * Workspace UI plan). Consumed by `RunProgressView` (Task 9) to drive
 * real-time Run progress — mirrors `hooks/domain/useOperation.ts`'s
 * gateway/workspace wiring so both hooks look the same to a caller.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import {
  PollingRunProgressSource,
  type RunProgressSnapshot,
} from './RunProgressSource';

const INITIAL_SNAPSHOT: RunProgressSnapshot = {
  run: null,
  items: [],
  itemsPagination: { cursor: null, hasMore: false },
  latestCommandOperation: null,
  isRunTerminal: false,
  isOperationTerminal: false,
  isPartialSuccess: false,
  lastRefreshedAt: 0,
  transportError: null,
  problem: null,
};

export interface UseRunProgressResult extends RunProgressSnapshot {
  /** Cancels any pending scheduled poll and re-fetches immediately. */
  forceRefresh: () => void;
  /** Changes the RunItem page cursor and immediately re-fetches. */
  setItemPage: (cursor: string | null) => void;
}

/**
 * Polls a GenerationRun (and its most recent command's Operation, and its
 * current RunItems page) by `runId` until both the Run and that Operation
 * reach a terminal state (spec: `app/generation/run.py`, `get_operation`
 * spec §5.6). `runId` may be `null` while the caller hasn't learned it yet
 * (e.g. still resolving from the route) — polling is disabled until then,
 * matching `useOperation`'s `operationRecordId` contract.
 */
export function useRunProgress(runId: string | null): UseRunProgressResult {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const workspaceSlug = current.slug;

  const [snapshot, setSnapshot] =
    useState<RunProgressSnapshot>(INITIAL_SNAPSHOT);
  const sourceRef = useRef<PollingRunProgressSource | null>(null);

  useEffect(() => {
    setSnapshot(INITIAL_SNAPSHOT);

    if (!runId) {
      sourceRef.current = null;
      return;
    }

    const source = new PollingRunProgressSource(gateway, workspaceSlug);
    sourceRef.current = source;
    source.subscribe(runId, {}, setSnapshot);

    return () => {
      source.unsubscribe();
      if (sourceRef.current === source) {
        sourceRef.current = null;
      }
    };
  }, [runId, gateway, workspaceSlug]);

  const forceRefresh = useCallback(() => {
    sourceRef.current?.forceRefresh();
  }, []);

  const setItemPage = useCallback((cursor: string | null) => {
    sourceRef.current?.setItemPage(cursor);
  }, []);

  return useMemo(
    () => ({ ...snapshot, forceRefresh, setItemPage }),
    [snapshot, forceRefresh, setItemPage]
  );
}
