import { useCallback, useRef, useState } from 'react';
import useSWR from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import {
  TEST_POLL_MS,
  useRunner,
  type TestCommandOutcome,
} from '@/hooks/tests/useTestStart';
import type { TestDetail, TestRound } from '@/lib/tests/take-to-test';
import {
  packageReady,
  type BindingChoice,
  type BindingSuggestions,
  type ReportUploadBody,
  type TestBindings,
  type TestProgress,
  type TestReport,
  type TestResult,
  type TestSchedule,
} from '@/lib/tests/test-in-progress';

type Download =
  components['schemas']['app__generation__response_models__DownloadIntentResponse'];

/** A short, stable name for one upload, so the same file sent again replays the same command. */
function fingerprint(value: string): string {
  let hash = 5381;
  for (let i = 0; i < value.length; i += 1)
    hash = (hash * 33) ^ value.charCodeAt(i);
  return `${value.length}:${(hash >>> 0).toString(36)}`;
}

async function read<T>(
  answer: Promise<{ kind: string; data?: unknown; problem?: ProblemDetails }>
): Promise<T> {
  const result = await answer;
  if (result.kind === 'ok') return result.data as T;
  throw (result as { problem: ProblemDetails }).problem;
}

export interface TestInProgress {
  test: TestDetail | undefined;
  round: TestRound | undefined;
  progress: TestProgress | undefined;
  result: TestResult | undefined;
  schedule: TestSchedule | undefined;
  /** A read failed; the page says so and offers to read again. */
  readProblem: ProblemDetails | null;
  loading: boolean;
  busy: boolean;
  refresh: () => Promise<void>;
  preview: (body: ReportUploadBody) => Promise<TestCommandOutcome<TestReport>>;
  suggest: (batchId: string) => Promise<TestCommandOutcome<BindingSuggestions>>;
  confirmBindings: (
    adAccountId: string,
    bindings: BindingChoice[]
  ) => Promise<TestCommandOutcome<TestBindings>>;
  confirmReport: (batchId: string) => Promise<TestCommandOutcome<TestReport>>;
  moveStart: (
    startDate: string,
    reason?: string
  ) => Promise<TestCommandOutcome<TestSchedule>>;
  endEarly: (
    endDate: string,
    reason?: string
  ) => Promise<TestCommandOutcome<TestSchedule>>;
  recordDeviation: (
    assetVersionIds: string[],
    note: string
  ) => Promise<TestCommandOutcome<TestProgress>>;
  /** "See the result": evaluate the round (free), then read its result again. */
  seeResult: () => Promise<TestCommandOutcome<unknown>>;
  download: () => Promise<TestCommandOutcome<Download>>;
}

/**
 * A test round once its ad package is made (design §5.7–§5.9, #257). It
 * reads the test, the round (package, matches, uploads), the numbers so far
 * and the schedule with the minimal result; every command names the round's
 * revision (If-Match), runs with one action id per intended command so a
 * lost reply replays it, and reads everything again once answered.
 */
