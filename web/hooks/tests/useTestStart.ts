import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import useSWR from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ReconciliationRequired } from '@/lib/domain-client/intent';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import {
  checkProgress,
  openAds,
  roundBusy,
  unprovenAds,
  type OpenAd,
  type TestDetail,
  type TestPackage,
  type TestAnswers,
  type TestQuote,
  type TestRound,
  type UnprovenAd,
} from '@/lib/tests/take-to-test';

/** How often the page asks again while a check or the ad package is on its way. */
export const TEST_POLL_MS = 3_000;

/** How one command ended. */
export type TestCommandOutcome<T> =
  | { kind: 'done'; data: T }
  /** Refused; `problem.code` says why and nothing was written. */
  | { kind: 'refused'; problem: ProblemDetails }
  /** No answer arrived; asking again repeats the same command. */
  | { kind: 'uncertain' };

type Answer<T> =
  | { kind: 'ok'; data: T }
  | { kind: 'created'; data: T }
  | { kind: 'accepted'; data: T }
  | { kind: 'problem'; problem: ProblemDetails }
  | ReconciliationRequired
  | { kind: string };

/** A refusal's `params`, which the generated problem type leaves out. */
export function problemParams<T = Record<string, unknown>>(
  problem: ProblemDetails | null | undefined
): T | null {
  return ((problem as { params?: T } | null | undefined)?.params ??
    null) as T | null;
}

/**
 * One command runner per hook: one action id per intended command, kept
 * until it is answered, so asking again after a lost reply replays the same
 * command (idempotency) instead of starting a second one.
 */
function useRunner() {
  const actions = useRef(new Map<string, string>());
  return useCallback(
    async <T>(
      identity: string,
      send: (action: string) => Promise<Answer<T>>
    ): Promise<TestCommandOutcome<T>> => {
      const action = actions.current.get(identity) ?? crypto.randomUUID();
      actions.current.set(identity, action);
      let result: Answer<T>;
      try {
        result = await send(action);
      } catch {
        return { kind: 'uncertain' };
      }
      if (
        result.kind === 'ok' ||
        result.kind === 'created' ||
        result.kind === 'accepted'
      ) {
        actions.current.delete(identity);
        return { kind: 'done', data: (result as { data: T }).data };
      }
      if (result.kind === 'problem') {
        actions.current.delete(identity);
        return {
          kind: 'refused',
          problem: (result as { problem: ProblemDetails }).problem,
        };
      }
      return { kind: 'uncertain' };
    },
    []
  );
}

/** The SWR key of a product's tests; starting or cancelling one refreshes it. */
export const productTestsKey = (workspaceId: string, productId: string) =>
  ['marketing-tests', workspaceId, productId] as const;

export interface ProductTestInProgress {
  /** Still reading the product's tests. */
  loading: boolean;
  /** The product's test in progress (a round not closed), if any (plan E3). */
  testId: string | null;
  refresh: () => Promise<void>;
}

/**
 * The product's test in progress, if any (`list_marketing_tests`, #255): a
 * product has at most one (plan E3), and the "take to test" screen carries
 * on with it -- its checks, its package, its download -- when the page is
 * opened again (plan E7). A list that cannot be read leaves the form: the
 * start command itself refuses a second test (`test_in_progress`).
 */
export function useProductTestInProgress(
  productId: string
): ProductTestInProgress {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const query = useSWR(
    productTestsKey(current.workspaceId, productId),
    async () => {
      const result = await gateway.listMarketingTests({
        workspaceSlug: current.slug,
        productId,
      });
      return result.kind === 'ok' ? result.data : null;
    },
    { revalidateOnFocus: false }
  );
  const { mutate } = query;
  const refresh = useCallback(async () => {
    await mutate();
  }, [mutate]);
  const running = query.data?.tests.find((test) => test.current_round_id);
  return {
    loading: query.data === undefined && !query.error,
    testId: running?.id ?? null,
    refresh,
  };
}

