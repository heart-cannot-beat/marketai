import { useCallback, useRef, useState } from 'react';
import useSWR from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { useUpload } from '@/hooks/domain/useUpload';
import { waitForOperation } from '@/hooks/products/useProductIntake';
import { newActionId } from '@/components/generation/shared/newActionId';
import { buildIntakeRequest, splitPaste } from '@/lib/products/intake-model';
import { utf8ToBase64 } from '@/lib/credentials/plaintext';
import {
  DomainProblemError,
  type DomainProblem,
} from '@/lib/i18n/domain-errors';
import {
  isTransientProblem,
  transientRetriesExhausted,
} from '@/lib/domain-client/transient-problems';
import {
  isRunning,
  type FirstBatch,
  type FirstBatchQuote,
} from '@/lib/ideas/first-batch-view';

type IntakeDetail = components['schemas']['ProductIntakeDetail'];

/** How often the reading page asks again while a step is on its way. */
export const FIRST_BATCH_POLL_MS = 2_000;
/** While an uncertain call waits to be reconciled, ask now and then. */
export const FIRST_BATCH_UNCERTAIN_POLL_MS = 15_000;

/**
 * The start button's maximum (`get_marketing_first_batch_quote`, #193). It
 * says `openai_key_required` while the workspace has no OpenAI key, and is
 * asked again after the key row saves one.
 */
export function useFirstBatchQuote() {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const query = useSWR(
    ['marketing-first-batch-quote', current.workspaceId],
    async () => {
      const result = await gateway.getMarketingFirstBatchQuote({
        workspaceSlug: current.slug,
        executionMode: 'byok',
      });
      if (result.kind === 'ok') return result.data as FirstBatchQuote;
      throw new DomainProblemError(result.problem);
    },
    // A quote ages only when keys, prices or limits change; the page asks
    // again after saving a key and before every start.
    { revalidateOnFocus: true }
  );
  const { mutate } = query;
  /** Asks again; the new quote, or `undefined` when the read failed. */
  const refresh = useCallback(async (): Promise<
    FirstBatchQuote | undefined
  > => {
    try {
      return await mutate();
    } catch {
      return undefined;
    }
  }, [mutate]);
  return {
    quote: query.data,
    failed: Boolean(query.error),
    refresh,
  };
}

export interface StartInput {
  /** The paste box: a link, text or both. */
  raw: string;
  files: File[];
  market: string;
  adLanguage: string;
  strategyLanguage: 'zh-CN' | 'en-US';
  /** The quote whose maximum the button showed; clicking confirms it. */
  quoteHash: string;
}

export type StartPhase = 'idle' | 'uploading' | 'starting';

/** Why a start did not go through, by its stable code. */
export interface StartProblem {
  code: string;
  problem: DomainProblem | null;
}

export type StartOutcome =
  | { kind: 'started'; batch: FirstBatch }
  | { kind: 'refused'; problem: StartProblem };

/**
 * Starts the first batch (`start_marketing_first_batch`, #193): uploads the
 * dropped images as `product_intake_image`, then starts with the confirmed
 * quote. One action id per intended start, kept until it is answered, so a
 * click after a lost reply replays the same command instead of a new one.
 */
