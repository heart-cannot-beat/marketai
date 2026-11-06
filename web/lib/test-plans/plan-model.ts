/**
 * Pure model of the guided ad test (front-flow contract §3.2 / §3.3,
 * Issue #165): the three questions and their defaults, the create / revise
 * payloads, the totals the page shows, and how a Run's items are mapped
 * back to the direction cards that produced them.
 *
 * Nothing here renders and nothing here reads ids into copy: the page shows
 * titles, counts and costs only (contract §1 principle 2).
 */
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { formatNumber } from '@/lib/i18n/format';

type Schemas = components['schemas'];

export type TestPlanDetail = Schemas['TestPlanDetail'];
export type TestPlanSummary = Schemas['TestPlanSummary'];
export type DirectionView = Schemas['DirectionView'];
export type FactorView = Schemas['FactorView'];
export type StageCounts = Schemas['StageCounts'];
export type Expansion = Schemas['Expansion'];
export type TestPlanRunView = Schemas['TestPlanRunView'];
export type GenerationDefaultsView = Schemas['GenerationDefaultsView'];
export type TestPlanCreateRequest = Schemas['TestPlanCreateRequest'];
export type TestPlanReviseRequest = Schemas['TestPlanReviseRequest'];
export type DirectionEdit = Schemas['DirectionEdit'];
export type DirectionAdd = Schemas['DirectionAdd'];

export type PlanGoal = 'explore' | 'optimize';
export type PlanScale = 'sample' | 'small' | 'full';
export type RunStage = 'sample' | 'full';

export const PLAN_SCALES: PlanScale[] = ['sample', 'small', 'full'];

/** Planner ceilings (contract §5.3), shown before the plan is drafted. */
export const SCALE_COMBINATION_CAP: Record<PlanScale, number | null> = {
  sample: null,
  small: 12,
  full: 32,
};

/** Variants per combination when the Workspace has no saved defaults
 * (contract §4: sample 1, small / full 2). */
export const FALLBACK_VARIANTS: Record<PlanScale, number> = {
  sample: 1,
  small: 2,
  full: 2,
};

// ---------------------------------------------------------------------------
// The three questions
// ---------------------------------------------------------------------------

export interface AudienceCardLite {
  who: string;
  why?: string;
  pain?: string;
}

export interface ThreeAnswers {
  goal: PlanGoal;
  /** Index of the product's audience card, or `null` for free text. */
  audienceCardIndex: number | null;
  audienceText: string;
  scale: PlanScale;
  seedAssetVersionIds: string[];
}

/** Every question answered by its default (contract §3.2). */
export function defaultAnswers(audienceCardCount: number): ThreeAnswers {
  return {
    goal: 'explore',
    audienceCardIndex: audienceCardCount > 0 ? 0 : null,
    audienceText: '',
    scale: 'sample',
    seedAssetVersionIds: [],
  };
}

/**
 * The `POST /test-plans` body. Seeds only travel with `optimize`; free
 * text replaces the card; an empty free text with no card leaves the
 * audience to the server (it falls back to the product's own audience).
 */
export function buildCreatePayload(
  productId: string,
  answers: ThreeAnswers
): TestPlanCreateRequest {
  const body: TestPlanCreateRequest = {
    product_id: productId,
    goal: answers.goal,
    scale: answers.scale,
    seed_asset_version_ids:
      answers.goal === 'optimize'
        ? answers.seedAssetVersionIds.slice(0, 10)
        : [],
  };
  const text = answers.audienceText.trim();
  if (answers.audienceCardIndex !== null) {
    body.audience_card_index = answers.audienceCardIndex;
  } else if (text) {
    body.audience_text = text.slice(0, 500);
  }
  return body;
}

/** The product's audience cards from its published Library content. */
export function audienceCardsFromContent(
  content: Record<string, unknown> | null | undefined
): AudienceCardLite[] {
  const raw = content?.audiences;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item) => {
    if (!item || typeof item !== 'object') return [];
    const card = item as Record<string, unknown>;
    const who = typeof card.who === 'string' ? card.who.trim() : '';
    if (!who) return [];
    return [
      {
        who,
        why: typeof card.why === 'string' ? card.why : '',
        pain: typeof card.pain === 'string' ? card.pain : '',
      },
    ];
  });
}

/** Rule of thumb before a plan exists: images per scale, from the
 * effective defaults' variants. `sample` is one image per direction. */
export function ruleOfThumbImages(
  scale: PlanScale,
  defaults: Pick<GenerationDefaultsView, 'variants'> | null | undefined
): {
  maxCombinations: number | null;
  variants: number;
  maxImages: number | null;
} {
  const variants =
    Number(defaults?.variants?.[scale]) || FALLBACK_VARIANTS[scale];
  const cap = SCALE_COMBINATION_CAP[scale];
  return {
    maxCombinations: cap,
    variants,
    maxImages: cap === null ? null : cap * variants,
  };
}