export interface StartRequest {
  quote: TestQuote;
  /** The answers the quote was read with. */
  query: TestAnswers;
  /** The kept ads the page showed. */
  assetVersionIds: string[];
}

/**
 * "Start the test · at most $X" (`start_marketing_test`, #249): the quote
 * the page shows, its hash and the kept ads, confirmed with one click; the
 * ideas' revision the quote read is the If-Match. Anything that changed since
 * is refused (`test_quote_changed`, `stale_marketing_revision`) and the page
 * asks the user to look again.
 */
export function useTestStart(projectId: string) {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const run = useRunner();
  const start = useCallback(
    (request: StartRequest) => {
      const ids = [...request.assetVersionIds].sort();
      return run<TestDetail>(
        `start:${request.quote.quote_hash}:${ids.join(',')}`,
        (action) =>
          gateway.startMarketingTest({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            projectId,
            expectedRevision: request.quote.ideas_revision,
            body: {
              answers: request.query,
              quote_hash: request.quote.quote_hash,
              asset_version_ids: ids,
            },
          })
      );
    },
    [current.slug, gateway, projectId, run]
  );
  return { start };
}

/** Refusals of the package command the page waits out and asks again. */
const WAIT_CODES = new Set(['checks_running']);
/** Refusals that mean the page read an older round: read it again, then ask again. */
const STALE_CODES = new Set(['stale_round_revision']);

export type StartStage =
  /** Reading the test and its round. */
  | 'loading'
  /** The "look together" checks are running. */
  | 'checking'
  /** Every check answered: the round's reviews and the package are being made. */
  | 'packaging'
  /** Some checks did not pass: only those ads open, each to accept with a reason. */
  | 'findings'
  /** A check has nothing to show it passed (it failed or is outdated). */
  | 'unproven'
  /** Refused for another reason: the page says it and offers to cancel. */
  | 'blocked'
  | 'ready'
  | 'package_failed'
  /** The round ended (cancelled here or elsewhere). */
  | 'closed';

export interface StartProgress {
  stage: StartStage;
  test: TestDetail | undefined;
  round: TestRound | undefined;
  progress: { total: number; answered: number };
  /** `findings`: the ads to look at. */
  open: OpenAd[];
  /** `unproven`: the ads without evidence, with why. */
  unproven: UnprovenAd[];
  /** The last refusal or failed read, for the page to say. */
  problem: ProblemDetails | null;
  /** A command's answer never came; the page says so and asks again by itself. */
  uncertain: boolean;
  busy: boolean;
  /** The download link once asked for. */
  downloadUrl: string | null;
  /** Accept the open findings, one reason per ad, and make the package. */
  accept: (notes: Record<string, string>) => Promise<void>;
  cancel: () => Promise<TestCommandOutcome<TestDetail>>;
  download: () => Promise<void>;
  retryPackage: () => Promise<void>;
  refresh: () => Promise<void>;
}

/**
 * A started test until its ad package downloads (design §5.3, plan E7). It
 * reads the test and follows its round while the "look together" checks
 * run; once every check has its result it asks for the package
 * (`complete_marketing_test_start`, idempotent, If-Match the round's
 * revision) by itself -- also when the page is opened again later. Checks
 * that did not pass open on their own; the user accepts them with a reason
 * (sent with the next ask) or cancels the test to swap or take out an ad.
 */