export function useStartFirstBatch() {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const { startUpload } = useUpload('product_intake_image');
  const [phase, setPhase] = useState<StartPhase>('idle');
  const [uploadProgress, setUploadProgress] = useState<{
    done: number;
    total: number;
  } | null>(null);
  // Per intended start (same paste, files, market, language and quote): the
  // action id and the uploaded images, kept until the start is answered, so
  // a click after a lost reply replays the same command with the same body.
  const pending = useRef(
    new Map<string, { action: string; uploadIntentIds?: string[] }>()
  );

  const refuse = (code: string, problem: DomainProblem | null = null) => {
    setPhase('idle');
    setUploadProgress(null);
    return { kind: 'refused' as const, problem: { code, problem } };
  };

  const upload = useCallback(
    async (files: File[]): Promise<string[] | StartProblem> => {
      setPhase('uploading');
      setUploadProgress({ done: 0, total: files.length });
      const ownerId = newActionId();
      const ids: string[] = [];
      for (const file of files) {
        const uploaded = await startUpload({
          file,
          ownerKind: 'product_intake',
          ownerId,
          actionInstanceId: newActionId(),
        });
        if (uploaded.kind !== 'accepted')
          return uploaded.kind === 'problem'
            ? {
                code: uploaded.problem.code || 'upload_failed',
                problem: uploaded.problem,
              }
            : { code: 'upload_failed', problem: null };
        const state = await waitForOperation(
          gateway,
          current.slug,
          uploaded.operationRecordId,
          () => false
        );
        if (state !== 'succeeded')
          return { code: 'upload_rejected', problem: null };
        ids.push(uploaded.uploadIntentId);
        setUploadProgress({ done: ids.length, total: files.length });
      }
      return ids;
    },
    [current.slug, gateway, startUpload]
  );

  const start = useCallback(
    async (input: StartInput): Promise<StartOutcome> => {
      const parts = splitPaste(input.raw);
      const identity = JSON.stringify([
        parts,
        input.files.map((file) => [file.name, file.size, file.lastModified]),
        input.market,
        input.adLanguage,
        input.quoteHash,
      ]);
      const intended = pending.current.get(identity) ?? {
        action: newActionId(),
      };
      pending.current.set(identity, intended);

      let uploadIntentIds = intended.uploadIntentIds ?? [];
      if (input.files.length > 0 && !intended.uploadIntentIds) {
        const uploaded = await upload(input.files).catch(() => ({
          code: 'upload_failed',
          problem: null,
        }));
        if (!Array.isArray(uploaded)) {
          pending.current.delete(identity);
          return refuse(uploaded.code, uploaded.problem);
        }
        uploadIntentIds = uploaded;
        intended.uploadIntentIds = uploaded;
      }
      const intake = buildIntakeRequest(parts, uploadIntentIds);
      if (!intake) {
        pending.current.delete(identity);
        return refuse('intake_input_required');
      }

      setPhase('starting');
      const result = await gateway
        .startMarketingFirstBatch({
          workspaceSlug: current.slug,
          actionInstanceId: intended.action,
          body: {
            url: intake.url ?? null,
            text: intake.text ?? null,
            upload_intent_ids: intake.upload_intent_ids ?? [],
            market: input.market,
            ad_language: input.adLanguage,
            strategy_language: input.strategyLanguage,
            execution_mode: 'byok',
            quote_hash: input.quoteHash,
          },
        })
        .catch(() => null);
      if (result?.kind === 'created') {
        pending.current.delete(identity);
        setPhase('idle');
        setUploadProgress(null);
        return { kind: 'started', batch: result.data as FirstBatch };
      }
      if (result?.kind === 'problem') {
        pending.current.delete(identity);
        return refuse(result.problem.code || 'start_failed', result.problem);
      }
      // No answer (lost reply, network, an unreadable error page): whether it
      // started is unknown, and the same click again replays this action.
      return refuse('start_unconfirmed');
    },
    [current.slug, gateway, upload]
  );

  return { start, phase, uploadProgress };
}

export interface ResumeInput {
  quoteHash: string;
  selectedVariant?: string;
  additionalText?: string;
}

export type ResumeOutcome =
  | { kind: 'resumed' }
  | { kind: 'refused'; problem: DomainProblem | null; uncertain: boolean };

/** A batch, or the final answer that this workspace has no such batch. */
type Loaded =
  | { batch: FirstBatch; notFound?: undefined }
  | { batch?: undefined; notFound: true };

function pollInterval(batch: FirstBatch | undefined): number {
  if (isRunning(batch)) return FIRST_BATCH_POLL_MS;
  if (batch?.attention?.reason === 'uncertain' && !batch.resume)
    return FIRST_BATCH_UNCERTAIN_POLL_MS;
  return 0;
}

/**
 * One first batch (`get_marketing_first_batch`, #193), asked again every
 * couple of seconds while a step is on its way; continuing a stop
 * (`resume_marketing_first_batch`) confirms what its `resume` offers.
 */