// ---------------------------------------------------------------------------
// Revise payloads
// ---------------------------------------------------------------------------

export interface DirectionDraft {
  title: string;
  whatItTests: string;
  sceneDescription: string;
  headline: string;
  primaryText: string;
  /** factor key -> value ids */
  factorValues: Record<string, string[]>;
}

export function draftFromDirection(direction: DirectionView): DirectionDraft {
  return {
    title: direction.title ?? '',
    whatItTests: direction.what_it_tests ?? '',
    sceneDescription: direction.scene_description ?? '',
    headline: direction.copy?.headline ?? '',
    primaryText: direction.copy?.primary_text ?? '',
    factorValues: Object.fromEntries(
      Object.entries(direction.factor_values ?? {}).map(([key, ids]) => [
        key,
        [...ids],
      ])
    ),
  };
}

function sameIds(a: string[] | undefined, b: string[] | undefined): boolean {
  const left = [...(a ?? [])].sort();
  const right = [...(b ?? [])].sort();
  return left.length === right.length && left.every((id, i) => id === right[i]);
}

/**
 * Only the fields the user changed, so the audit records what was edited
 * and an untouched field never round-trips through server-side cleaning.
 * `null` when nothing changed.
 */
export function buildDirectionEdit(
  direction: DirectionView,
  draft: DirectionDraft
): DirectionEdit | null {
  const edit: DirectionEdit = { direction_id: direction.direction_id };
  let changed = false;
  if (draft.title.trim() !== (direction.title ?? '').trim()) {
    edit.title = draft.title.trim();
    changed = true;
  }
  if (draft.whatItTests.trim() !== (direction.what_it_tests ?? '').trim()) {
    edit.what_it_tests = draft.whatItTests.trim();
    changed = true;
  }
  if (
    draft.sceneDescription.trim() !== (direction.scene_description ?? '').trim()
  ) {
    edit.scene_description = draft.sceneDescription.trim();
    changed = true;
  }
  const copy: { headline?: string; primary_text?: string } = {};
  if (draft.headline.trim() !== (direction.copy?.headline ?? '').trim()) {
    copy.headline = draft.headline.trim();
  }
  if (
    draft.primaryText.trim() !== (direction.copy?.primary_text ?? '').trim()
  ) {
    copy.primary_text = draft.primaryText.trim();
  }
  if (Object.keys(copy).length > 0) {
    edit.copy = copy;
    changed = true;
  }
  const factorChanges: Record<string, { value_id: string }[]> = {};
  for (const [key, ids] of Object.entries(draft.factorValues)) {
    if (!sameIds(ids, direction.factor_values?.[key])) {
      factorChanges[key] = ids.map((id) => ({ value_id: id }));
    }
  }
  if (Object.keys(factorChanges).length > 0) {
    edit.factor_values = factorChanges;
    changed = true;
  }
  return changed ? edit : null;
}

/** "我的想法": a title and a scene; the server fills the factor values
 * with each factor's first value. */
export function buildDirectionAdd(idea: {
  title: string;
  sceneDescription: string;
}): DirectionAdd | null {
  const title = idea.title.trim();
  if (!title) return null;
  const add: DirectionAdd = { title };
  const scene = idea.sceneDescription.trim();
  if (scene) add.scene_description = scene;
  return add;
}

// ---------------------------------------------------------------------------
// Stages, runs and totals
// ---------------------------------------------------------------------------

const ACTIVE_RUN_STATUSES = new Set([
  'pending',
  'queued',
  'running',
  'qc_pending',
]);

export function stageRun(
  plan: Pick<TestPlanDetail, 'runs'> | null | undefined,
  stage: RunStage
): TestPlanRunView | undefined {
  return (plan?.runs ?? []).find((run) => run.stage === stage);
}

/** A stage's Run is still producing images (or still being submitted). */
export function isRunActive(run: TestPlanRunView | undefined): boolean {
  if (!run) return false;
  if (run.status === 'materializing') return true;
  if (run.status !== 'submitted') return false;
  return run.run_status ? ACTIVE_RUN_STATUSES.has(run.run_status) : true;
}

export function isRunSettled(run: TestPlanRunView | undefined): boolean {
  return Boolean(
    run && run.status === 'submitted' && run.run_id && !isRunActive(run)
  );
}

/** The plan needs polling: drafting, or a stage Run still in flight. */
export function planNeedsPolling(plan: TestPlanDetail | null): boolean {
  if (!plan) return false;
  if (plan.status === 'drafting') return true;
  return (plan.runs ?? []).some((run) => isRunActive(run));
}