export function useStartProgress(testId: string): StartProgress {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const run = useRunner();
  const slug = current.slug;

  const testQuery = useSWR(
    ['marketing-test', current.workspaceId, testId],
    async () => {
      const result = await gateway.getMarketingTest({
        workspaceSlug: slug,
        testId,
      });
      if (result.kind === 'ok') return result.data;
      throw (result as { problem: ProblemDetails }).problem;
    },
    { revalidateOnFocus: false }
  );
  const test = testQuery.data;
  // A cancelled test has no current round; its last round is still read, closed.
  const roundId =
    test?.current_round_id ?? test?.rounds[test.rounds.length - 1]?.id ?? null;

  const [waiting, setWaiting] = useState(false);
  const roundQuery = useSWR(
    roundId ? ['marketing-test-round', current.workspaceId, roundId] : null,
    async () => {
      const result = await gateway.getMarketingRound({
        workspaceSlug: slug,
        roundId: roundId!,
        includeQcKinds: true,
      });
      if (result.kind === 'ok')
        return (result.data as unknown as { data: TestRound }).data;
      throw (result as { problem: ProblemDetails }).problem;
    },
    {
      refreshInterval: (latest) =>
        roundBusy(latest) || waiting ? TEST_POLL_MS : 0,
    }
  );
  const round = roundQuery.data;
  const { mutate: mutateRound } = roundQuery;

  const [refusal, setRefusal] = useState<ProblemDetails | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [busy, setBusy] = useState(false);
  const [asking, setAsking] = useState(false);
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const accepted = useRef<
    components['schemas']['CompleteMarketingTestStart']['accepted_findings']
  >([]);
  const attempted = useRef<string | null>(null);
  const [tick, setTick] = useState(0);

  const complete = useCallback(
    async (latest: TestRound) => {
      const body = { accepted_findings: accepted.current ?? [] };
      setAsking(true);
      try {
        const outcome = await run<TestPackage>(
          `package:${latest.id}:${latest.revision}:${JSON.stringify(body)}`,
          (action) =>
            gateway.completeMarketingTestStart({
              workspaceSlug: slug,
              actionInstanceId: action,
              testId,
              expectedRevision: latest.revision,
              body,
            })
        );
        if (outcome.kind === 'done') {
          setRefusal(null);
          setUncertain(false);
          setWaiting(false);
          await mutateRound();
          return;
        }
        if (outcome.kind === 'uncertain') {
          // Asked again with the same action id: never a second package.
          setUncertain(true);
          setWaiting(true);
          setTimeout(() => {
            attempted.current = null;
            setTick((n) => n + 1);
          }, TEST_POLL_MS);
          return;
        }
        const code = outcome.problem.code;
        if (WAIT_CODES.has(code) || STALE_CODES.has(code)) {
          setWaiting(true);
          await mutateRound();
          setTimeout(() => {
            attempted.current = null;
            setTick((n) => n + 1);
          }, TEST_POLL_MS);
          return;
        }
        setUncertain(false);
        setWaiting(false);
        setRefusal(outcome.problem);
        if (code === 'round_closed') await testQuery.mutate();
      } finally {
        setAsking(false);
      }
    },
    [gateway, mutateRound, run, slug, testId, testQuery]
  );

  // Every check has its result and the round has no package yet: ask for it
  // (plan E7), once per state of the round -- again after a wait, a stale
  // revision or an answer that never came.
  useEffect(() => {
    if (!round || round.handoff || round.state === 'closed') return;
    if (!checkProgress(round).allAnswered) return;
    const key = `${round.revision}:${round.assets
      .map((a) => `${a.paired_ai_qc?.id}:${a.paired_ai_qc?.state}`)
      .join(',')}`;
    if (attempted.current === key) return;
    attempted.current = key;
    void complete(round);
  }, [round, complete, tick]);

  const open = useMemo(
    () =>
      refusal?.code === 'check_findings_open'
        ? openAds(problemParams(refusal), round)
        : [],
    [refusal, round]
  );
  const unproven = useMemo(
    () =>
      refusal?.code === 'check_evidence_missing'
        ? unprovenAds(problemParams(refusal), round)
        : [],
    [refusal, round]
  );

  const accept = useCallback(
    async (notes: Record<string, string>) => {
      if (!round) return;
      accepted.current = open.map((ad) => ({
        asset_version_id: ad.assetVersionId,
        check_id: ad.checkId,
        finding_ids: ad.findings.map((finding) => finding.check_id),
        note: (notes[ad.assetVersionId] ?? '').trim(),
      }));
      attempted.current = null;
      setRefusal(null);
      setTick((n) => n + 1);
    },
    [open, round]
  );

  const cancel = useCallback(async () => {
    setBusy(true);
    try {
      const outcome = await run<TestDetail>(
        `cancel:${testId}:${round?.revision}`,
        (action) =>
          gateway.cancelMarketingTest({
            workspaceSlug: slug,
            actionInstanceId: action,
            testId,
            expectedRevision: round?.revision,
            body: {},
          })
      );
      if (outcome.kind === 'done') {
        await testQuery.mutate(outcome.data, { revalidate: false });
        await mutateRound();
      } else if (outcome.kind === 'refused') setRefusal(outcome.problem);
      return outcome;
    } finally {
      setBusy(false);
    }
  }, [gateway, mutateRound, round?.revision, run, slug, testId, testQuery]);

  const download = useCallback(async () => {
    if (!round) return;
    setBusy(true);
    try {
      const outcome = await run<
        components['schemas']['app__generation__response_models__DownloadIntentResponse']
      >(`download:${round.id}`, (action) =>
        gateway.downloadMarketingHandoff({
          workspaceSlug: slug,
          actionInstanceId: action,
          roundId: round.id,
        })
      );
      if (outcome.kind === 'done' && outcome.data.download_grant?.url) {
        setDownloadUrl(outcome.data.download_grant.url);
        setUncertain(false);
      } else if (outcome.kind === 'refused') setRefusal(outcome.problem);
      else setUncertain(true);
    } finally {
      setBusy(false);
    }
  }, [gateway, round, run, slug]);

  const retryPackage = useCallback(async () => {
    if (!round) return;
    setBusy(true);
    try {
      const outcome = await run(`retry-package:${round.revision}`, (action) =>
        gateway.retryMarketingHandoff({
          workspaceSlug: slug,
          actionInstanceId: action,
          roundId: round.id,
          expectedRevision: round.revision,
        })
      );
      if (outcome.kind === 'refused') setRefusal(outcome.problem);
      setUncertain(outcome.kind === 'uncertain');
      await mutateRound();
    } finally {
      setBusy(false);
    }
  }, [gateway, mutateRound, round, run, slug]);

  const refresh = useCallback(async () => {
    // Reasons given for an older check would be refused (`check_changed`):
    // the newest check's findings open again instead.
    accepted.current = [];
    setRefusal(null);
    attempted.current = null;
    await testQuery.mutate();
    await mutateRound();
    setTick((n) => n + 1);
  }, [mutateRound, testQuery]);

  const progress = round
    ? checkProgress(round)
    : { total: 0, answered: 0, allAnswered: false };
  const readProblem =
    (testQuery.error as ProblemDetails | undefined) ??
    (roundQuery.error as ProblemDetails | undefined) ??
    null;

  let stage: StartStage;
  if (!test || !round) stage = 'loading';
  else if (round.handoff?.state === 'ready') stage = 'ready';
  else if (round.handoff?.state === 'failed') stage = 'package_failed';
  else if (round.handoff) stage = 'packaging';
  else if (round.state === 'closed' || refusal?.code === 'round_closed')
    stage = 'closed';
  else if (refusal?.code === 'check_findings_open' && open.length)
    stage = 'findings';
  else if (refusal?.code === 'check_evidence_missing') stage = 'unproven';
  else if (refusal) stage = 'blocked';
  else if (progress.allAnswered || asking) stage = 'packaging';
  else stage = 'checking';

  return {
    stage,
    test,
    round,
    progress: { total: progress.total, answered: progress.answered },
    open,
    unproven,
    problem: refusal ?? readProblem,
    uncertain,
    busy: busy || asking,
    downloadUrl,
    accept,
    cancel,
    download,
    retryPackage,
    refresh,
  };
}