export function useFirstBatch(batchId: string | null) {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const query = useSWR<Loaded>(
    batchId ? ['marketing-first-batch', current.workspaceId, batchId] : null,
    async () => {
      const result = await gateway.getMarketingFirstBatch({
        workspaceSlug: current.slug,
        firstBatchId: batchId as string,
      });
      if (result.kind === 'ok') return { batch: result.data as FirstBatch };
      if (result.status === 404) return { notFound: true };
      // Anything else may pass: SWR keeps the last batch on screen and asks
      // again (every 5 s while the workspace's access is still syncing).
      throw new DomainProblemError(result.problem);
    },
    { refreshInterval: (latest) => pollInterval(latest?.batch) }
  );

  const actions = useRef(new Map<string, string>());
  const { mutate } = query;
  const resume = useCallback(
    async (input: ResumeInput): Promise<ResumeOutcome> => {
      if (!batchId) return { kind: 'refused', problem: null, uncertain: false };
      const identity = JSON.stringify([batchId, input]);
      const action = actions.current.get(identity) ?? newActionId();
      actions.current.set(identity, action);
      const result = await gateway
        .resumeMarketingFirstBatch({
          workspaceSlug: current.slug,
          actionInstanceId: action,
          firstBatchId: batchId,
          body: {
            quote_hash: input.quoteHash,
            ...(input.selectedVariant
              ? { selected_variant: input.selectedVariant }
              : {}),
            ...(input.additionalText
              ? { additional_text: input.additionalText }
              : {}),
          },
        })
        .catch(() => null);
      if (result === null)
        // No answer: the same click again replays this action.
        return { kind: 'refused', problem: null, uncertain: true };
      if (result.kind === 'ok') {
        actions.current.delete(identity);
        await mutate(
          { batch: result.data as FirstBatch },
          { revalidate: false }
        );
        return { kind: 'resumed' };
      }
      if (result.kind === 'problem') {
        actions.current.delete(identity);
        // The stop may have changed meanwhile (another tab, a new price).
        void mutate();
        return { kind: 'refused', problem: result.problem, uncertain: false };
      }
      return { kind: 'refused', problem: null, uncertain: true };
    },
    [batchId, current.slug, gateway, mutate]
  );

  const refresh = useCallback(async () => {
    await mutate();
  }, [mutate]);

  const error: unknown = query.error;
  const waitingForAccess =
    error instanceof DomainProblemError &&
    isTransientProblem(error.problem) &&
    !transientRetriesExhausted(error);
  const batch = query.data?.batch;
  return {
    batch,
    notFound: Boolean(query.data?.notFound),
    /** Nothing to show: not found, or the first read failed for good. */
    failed:
      Boolean(query.data?.notFound) ||
      (!batch && Boolean(error) && !waitingForAccess),
    isLoading: !batch && !query.data?.notFound && (!error || waitingForAccess),
    refresh,
    resume,
  };
}

/**
 * The reading behind a first batch (`get_product_intake`): what was pasted,
 * and the product's name once it is read. Asked again while still reading.
 */
export function useFirstBatchIntake(intakeId: string | null, reading: boolean) {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const query = useSWR(
    intakeId
      ? ['marketing-first-batch-intake', current.workspaceId, intakeId]
      : null,
    async () => {
      const result = await gateway.getProductIntake({
        workspaceSlug: current.slug,
        intakeId: intakeId as string,
      });
      if (result.kind === 'ok') return result.data as IntakeDetail;
      throw new DomainProblemError(result.problem);
    },
    { refreshInterval: reading ? FIRST_BATCH_POLL_MS : 0 }
  );
  return query.data;
}

/** The OpenAI key line of the generation binding the first batch runs on. */
const OPENAI = {
  credentialKey: 'openai_api_key',
  provider: 'openai',
  scope: 'generation',
} as const;

export type KeySaveOutcome =
  | { kind: 'saved' }
  /** Refused, not saved: the problem says why. */
  | { kind: 'refused'; problem: DomainProblem }
  /** No answer: whether it was saved is unknown. */
  | { kind: 'unresolved' }
  /** Saved, but not switched on for making ads. */
  | { kind: 'saved_not_enabled'; problem: DomainProblem | null };

/**
 * The start page's key row (design §5.1, #195): saves an OpenAI key after
 * asking OpenAI whether it works (`create_credential` with `verify: true`),
 * then switches it on for making ads (`bind_credential`). Owner only, like
 * Settings, which keeps its own entry.
 */
export function useSaveOpenAIKey() {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  return useCallback(
    async (plaintext: string): Promise<KeySaveOutcome> => {
      const created = await gateway
        .createCredential({
          workspaceSlug: current.slug,
          actionInstanceId: newActionId(),
          body: {
            credential_key: OPENAI.credentialKey,
            provider: OPENAI.provider,
            plaintext_b64: utf8ToBase64(plaintext),
            display_label: null,
            verify: true,
          },
        })
        .catch(() => null);
      if (created === null || created.kind === 'reconciliation_required')
        return { kind: 'unresolved' };
      if (created.kind === 'problem')
        return { kind: 'refused', problem: created.problem };
      // The success body is typed `null` by the contract; read it as data.
      const versionId = (
        created.data as unknown as { credential_version_id?: unknown } | null
      )?.credential_version_id;
      if (typeof versionId !== 'string')
        return { kind: 'saved_not_enabled', problem: null };
      const bound = await gateway
        .bindCredential({
          workspaceSlug: current.slug,
          actionInstanceId: newActionId(),
          credentialVersionId: versionId,
          scope: OPENAI.scope,
          body: { provider: OPENAI.provider },
        })
        .catch(() => null);
      if (bound === null || bound.kind === 'reconciliation_required')
        return { kind: 'saved_not_enabled', problem: null };
      if (bound.kind === 'problem')
        return { kind: 'saved_not_enabled', problem: bound.problem };
      return { kind: 'saved' };
    },
    [current.slug, gateway]
  );
}