export type PlanPhase =
  | 'drafting'
  | 'failed'
  | 'review' // ready, nothing run yet
  | 'sampling'
  | 'sampled'
  | 'expanding'
  | 'done';

export function planPhase(plan: TestPlanDetail): PlanPhase {
  if (plan.status === 'drafting') return 'drafting';
  if (plan.status === 'failed') return 'failed';
  const sample = stageRun(plan, 'sample');
  const full = stageRun(plan, 'full');
  if (full && full.status !== 'failed') {
    return isRunActive(full) ? 'expanding' : 'done';
  }
  if (sample && sample.status !== 'failed') {
    return isRunActive(sample) ? 'sampling' : 'sampled';
  }
  return 'review';
}

export function activeDirections(plan: TestPlanDetail): DirectionView[] {
  return (plan.directions ?? []).filter((direction) => !direction.removed);
}

/** Images this direction contributes to a stage. */
export function directionImages(
  direction: DirectionView,
  stage: StageCounts | null | undefined
): number {
  if (!stage || direction.removed) return 0;
  const combinations = Number(direction.selected_in?.[stage.scale] ?? 0);
  return combinations * (stage.variants || 1);
}

/**
 * Issue #193: true when "完整测试" would measure nothing the sample does not.
 * Before sampling, that is when the full stage plans no more combinations
 * than the sample (every active direction spans one combination -- a
 * single-valued AI draft, or a user who edited a direction back to single
 * values); after sampling, when the expansion has no new combination to add.
 * The page then explains why and links to editing a direction instead of
 * only greying the action out.
 */
export function fullTestAddsNothing(
  plan: TestPlanDetail,
  phase: PlanPhase
): boolean {
  if (activeDirections(plan).length === 0) return false;
  if (phase === 'review') {
    const sample = plan.stages?.sample;
    const full = plan.stages?.full;
    if (!sample || !full) return false;
    return full.combinations <= sample.combinations;
  }
  if (phase === 'sampled') {
    const expansion = plan.expansion;
    if (!expansion || expansion.sample_run_in_progress) return false;
    return expansion.combinations === 0;
  }
  return false;
}

/** "$0.32" (en) / "US$0.32" (zh-CN), or 不计费 for a zero quote (Mock). */
export function formatCost(
  cost: { amount: string; currency: string } | null | undefined,
  locale: string
): { free: boolean; text: string } {
  const amount = Number(cost?.amount ?? 0);
  if (!cost || !Number.isFinite(amount) || amount <= 0) {
    return { free: true, text: '' };
  }
  try {
    return {
      free: false,
      text: formatNumber(locale, amount, {
        style: 'currency',
        currency: cost.currency || 'USD',
        minimumFractionDigits: 2,
        maximumFractionDigits: 4,
      }),
    };
  } catch {
    return { free: false, text: `${cost.currency} ${cost.amount}` };
  }
}

// ---------------------------------------------------------------------------
// Run items -> direction cards
// ---------------------------------------------------------------------------

export interface PlanRunItem {
  id: string;
  ordinal: number;
  state: string;
  combination_fingerprint?: string | null;
  variant_index?: number;
  tags?: Record<string, unknown>;
  bound_asset_version_id?: string | null;
  thumbnail?: Schemas['AssetThumbnailLink'] | null;
}

const DIRECTION_DIMENSION = 'direction';

/** `"<template entry>:<dimension>"` or `"<dimension>"` -> `"<dimension>"`. */
function shortDimension(key: string): string {
  const index = key.lastIndexOf(':');
  return index >= 0 ? key.slice(index + 1) : key;
}

function dimensionsFromFingerprint(
  fingerprint: string | null | undefined
): Record<string, string> {
  // `app.plans.runs.generation_fingerprint`:
  // "<entry>:<dimension>=<value>" pairs, sorted, joined with ":".
  const out: Record<string, string> = {};
  if (!fingerprint) return out;
  const pattern = /([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)=([^:]*)/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(fingerprint)) !== null) {
    out[match[2]] = match[3];
  }
  return out;
}

/** An item's combination as `{ dimension: value }`, from its tags (the
 * frozen recipe) or, failing that, its fingerprint. */
export function itemDimensions(
  item: CombinationCarrier
): Record<string, string> {
  const fromTags: Record<string, string> = {};
  for (const [key, value] of Object.entries(item.tags ?? {})) {
    if (value === null || value === undefined || typeof value === 'object') {
      continue;
    }
    fromTags[shortDimension(key)] = String(value);
  }
  if (fromTags[DIRECTION_DIMENSION]) return fromTags;
  const parsed = dimensionsFromFingerprint(item.combination_fingerprint);
  return parsed[DIRECTION_DIMENSION] ? parsed : fromTags;
}

