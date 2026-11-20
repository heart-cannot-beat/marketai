import type { components } from '@/lib/domain-client/generated/domain-v1';
import { formatNumber } from '@/lib/i18n/format';

/**
 * What the start page and the reading page show of the first batch (ADR 0018,
 * design §5.1, §5.2, #195), read from `get_marketing_first_batch_quote` and
 * `get_marketing_first_batch`: the step it is at, why it stopped and what
 * continuing takes, and what the start button can say. Nothing here guesses
 * beyond what the server said: an amount is the server's, rounded up.
 */

export type FirstBatch = components['schemas']['FirstBatchDetail'];
export type FirstBatchQuote = components['schemas']['FirstBatchQuote'];
export type FirstBatchResume = components['schemas']['FirstBatchResume'];
type Attention = components['schemas']['FirstBatchAttention'];
type IntakeInput = components['schemas']['IntakeInputSummary'];

export type StopReason = Attention['reason'];
/** A batch's state (`checking`: every image made, the checks running, #197). */
export type FirstBatchState = FirstBatch['state'];
/** The step a batch is at or stopped at (`checks` since #197). */
export type ProgressStepKey = Attention['step'];
export type ProgressStepState = 'done' | 'active' | 'pending' | 'stopped';

/** The progress list; the check shows only when the batch stopped there. */
export const PROGRESS_STEPS: ProgressStepKey[] = [
  'reading',
  'understanding',
  'ideas',
  'images',
];

const STEP_OF_STATE: Partial<Record<FirstBatchState, ProgressStepKey>> = {
  reading: 'reading',
  understanding: 'understanding',
  ideas: 'ideas',
  generating: 'images',
};

const RUNNING: ReadonlyArray<FirstBatchState> = [
  'reading',
  'understanding',
  'ideas',
  'generating',
];

/** A batch's (or its summary's) state, including the ones S9 adds. */
export const stateOf = (batch: { state: string }) =>
  batch.state as FirstBatchState;

const stepOf = (attention: Attention | null | undefined) => attention?.step;

/** True while the batch still has a step on its way (nothing to do but wait). */
export function isRunning(batch: FirstBatch | undefined): boolean {
  return !!batch && RUNNING.includes(stateOf(batch));
}

/**
 * Every ad is made: the batch is done, or (S9) its ads are being checked and
 * the checks land on the ads page as they finish.
 */
export function adsMade(batch: { state: string } | null | undefined): boolean {
  const state = batch ? stateOf(batch) : null;
  return state === 'done' || state === 'checking';
}

/** The reading page's progress list, in order. */
export function progressSteps(
  batch: FirstBatch
): { key: ProgressStepKey; state: ProgressStepState }[] {
  if (adsMade(batch))
    return PROGRESS_STEPS.map((key) => ({ key, state: 'done' }));
  const stopped = batch.state === 'needs_attention' || batch.state === 'failed';
  const current =
    stepOf(batch.attention) ?? STEP_OF_STATE[stateOf(batch)] ?? 'reading';
  if (current === 'checks')
    return [
      ...PROGRESS_STEPS.map((key) => ({ key, state: 'done' as const })),
      { key: 'checks', state: stopped ? 'stopped' : 'active' },
    ];
  const at = PROGRESS_STEPS.indexOf(current);
  return PROGRESS_STEPS.map((key, index) => ({
    key,
    state:
      index < at
        ? 'done'
        : index > at
          ? 'pending'
          : stopped
            ? 'stopped'
            : 'active',
  }));
}

/**
 * What the user does about a stop:
 * - `choose_model`: the link lists several models; continue with one.
 * - `start_again`: the batch ended (the page could not be read, or what was
 *   read could not become a product); paste text or a screenshot instead.
 * - `add_material`: too little to carry two claims; add material, continue.
 * - `continue`: a refused draft, a failed step or the confirmed maximum;
 *   continuing confirms the new maximum.
 * - `uncertain`: a call's result is unknown; it is never sent again on its
 *   own, so there is a way on only once it has been reconciled.
 */
export type StopKind =
  | 'choose_model'
  | 'start_again'
  | 'add_material'
  | 'continue'
  | 'uncertain';

export interface StopView {
  kind: StopKind;
  reason: StopReason;
  step: ProgressStepKey;
  /** The models to choose from (`choose_model`). */
  variants: string[];
  /** Ads that did not come out, when the images step stopped. */
  failedAds: number;
  /** What continuing needs and costs; `null` when it cannot continue now. */
  resume: FirstBatchResume | null;
  /**
   * `uncertain` while making images: the generation runs whose existing
   * reconciliation settles them (#204). Planning and check calls have none.
   */
  runIds: string[];
}

const KIND_OF_REASON: Record<StopReason, StopKind> = {
  several_models: 'choose_model',
  page_unreadable: 'start_again',
  reading_failed: 'start_again',
  too_few_claims: 'add_material',
  over_budget: 'continue',
  ideas_refused: 'continue',
  step_failed: 'continue',
  uncertain: 'uncertain',
};

export function stopView(batch: FirstBatch): StopView | null {
  if (batch.state !== 'needs_attention' && batch.state !== 'failed')
    return null;
  const attention = batch.attention;
  const reason: StopReason = attention?.reason ?? 'step_failed';
  const ended = batch.state === 'failed';
  return {
    // An ended batch is never continued, whatever stopped it.
    kind: ended ? 'start_again' : KIND_OF_REASON[reason],
    reason,
    step: attention?.step ?? 'reading',
    variants: attention?.variants ?? [],
    failedAds:
      attention?.step === 'images' && reason === 'step_failed'
        ? (attention.combination_ids ?? []).length
        : 0,
    resume: ended ? null : batch.resume,
    runIds:
      reason === 'uncertain' && attention?.step === 'images'
        ? (attention.run_ids ?? [])
        : [],
  };
}

export type QuoteState =
  | { kind: 'loading' }
  | { kind: 'ready'; maximum: string; quoteHash: string }
  /** No OpenAI key in the workspace: the key row (Owner) or a hint. */
  | { kind: 'key_required' }
  /** Anything else that keeps the batch from starting, by its code. */
  | { kind: 'unavailable'; reason: string };

export function quoteState(quote: FirstBatchQuote | undefined): QuoteState {
  if (!quote) return { kind: 'loading' };
  if (quote.available)
    return {
      kind: 'ready',
      maximum: quote.maximum_cost,
      quoteHash: quote.quote_hash,
    };
  if (quote.unavailable_reason === 'openai_key_required')
    return { kind: 'key_required' };
  return { kind: 'unavailable', reason: quote.unavailable_reason || 'unknown' };
}

/**
 * A maximum as the button says it: US dollars, rounded up to the cent so the
 * label is never below what the click confirms.
 */
export function costLabel(locale: string, amount: string): string {
  const cents = Math.ceil(Number(amount) * 100 - 1e-9);
  return formatNumber(locale, Math.max(cents, 0) / 100, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/** True when an amount is more than nothing. */
export function costsMore(amount: string | null | undefined): boolean {
  return Number(amount ?? 0) > 0;
}

/**
 * Whether the product was read from uploaded images alone: then the facts'
 * source is the text the AI read from those images (design §5.2, D4).
 */
export function readFromImagesOnly(input: IntakeInput | undefined): boolean {
  return (
    !!input &&
    !input.url &&
    (input.text_chars ?? 0) === 0 &&
    (input.upload_count ?? 0) > 0
  );
}
