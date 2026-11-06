/**
 * Real-time GenerationRun progress source (Task 8, Generation Workspace UI
 * plan). A `GenerationRunDetail` (`get_generation_run`) is a *projection* —
 * `status`/`done`/`total`/`failed` are recomputed server-side from the
 * Run's RunItems (see `app/generation/run.py::derive_run_state`) — while
 * the Run's most recent command (submit/retry/resume/cancel/reconcile,
 * `GenerationRunCommandSummary`) is tracked separately through its own
 * Operation (`get_operation`, spec §5.6). A Run can look terminal while its
 * last command Operation is still `queued`/`running`/`uncertain` (or vice
 * versa), so this source polls both and only settles once *both* are
 * terminal.
 *
 * `PollingRunProgressSource` is a plain class (not a hook) so it can be
 * unit-tested without React — `useRunProgress.ts` is the hook wrapper Task
 * 9's components actually import.
 */
import type { DomainGateway } from '@/lib/domain-client/gateway';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

export type GenerationRunDetail = components['schemas']['GenerationRunDetail'];
export type GenerationRunItemDetail =
  components['schemas']['GenerationRunItemDetail'];
export type OperationDetailResponse =
  components['schemas']['OperationDetailResponse'];

/** Snapshot delivered to every `onSnapshot` callback (spec: this task's brief). */
export interface RunProgressSnapshot {
  /** `null` until the first successful `get_generation_run` response. */
  run: GenerationRunDetail | null;
  items: GenerationRunItemDetail[];
  itemsPagination: { cursor: string | null; hasMore: boolean };
  /** The Operation behind the Run's most recent command, or `null` when the
   * Run has no commands yet (nothing in flight to wait on). */
  latestCommandOperation: OperationDetailResponse | null;
  /** `run.status` has reached one of `derive_run_state`'s terminal outcomes
   * (`succeeded` / `partially_succeeded` / `cancelled` / `failed`). */
  isRunTerminal: boolean;
  /** `latestCommandOperation` is `null` or has reached a terminal
   * `OperationDetailResponse.state` (spec §5.6). */
  isOperationTerminal: boolean;
  isPartialSuccess: boolean;
  lastRefreshedAt: number;
  /** Set when the most recent poll attempt threw (network failure) — the
   * previous `run`/`items`/`latestCommandOperation` are preserved rather
   * than cleared, so a transient blip never flashes the UI back to empty. */
  transportError: Error | null;
  /** Set when the BFF returned a definitive `problem` response (e.g. the
   * Run no longer exists) — polling stops once this is set. */
  problem: ProblemDetails | null;
}

export interface RunProgressSource {
  subscribe(
    runId: string,
    options: { itemPageCursor?: string | null },
    onSnapshot: (snapshot: RunProgressSnapshot) => void
  ): void;
  unsubscribe(): void;
  /** Cancels any pending scheduled poll and fetches immediately. */
  forceRefresh(): void;
  /** Changes the RunItem page cursor and immediately re-fetches. */
  setItemPage(cursor: string | null): void;
}

/** `run.status` values `derive_run_state` treats as terminal
 * (`app/generation/run.py`) — `running`/`uncertain` are not. */
const RUN_TERMINAL_STATES = new Set<string>([
  'succeeded',
  'failed',
  'cancelled',
  'partially_succeeded',
]);

/** `OperationDetailResponse['state']` values that stop polling (spec §5.6,
 * mirrors `TERMINAL_STATES` in `hooks/domain/useOperation.ts`). */
const OPERATION_TERMINAL_STATES = new Set<OperationDetailResponse['state']>([
  'succeeded',
  'failed',
  'cancelled',
  'partially_succeeded',
]);

/**
 * Adaptive poll cadence: fast (1-2s, jittered) while either the Run or its
 * latest command Operation is actively `running`; slow-and-jittered
 * (15-30s) while either is `queued`/`uncertain` (avoids a synchronized
 * thundering herd across many open tabs); a moderate 5s default otherwise
 * (e.g. a Run with no in-flight command yet still has active RunItems).
 */