export function useTestInProgress(testId: string): TestInProgress {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const run = useRunner();
  const slug = current.slug;
  const ws = current.workspaceId;

  const testQuery = useSWR(
    ['marketing-test', ws, testId],
    () =>
      read<TestDetail>(
        gateway.getMarketingTest({ workspaceSlug: slug, testId })
      ),
    { revalidateOnFocus: false }
  );
  const test = testQuery.data;
  const roundId =
    test?.current_round_id ?? test?.rounds[test.rounds.length - 1]?.id ?? null;

  const roundQuery = useSWR(
    roundId ? ['marketing-test-run-round', ws, roundId] : null,
    async () => {
      const envelope = await read<{ data: TestRound }>(
        gateway.getMarketingRound({
          workspaceSlug: slug,
          roundId: roundId!,
          includeTextLanguage: true,
        })
      );
      return envelope.data;
    },
    {
      // The package is still being made: ask again until it is ready or failed.
      refreshInterval: (latest) =>
        latest?.handoff &&
        (latest.handoff.state === 'queued' ||
          latest.handoff.state === 'running')
          ? TEST_POLL_MS
          : 0,
    }
  );
  const round = roundQuery.data;
  const ready = packageReady(round);

  const progressQuery = useSWR(
    roundId && ready ? ['marketing-test-progress', ws, testId, roundId] : null,
    () =>
      read<TestProgress>(
        gateway.getMarketingTestProgress({
          workspaceSlug: slug,
          testId,
          roundId: roundId!,
        })
      ),
    { revalidateOnFocus: false }
  );
  const resultQuery = useSWR(
    roundId ? ['marketing-test-result', ws, testId, roundId] : null,
    () =>
      read<TestResult>(
        gateway.getMarketingTestResult({
          workspaceSlug: slug,
          testId,
          roundId: roundId!,
        })
      ),
    { revalidateOnFocus: false }
  );

  // Every read and command answer names the round's revision; it only grows,
  // so the newest one seen is the If-Match of the next command.
  const revision = useRef(0);
  revision.current = Math.max(
    revision.current,
    round?.revision ?? 0,
    progressQuery.data?.revision ?? 0,
    resultQuery.data?.schedule.revision ?? 0
  );

  const [busy, setBusy] = useState(false);
  const { mutate: mutateTest } = testQuery;
  const { mutate: mutateRound } = roundQuery;
  const { mutate: mutateProgress } = progressQuery;
  const { mutate: mutateResult } = resultQuery;
  const refresh = useCallback(async () => {
    await Promise.all([
      mutateTest(),
      mutateRound(),
      mutateProgress(),
      mutateResult(),
    ]);
  }, [mutateProgress, mutateResult, mutateRound, mutateTest]);

  /** Run one command, then read everything again (also after a refusal: something changed). */
  const command = useCallback(
    async <T>(
      identity: string,
      send: (action: string, expected: number) => Promise<unknown>,
      options: { reread?: boolean } = {}
    ): Promise<TestCommandOutcome<T>> => {
      setBusy(true);
      try {
        const expected = revision.current;
        const outcome = await run<T>(
          identity,
          (action) => send(action, expected) as never
        );
        if (outcome.kind !== 'uncertain' && options.reread !== false)
          await refresh();
        return outcome;
      } finally {
        setBusy(false);
      }
    },
    [refresh, run]
  );

  const target = { workspaceSlug: slug, testId, roundId: roundId ?? '' };

  const preview = (body: ReportUploadBody) =>
    command<TestReport>(
      `preview:${roundId}:${fingerprint(JSON.stringify(body))}`,
      (action, expected) =>
        gateway.previewMarketingTestReport({
          ...target,
          actionInstanceId: action,
          expectedRevision: expected,
          body,
        })
    );

  const suggest = (batchId: string) =>
    command<BindingSuggestions>(
      `suggest:${batchId}:${revision.current}`,
      (action) =>
        gateway.suggestMarketingTestBindings({
          ...target,
          actionInstanceId: action,
          body: { batch_id: batchId },
        }),
      // Reading the names stores nothing.
      { reread: false }
    );

  const confirmBindings = (adAccountId: string, bindings: BindingChoice[]) =>
    command<TestBindings>(
      `bindings:${roundId}:${adAccountId}:${JSON.stringify(bindings)}`,
      (action, expected) =>
        gateway.confirmMarketingTestBindings({
          ...target,
          actionInstanceId: action,
          expectedRevision: expected,
          body: { ad_account_id: adAccountId, bindings },
        })
    );

  const confirmReport = (batchId: string) =>
    command<TestReport>(`confirm-report:${batchId}`, (action, expected) =>
      gateway.confirmMarketingTestReport({
        ...target,
        actionInstanceId: action,
        expectedRevision: expected,
        body: { batch_id: batchId },
      })
    );

  const moveStart = (startDate: string, reason?: string) =>
    command<TestSchedule>(`start:${roundId}:${startDate}`, (action, expected) =>
      gateway.changeMarketingTestStart({
        ...target,
        actionInstanceId: action,
        expectedRevision: expected,
        body: { start_date: startDate, reason: reason?.trim() || null },
      })
    );

  const endEarly = (endDate: string, reason?: string) =>
    command<TestSchedule>(`end:${roundId}:${endDate}`, (action, expected) =>
      gateway.endMarketingTestRoundEarly({
        ...target,
        actionInstanceId: action,
        expectedRevision: expected,
        body: { end_date: endDate, reason: reason?.trim() || null },
      })
    );

  const recordDeviation = (assetVersionIds: string[], note: string) =>
    command<TestProgress>(
      `deviation:${roundId}:${[...assetVersionIds].sort().join(',')}:${fingerprint(note.trim())}`,
      (action, expected) =>
        gateway.recordMarketingTestDeviation({
          ...target,
          actionInstanceId: action,
          expectedRevision: expected,
          body: { asset_version_ids: assetVersionIds, note: note.trim() },
        })
    );

  const seeResult = () =>
    command<unknown>(
      `evaluate:${roundId}:${revision.current}`,
      (action, expected) =>
        gateway.evaluateMarketingRound({
          workspaceSlug: slug,
          actionInstanceId: action,
          roundId: roundId ?? '',
          expectedRevision: expected,
        })
    );

  const download = () =>
    command<Download>(
      `download:${roundId}`,
      (action) =>
        gateway.downloadMarketingHandoff({
          workspaceSlug: slug,
          actionInstanceId: action,
          roundId: roundId ?? '',
        }),
      { reread: false }
    );

  const readProblem =
    (testQuery.error as ProblemDetails | undefined) ??
    (roundQuery.error as ProblemDetails | undefined) ??
    (progressQuery.error as ProblemDetails | undefined) ??
    (resultQuery.error as ProblemDetails | undefined) ??
    null;

  return {
    test,
    round,
    progress: progressQuery.data,
    result: resultQuery.data,
    schedule: resultQuery.data?.schedule,
    readProblem,
    loading:
      !test || !round || !resultQuery.data || (ready && !progressQuery.data),
    busy,
    refresh,
    preview,
    suggest,
    confirmBindings,
    confirmReport,
    moveStart,
    endEarly,
    recordDeviation,
    seeResult,
    download,
  };
}
