import { useCallback, useMemo, useRef } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ReconciliationRequired } from '@/lib/domain-client/intent';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';
import { ideasProjectKey, type Loaded } from './useIdeasProject';

/**
 * The commands of the ideas page, the check before downloading and the
 * edit screen (ADR 0018, #198, #200, #201). Each carries the revision the
 * user saw where the server asks for one (If-Match); an answer that is the
 * project is written into the ideas page's cache, so every view agrees.
 */

export type IdeasDownload = components['schemas']['IdeasDownload'];
export type GenerationPreview =
  components['schemas']['GenerationPreviewDetail'];
export type FeedbackDraft = components['schemas']['IdeasFeedbackDraft'];
export type PrepareFeedback = components['schemas']['PrepareIdeasFeedback'];
export type IdeasAddition = components['schemas']['IdeasAddition'];
export type ImageEditBody = components['schemas']['CreateIdeasImageEdit'];
export type ChecksQuote = components['schemas']['IdeasChecksQuote'];
type VerifyBody = components['schemas']['VerifyIdeas'];
type CallSummary = components['schemas']['IdeasCallSummary'];
type Drafts = components['schemas']['IdeasGenerationDrafts'];
type OperationRef = components['schemas']['OperationRef'];

/** The image settings an ad of these ideas is made with (design §5.3, "出图设置"). */
export interface ImageSettings {
  provider: components['schemas']['PrepareIdeasGeneration']['provider'];
  model: string;
  aspect_ratio: NonNullable<
    components['schemas']['PrepareIdeasGeneration']['aspect_ratio']
  >;
  image_size: NonNullable<
    components['schemas']['PrepareIdeasGeneration']['image_size']
  >;
  variants: number;
}

/** How one command ended. */
export type CommandOutcome<T> =
  | { kind: 'done'; data: T }
  /** Refused; `problem.code` says why. */
  | { kind: 'refused'; problem: ProblemDetails }
  /** No answer arrived; asking again repeats the same command. */
  | { kind: 'uncertain' };

type Answer<T> =
  | { kind: 'ok'; data: T }
  /** A 201 answer (`create_*`, `prepare_*`, `download_*`, `place_*`). */
  | { kind: 'created'; data: T }
  | { kind: 'accepted'; data: T }
  | { kind: 'problem'; problem: ProblemDetails }
  | ReconciliationRequired
  | { kind: string };

/** The largest amount a preview may cost (#200: the confirmed number). */
export function previewCost(preview: GenerationPreview): string {
  return preview.maximum_cost?.amount ?? preview.selection_maximum_cost ?? '0';
}

export interface IdeasCommands {
  /** One "check before download": facts, looks, findings (#198). */
  verify: (body: VerifyBody) => Promise<CommandOutcome<IdeasDetail>>;
  /** Exactly these ads, one short-lived link each (#198). */
  download: (ids: string[]) => Promise<CommandOutcome<IdeasDownload>>;
  /** Checks the ads the quote counted, confirming its maximum (#197). */
  startChecks: (quoteHash: string) => Promise<CommandOutcome<IdeasDetail>>;
  /** One more claim, or one more way of telling a claim (#200). */
  addIdeas: (
    addition: IdeasAddition,
    quoteHash: string
  ) => Promise<CommandOutcome<CallSummary>>;
  /** Prepares a correction or an image tool of one ad; nothing is spent (#200). */
  prepareFeedback: (
    body: PrepareFeedback
  ) => Promise<CommandOutcome<FeedbackDraft>>;
  /**
   * The generation draft of one ad of the ideas at the current revision
   * (#191). The server keeps one set of drafts per revision, made with one set
   * of image settings: `settings` are tried in order while it answers that the
   * drafts were prepared with other settings, and `onPrepared` hears which
   * ones it took.
   */
  draftFor: (
    combinationId: string,
    settings: ImageSettings[],
    onPrepared?: (revision: number, settings: ImageSettings) => void
  ) => Promise<CommandOutcome<string>>;
  /** Freezes a draft into a preview: the maximum cost to confirm. */
  quoteDraft: (draftId: string) => Promise<CommandOutcome<GenerationPreview>>;
  /** Confirms a preview's maximum and makes the image. */
  submitPreview: (
    preview: GenerationPreview
  ) => Promise<CommandOutcome<OperationRef>>;
  /** Crop and rotate one ad: a free new version (#200). */
  editImage: (body: ImageEditBody) => Promise<CommandOutcome<IdeasDetail>>;
  /** Places an uploaded image under one ad of the ideas (#200). */
  placeUpload: (
    externalCreativeId: string,
    combinationId: string
  ) => Promise<CommandOutcome<IdeasDetail>>;
  /** Asks the server for the ideas again. */
  refresh: () => Promise<void>;
}