function getPollIntervalMs(
  runStatus: string | undefined,
  operationState: OperationDetailResponse['state'] | null
): number {
  if (operationState === 'running' || runStatus === 'running') {
    return 1000 + Math.random() * 1000;
  }
  if (
    operationState === 'admitted' ||
    operationState === 'queued' ||
    operationState === 'uncertain' ||
    runStatus === 'uncertain'
  ) {
    return 15_000 + Math.random() * 15_000;
  }
  return 5000;
}

export class PollingRunProgressSource implements RunProgressSource {
  private readonly gateway: DomainGateway;
  private readonly workspaceSlug: string;

  private runId: string | null = null;
  private itemPageCursor: string | null = null;
  private onSnapshot: ((snapshot: RunProgressSnapshot) => void) | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = true;
  private consecutiveFailures = 0;
  private visibilityHandler: (() => void) | null = null;

  // Last known-good state, reused across `not_modified` (304) responses and
  // transport-error snapshots so the UI never flashes back to empty.
  private lastRun: GenerationRunDetail | null = null;
  private lastItems: GenerationRunItemDetail[] = [];
  private lastItemsPagination: { cursor: string | null; hasMore: boolean } = {
    cursor: null,
    hasMore: false,
  };
  private lastOperation: OperationDetailResponse | null = null;
  private runEtag: string | null = null;
  private operationEtag: string | null = null;

  constructor(gateway: DomainGateway, workspaceSlug: string) {
    this.gateway = gateway;
    this.workspaceSlug = workspaceSlug;
  }

  subscribe(
    runId: string,
    options: { itemPageCursor?: string | null },
    onSnapshot: (snapshot: RunProgressSnapshot) => void
  ): void {
    this.runId = runId;
    this.itemPageCursor = options.itemPageCursor ?? null;
    this.onSnapshot = onSnapshot;
    this.stopped = false;
    this.consecutiveFailures = 0;
    this.lastRun = null;
    this.lastItems = [];
    this.lastItemsPagination = { cursor: null, hasMore: false };
    this.lastOperation = null;
    this.runEtag = null;
    this.operationEtag = null;

    // A backgrounded tab should not keep polling on the fast cadence; a
    // refresh on return catches up immediately rather than waiting out
    // whatever interval was scheduled before the tab lost focus.
    if (typeof document !== 'undefined') {
      this.visibilityHandler = () => {
        if (document.visibilityState === 'visible') {
          this.forceRefresh();
        }
      };
      document.addEventListener('visibilitychange', this.visibilityHandler);
    }

    void this.poll();
  }