/** The plan-local signature (`direction=..|factor=value`, factors sorted)
 * — the same form as `DirectionView.combination_signatures`. */
export function planSignature(
  dimensions: Record<string, string>
): string | null {
  const direction = dimensions[DIRECTION_DIMENSION];
  if (!direction) return null;
  const parts = [`${DIRECTION_DIMENSION}=${direction}`];
  for (const key of Object.keys(dimensions).sort()) {
    if (key === DIRECTION_DIMENSION) continue;
    parts.push(`${key}=${dimensions[key]}`);
  }
  return parts.join('|');
}

export interface ItemsByDirection {
  byDirection: Map<string, PlanRunItem[]>;
  unmatched: PlanRunItem[];
}

/** Anything that carries a combination: a Run item, or an AssetVersion
 * (whose `tags` are its producing item's frozen recipe tags). */
export type CombinationCarrier = Pick<
  PlanRunItem,
  'tags' | 'combination_fingerprint'
>;

/**
 * The direction a combination belongs to: by exact plan signature first
 * (the combination the planner selected), then by its direction value (a
 * direction whose factor values were edited after the Run still owns its
 * images). `null` when neither matches a direction of this plan.
 */
export function directionMatcher(
  directions: DirectionView[]
): (carrier: CombinationCarrier) => string | null {
  const bySignature = new Map<string, string>();
  const known = new Set<string>();
  for (const direction of directions) {
    known.add(direction.direction_id);
    for (const signature of direction.combination_signatures ?? []) {
      bySignature.set(signature, direction.direction_id);
    }
  }
  return (carrier) => {
    const dimensions = itemDimensions(carrier);
    const signature = planSignature(dimensions);
    const owner = signature ? bySignature.get(signature) : undefined;
    if (owner) return owner;
    const value = dimensions[DIRECTION_DIMENSION] ?? '';
    return known.has(value) ? value : null;
  };
}

/** Groups Run items under the direction card they belong to
 * (`directionMatcher`); items no direction claims are `unmatched`. */
export function groupItemsByDirection(
  directions: DirectionView[],
  items: PlanRunItem[]
): ItemsByDirection {
  const match = directionMatcher(directions);
  const byDirection = new Map<string, PlanRunItem[]>();
  const unmatched: PlanRunItem[] = [];
  const sorted = [...items].sort((a, b) => a.ordinal - b.ordinal);
  for (const item of sorted) {
    const owner = match(item);
    if (!owner) {
      unmatched.push(item);
      continue;
    }
    const list = byDirection.get(owner) ?? [];
    list.push(item);
    byDirection.set(owner, list);
  }
  return { byDirection, unmatched };
}

// ---------------------------------------------------------------------------
// Problems
// ---------------------------------------------------------------------------

/** Codes the page explains in its own words (the server's message can
 * carry internal ids, so it is never shown as is). */
export const KNOWN_PROBLEM_CODES = new Set([
  'revision_conflict',
  'precondition_required',
  'test_plan_locked',
  'test_plan_not_ready',
  'test_plan_run_in_progress',
  'sample_run_in_progress',
  'nothing_to_generate',
  'credential_required',
  'guardrail_exceeded',
  'spend_guardrail_exceeded',
  'mode_unavailable',
  'model_unavailable',
  'entitlement_unavailable',
  'no_active_directions',
  'too_many_directions',
  'direction_title_missing',
  'direction_removed',
  'direction_not_found',
  'nothing_to_revise',
  'product_not_published',
  'product_archived',
  'product_not_found',
  'audience_card_out_of_range',
  'seed_assets_required',
  'too_many_seed_assets',
  'seed_asset_not_found',
  'test_plan_not_found',
  'legacy_test_plans_frozen',
  'template_invalid',
  'template_publish_failed',
  'too_many_factor_values',
  'direction_factor_missing',
  'generation_defaults_owner_only',
  'provider_mode_mismatch',
  'aspect_ratio_unsupported',
  'variants_invalid',
  'provider_unavailable',
]);

/** Problems after which the page must reload the plan before going on. */
export const REFETCH_PROBLEM_CODES = new Set([
  'revision_conflict',
  'precondition_required',
  'test_plan_locked',
  'test_plan_not_ready',
  'test_plan_run_in_progress',
  'sample_run_in_progress',
  'nothing_to_generate',
  'direction_removed',
  'direction_not_found',
]);

export function problemKey(code: string | null | undefined): string {
  return code && KNOWN_PROBLEM_CODES.has(code)
    ? `problem-${code}`
    : 'problem-unknown';
}