export function useIdeasCommands(
  projectId: string,
  detail: IdeasDetail | undefined
): IdeasCommands {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const { mutate } = useSWRConfig();
  // One action id per intended command, kept until it is answered, so asking
  // again after a lost reply replays the same command (idempotency).
  const actions = useRef(new Map<string, string>());
  const revision = detail?.revision;
  const key = useMemo(
    () => ideasProjectKey(current.workspaceId, projectId),
    [current.workspaceId, projectId]
  );
  const slug = current.slug;

  const run = useCallback(
    async <T>(
      identity: string,
      send: (action: string) => Promise<Answer<T>>
    ): Promise<CommandOutcome<T>> => {
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

  const store = useCallback(
    async (outcome: CommandOutcome<IdeasDetail>) => {
      if (outcome.kind === 'done') {
        const loaded: Loaded = { detail: outcome.data };
        await mutate(key, loaded, { revalidate: false });
      }
      return outcome;
    },
    [key, mutate]
  );

  const refresh = useCallback(async () => {
    await mutate(key);
  }, [key, mutate]);

  const verify = useCallback(
    async (body: VerifyBody) =>
      store(
        await run<IdeasDetail>(
          `verify:${revision}:${JSON.stringify(body)}`,
          (action) =>
            gateway.verifyMarketingIdeas({
              workspaceSlug: slug,
              actionInstanceId: action,
              expectedRevision: revision,
              projectId,
              body,
            })
        )
      ),
    [gateway, projectId, revision, run, slug, store]
  );

  const download = useCallback(
    (ids: string[]) =>
      run<IdeasDownload>(`download:${ids.join(',')}`, (action) =>
        gateway.downloadMarketingIdeas({
          workspaceSlug: slug,
          actionInstanceId: action,
          projectId,
          body: { asset_version_ids: ids },
        })
      ),
    [gateway, projectId, run, slug]
  );

  const startChecks = useCallback(
    async (quoteHash: string) =>
      store(
        await run<IdeasDetail>(`checks:${quoteHash}`, (action) =>
          gateway.startMarketingIdeasChecks({
            workspaceSlug: slug,
            actionInstanceId: action,
            projectId,
            body: { execution_mode: 'byok', quote_hash: quoteHash },
          })
        )
      ),
    [gateway, projectId, run, slug, store]
  );

  const strategy = detail?.ideas?.strategy_language ?? 'en-US';
  const addIdeas = useCallback(
    async (addition: IdeasAddition, quoteHash: string) => {
      const outcome = await run<CallSummary>(
        `add:${revision}:${quoteHash}:${JSON.stringify(addition)}`,
        (action) =>
          gateway.startMarketingIdeasPlanning({
            workspaceSlug: slug,
            actionInstanceId: action,
            expectedRevision: revision,
            projectId,
            body: {
              kind: 'ideas',
              strategy_language: strategy,
              execution_mode: 'byok',
              quote_hash: quoteHash,
              addition,
            },
          })
      );
      if (outcome.kind === 'done') await refresh();
      return outcome;
    },
    [gateway, projectId, refresh, revision, run, slug, strategy]
  );

  const prepareFeedback = useCallback(
    (body: PrepareFeedback) =>
      run<FeedbackDraft>(`feedback:${JSON.stringify(body)}`, (action) =>
        gateway.prepareMarketingIdeasFeedback({
          workspaceSlug: slug,
          actionInstanceId: action,
          projectId,
          body,
        })
      ),
    [gateway, projectId, run, slug]
  );

  const draftFor = useCallback(
    async (
      combinationId: string,
      candidates: ImageSettings[],
      onPrepared?: (revision: number, settings: ImageSettings) => void
    ): Promise<CommandOutcome<string>> => {
      const tried = new Set<string>();
      let outcome: CommandOutcome<Drafts> = { kind: 'uncertain' };
      for (const settings of candidates) {
        const key = JSON.stringify(settings);
        if (tried.has(key)) continue;
        tried.add(key);
        outcome = await run<Drafts>(`drafts:${revision}:${key}`, (action) =>
          gateway.prepareMarketingIdeasGeneration({
            workspaceSlug: slug,
            actionInstanceId: action,
            expectedRevision: revision,
            projectId,
            body: settings,
          })
        );
        if (outcome.kind === 'done') {
          if (revision !== undefined) onPrepared?.(revision, settings);
          break;
        }
        if (
          outcome.kind !== 'refused' ||
          outcome.problem.code !== 'ideas_drafts_already_prepared'
        )
          break;
      }
      if (outcome.kind !== 'done') return outcome;
      const draft = outcome.data.drafts.find(
        (item) => item.combination_id === combinationId
      );
      return draft
        ? { kind: 'done', data: draft.draft_id }
        : {
            kind: 'refused',
            problem: {
              type: 'about:blank',
              title: '',
              status: 404,
              detail: '',
              instance: '',
              code: 'combination_not_found',
              trace_id: '',
              retryable: false,
              field_errors: [],
            },
          };
    },
    [gateway, projectId, revision, run, slug]
  );

  const quoteDraft = useCallback(
    async (draftId: string): Promise<CommandOutcome<GenerationPreview>> => {
      let draft;
      try {
        draft = await gateway.getGenerationDraft({
          workspaceSlug: slug,
          draftId,
        });
      } catch {
        return { kind: 'uncertain' };
      }
      if (draft.kind === 'problem')
        return { kind: 'refused', problem: draft.problem };
      if (draft.kind !== 'ok') return { kind: 'uncertain' };
      const draftRevision = draft.data.revision;
      return run<GenerationPreview>(
        `preview:${draftId}:${draftRevision}`,
        (action) =>
          gateway.createGenerationPreview({
            workspaceSlug: slug,
            actionInstanceId: action,
            draftId,
            ifMatch: draftRevision,
          })
      );
    },
    [gateway, run, slug]
  );

  const submitPreview = useCallback(
    async (preview: GenerationPreview) => {
      const outcome = await run<OperationRef>(
        `submit:${preview.id}`,
        (action) =>
          gateway.submitGenerationRun({
            workspaceSlug: slug,
            actionInstanceId: action,
            ifMatch: preview.draft_revision,
            body: {
              preview_id: preview.id,
              preview_hash: preview.preview_hash,
            },
          })
      );
      if (outcome.kind === 'done') await refresh();
      return outcome;
    },
    [gateway, refresh, run, slug]
  );

  const editImage = useCallback(
    async (body: ImageEditBody) =>
      store(
        await run<IdeasDetail>(`edit:${JSON.stringify(body)}`, (action) =>
          gateway.createMarketingIdeasImageEdit({
            workspaceSlug: slug,
            actionInstanceId: action,
            projectId,
            body,
          })
        )
      ),
    [gateway, projectId, run, slug, store]
  );

  const placeUpload = useCallback(
    async (externalCreativeId: string, combinationId: string) =>
      store(
        await run<IdeasDetail>(
          `upload:${externalCreativeId}:${combinationId}`,
          (action) =>
            gateway.placeMarketingIdeasUpload({
              workspaceSlug: slug,
              actionInstanceId: action,
              projectId,
              body: {
                external_creative_id: externalCreativeId,
                combination_id: combinationId,
              },
            })
        )
      ),
    [gateway, projectId, run, slug, store]
  );

  return {
    verify,
    download,
    startChecks,
    addIdeas,
    prepareFeedback,
    draftFor,
    quoteDraft,
    submitPreview,
    editImage,
    placeUpload,
    refresh,
  };
}

export type Quote<T> =
  | { kind: 'ok'; data: T }
  | { kind: 'problem'; problem: ProblemDetails }
  | { kind: 'loading' };

/**
 * What checking these ideas' unchecked ads costs at most (#197). Asked again
 * whenever the ads or their checks change, so the button's number is the
 * one the server would accept now.
 */
export function useChecksQuote(
  projectId: string,
  detail: IdeasDetail | undefined,
  enabled = true
): Quote<ChecksQuote> & { refresh: () => Promise<void> } {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const signature = (detail?.ads ?? [])
    .map((item) => `${item.asset_version_id}:${item.check?.state ?? '-'}`)
    .join(',');
  const query = useSWR(
    enabled && detail
      ? [
          'marketing-ideas-checks-quote',
          current.workspaceId,
          projectId,
          signature,
        ]
      : null,
    async () => {
      const result = await gateway.getMarketingIdeasChecksQuote({
        workspaceSlug: current.slug,
        projectId,
        executionMode: 'byok',
      });
      return result.kind === 'ok'
        ? ({ kind: 'ok', data: result.data } as const)
        : ({
            kind: 'problem',
            problem: (result as { problem: ProblemDetails }).problem,
          } as const);
    }
  );
  const { mutate } = query;
  const refresh = useCallback(async () => {
    await mutate();
  }, [mutate]);
  return { ...(query.data ?? { kind: 'loading' as const }), refresh };
}

type PlannerQuote = components['schemas']['PlannerCapabilities']['paid'];

/**
 * What one drafting call costs at most (#192, #200): the planner quote an
 * addition confirms. Read once; the server refuses a changed quote anyway.
 */
export function usePlanningQuote(enabled = true): Quote<PlannerQuote> & {
  refresh: () => Promise<void>;
} {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const query = useSWR(
    enabled ? ['marketing-ideas-planning-quote', current.workspaceId] : null,
    async () => {
      const result = await gateway.getMarketingCapabilities({
        workspaceSlug: current.slug,
      });
      return result.kind === 'ok'
        ? ({ kind: 'ok', data: result.data.paid } as const)
        : ({
            kind: 'problem',
            problem: (result as { problem: ProblemDetails }).problem,
          } as const);
    },
    { revalidateOnFocus: false }
  );
  const { mutate } = query;
  const refresh = useCallback(async () => {
    await mutate();
  }, [mutate]);
  return { ...(query.data ?? { kind: 'loading' as const }), refresh };
}