  unsubscribe(): void {
    this.stopped = true;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.visibilityHandler && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', this.visibilityHandler);
      this.visibilityHandler = null;
    }
    this.onSnapshot = null;
  }

  forceRefresh(): void {
    if (this.stopped) return;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    void this.poll();
  }

  setItemPage(cursor: string | null): void {
    this.itemPageCursor = cursor;
    this.forceRefresh();
  }

  private emit(snapshot: RunProgressSnapshot): void {
    if (this.stopped || !this.onSnapshot) return;
    this.onSnapshot(snapshot);
  }

  private async poll(): Promise<void> {
    if (this.stopped || !this.runId) return;
    const runId = this.runId;

    try {
      const runResult = await this.gateway.getGenerationRun({
        workspaceSlug: this.workspaceSlug,
        runId,
        ifNoneMatch: this.runEtag ?? undefined,
      });

      if (this.stopped) return;

      if (runResult.kind === 'problem') {
        this.consecutiveFailures = 0;
        this.emit({
          run: this.lastRun,
          items: this.lastItems,
          itemsPagination: this.lastItemsPagination,
          latestCommandOperation: this.lastOperation,
          isRunTerminal: this.lastRun
            ? RUN_TERMINAL_STATES.has(this.lastRun.status)
            : false,
          isOperationTerminal: this.lastOperation
            ? OPERATION_TERMINAL_STATES.has(this.lastOperation.state)
            : true,
          isPartialSuccess: this.lastRun?.status === 'partially_succeeded',
          lastRefreshedAt: Date.now(),
          transportError: null,
          problem: runResult.problem,
        });
        // A definitive problem (e.g. run not found, access revoked) is
        // terminal for this poll loop — no further scheduling.
        return;
      }

      if (runResult.kind === 'ok') {
        this.lastRun = runResult.data;
        this.runEtag = runResult.meta.etag ?? this.runEtag;
      }
      // `not_modified` (304): keep the cached `lastRun`/`runEtag` as-is.

      const run = this.lastRun;
      if (!run) {
        // Should not happen (a fresh subscribe always awaits an `ok` before
        // reaching here), but keeps this function total under `strict`.
        this.timer = setTimeout(() => this.poll(), 5000);
        return;
      }

      const latestCommand =
        run.commands && run.commands.length > 0
          ? run.commands[run.commands.length - 1]
          : null;

      if (latestCommand) {
        const opResult = await this.gateway.pollOperation({
          workspaceSlug: this.workspaceSlug,
          operationId: latestCommand.operation_id,
          ifNoneMatch: this.operationEtag ?? undefined,
        });
        if (this.stopped) return;
        if (opResult.kind === 'ok') {
          this.lastOperation = opResult.data;
          this.operationEtag = opResult.meta.etag ?? this.operationEtag;
        } else if (opResult.kind === 'problem') {
          // Degrade gracefully — the Run itself is still authoritative;
          // losing track of the command Operation shouldn't stop Run
          // polling. Treated as "unknown" (non-terminal) below via the
          // cached `lastOperation` (or `null` if never observed).
        }
      } else {
        this.lastOperation = null;
        this.operationEtag = null;
      }

      const itemsResult = await this.gateway.listGenerationRunItems({
        workspaceSlug: this.workspaceSlug,
        runId,
        cursor: this.itemPageCursor ?? undefined,
      });
      if (this.stopped) return;

      if (itemsResult.kind === 'ok') {
        const page = itemsResult.data as {
          items: unknown[];
          next_cursor?: string | null;
        };
        this.lastItems = (page.items ?? []) as GenerationRunItemDetail[];
        this.lastItemsPagination = {
          cursor: page.next_cursor ?? null,
          hasMore: Boolean(page.next_cursor),
        };
      }
      // `problem`/anything else: keep the last known items page rather than
      // clearing it — pagination reads degrade the same way run/operation do.

      this.consecutiveFailures = 0;

      const isRunTerminal = RUN_TERMINAL_STATES.has(run.status);
      const isOperationTerminal = this.lastOperation
        ? OPERATION_TERMINAL_STATES.has(this.lastOperation.state)
        : true;

      const snapshot: RunProgressSnapshot = {
        run,
        items: this.lastItems,
        itemsPagination: this.lastItemsPagination,
        latestCommandOperation: this.lastOperation,
        isRunTerminal,
        isOperationTerminal,
        isPartialSuccess: run.status === 'partially_succeeded',
        lastRefreshedAt: Date.now(),
        transportError: null,
        problem: null,
      };

      this.emit(snapshot);

      if (isRunTerminal && isOperationTerminal) {
        return;
      }

      const delay = getPollIntervalMs(
        run.status,
        this.lastOperation?.state ?? null
      );
      this.timer = setTimeout(() => this.poll(), delay);
    } catch (err) {
      if (this.stopped) return;
      this.consecutiveFailures += 1;
      const backoff = Math.min(60_000, 1000 * 2 ** this.consecutiveFailures);

      this.emit({
        run: this.lastRun,
        items: this.lastItems,
        itemsPagination: this.lastItemsPagination,
        latestCommandOperation: this.lastOperation,
        isRunTerminal: this.lastRun
          ? RUN_TERMINAL_STATES.has(this.lastRun.status)
          : false,
        isOperationTerminal: this.lastOperation
          ? OPERATION_TERMINAL_STATES.has(this.lastOperation.state)
          : true,
        isPartialSuccess: this.lastRun?.status === 'partially_succeeded',
        lastRefreshedAt: Date.now(),
        transportError: err instanceof Error ? err : new Error(String(err)),
        problem: null,
      });

      this.timer = setTimeout(() => this.poll(), backoff);
    }
  }
}
