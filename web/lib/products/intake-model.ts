/**
 * Pure model behind the "设置产品" front flow (Issue #162, front-flow
 * contract §3.1): how a paste becomes an intake request, how intake status
 * maps to the three visible steps, how failures map to guidance, and how
 * the editable product card becomes the confirm payload.
 *
 * Kept free of React and of the gateway so every rule here is unit-tested
 * directly; components and hooks only wire it up.
 */
import type { components } from '@/lib/domain-client/generated/domain-v1';

export type IntakeDetail = components['schemas']['ProductIntakeDetail'];
export type IntakeCard = components['schemas']['ProductCardDraft'];
export type IntakeImage = components['schemas']['IntakeImage'];
export type IntakeVariant = components['schemas']['ModelVariant'];
type MustMatchView = components['schemas']['MustMatchView'];
export type ConfirmRequest =
  components['schemas']['ProductIntakeConfirmRequest'];
export type CreateIntakeRequest =
  components['schemas']['ProductIntakeCreateRequest'];

// ---------------------------------------------------------------------------
// Paste box input
// ---------------------------------------------------------------------------

export const MAX_INTAKE_IMAGES = 12;
export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
export const MAX_TEXT_CHARS = 20_000;
export const ACCEPTED_IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp'];

// A link ends at whitespace, quotes, brackets or Chinese full-width
// punctuation (，。；！？、）), which pasted product copy often runs straight into.
const URL_PATTERN =
  /https?:\/\/[^\s<>"'\uff0c\u3002\uff1b\uff01\uff1f\u3001\uff09)]+/i;

export interface PasteParts {
  /** The first http(s) link found in the pasted text, if any. */
  url: string | null;
  /** Whatever text remains once the link is taken out (trimmed). */
  text: string | null;
}

/**
 * One paste box takes a link, a paragraph or both: the first http(s) link
 * becomes `url`, the rest (if anything is left) becomes `text`.
 */
export function splitPaste(raw: string): PasteParts {
  const value = raw.trim();
  if (!value) return { url: null, text: null };
  const match = URL_PATTERN.exec(value);
  if (!match) return { url: null, text: value };
  const url = match[0].replace(/[.,;:!?]+$/, '');
  const rest = (
    value.slice(0, match.index) + value.slice(match.index + match[0].length)
  )
    .replace(/\s+/g, ' ')
    .trim();
  return { url, text: rest.length > 0 ? value : null };
}

/** The host of a pasted link, for the "识别到链接" hint. */
export function linkHost(url: string | null): string | null {
  if (!url) return null;
  try {
    return new URL(url).host;
  } catch {
    return null;
  }
}

export type ImageRejection = 'type' | 'size' | 'count';

/** Which of the dropped files the intake can take, and why others were refused. */
export function acceptImages(
  existing: number,
  files: File[]
): {
  accepted: File[];
  rejected: Array<{ file: File; reason: ImageRejection }>;
} {
  const accepted: File[] = [];
  const rejected: Array<{ file: File; reason: ImageRejection }> = [];
  for (const file of files) {
    if (!ACCEPTED_IMAGE_TYPES.includes(file.type)) {
      rejected.push({ file, reason: 'type' });
    } else if (file.size > MAX_IMAGE_BYTES) {
      rejected.push({ file, reason: 'size' });
    } else if (existing + accepted.length >= MAX_INTAKE_IMAGES) {
      rejected.push({ file, reason: 'count' });
    } else {
      accepted.push(file);
    }
  }
  return { accepted, rejected };
}

/** The create body, or `null` when the paste box holds nothing usable. */
export function buildIntakeRequest(
  parts: PasteParts,
  uploadIntentIds: string[]
): CreateIntakeRequest | null {
  const body: CreateIntakeRequest = {};
  if (parts.url) body.url = parts.url;
  if (parts.text) body.text = parts.text.slice(0, MAX_TEXT_CHARS);
  if (uploadIntentIds.length > 0) body.upload_intent_ids = uploadIntentIds;
  return Object.keys(body).length > 0 ? body : null;
}

// ---------------------------------------------------------------------------
// Status → steps
// ---------------------------------------------------------------------------

export const IN_FLIGHT_STATUSES = [
  'queued',
  'fetching',
  'extracting',
  'preparing_images',
] as const;
export const FAILED_STATUSES = [
  'fetch_failed',
  'extraction_failed',
  'failed',
] as const;

export type IntakePhase = 'working' | 'ready' | 'confirmed' | 'failed';

export function intakePhase(status: string): IntakePhase {
  if (status === 'ready') return 'ready';
  if (status === 'confirmed') return 'confirmed';
  if ((FAILED_STATUSES as readonly string[]).includes(status)) return 'failed';
  return 'working';
}

export type StepKey = 'fetch' | 'extract' | 'images';
export type StepState = 'pending' | 'active' | 'done' | 'skipped' | 'failed';

const STEP_ORDER: StepKey[] = ['fetch', 'extract', 'images'];
const STATUS_STEP: Record<string, StepKey> = {
  fetching: 'fetch',
  extracting: 'extract',
  preparing_images: 'images',
};

/**
 * The three visible steps (抓取页面 / 识别信息 / 处理图片). Fetching is
 * skipped when nothing was linked; a failure marks the step it happened in.
 */
export function stepStates(
  detail: Pick<IntakeDetail, 'status' | 'current_step' | 'failure'> & {
    input?: { url?: string | null } | null;
  }
): Record<StepKey, StepState> {
  const hasUrl = Boolean(detail.input?.url);
  const phase = intakePhase(detail.status);
  const result = {} as Record<StepKey, StepState>;

  if (phase === 'ready' || phase === 'confirmed') {
    for (const key of STEP_ORDER) result[key] = 'done';
    if (!hasUrl) result.fetch = 'skipped';
    return result;
  }

  let activeIndex = -1;
  if (phase === 'failed') {
    const failedStep =
      normalizeStep(detail.failure?.step) ??
      normalizeStep(detail.current_step) ??
      (detail.status === 'fetch_failed' ? 'fetch' : 'extract');
    activeIndex = STEP_ORDER.indexOf(failedStep);
  } else {
    const step =
      STATUS_STEP[detail.status] ?? normalizeStep(detail.current_step);
    activeIndex = step ? STEP_ORDER.indexOf(step) : -1;
  }

  STEP_ORDER.forEach((key, index) => {
    if (index < activeIndex) result[key] = 'done';
    else if (index === activeIndex)
      result[key] = phase === 'failed' ? 'failed' : 'active';
    else result[key] = 'pending';
  });
  if (!hasUrl && result.fetch !== 'failed') result.fetch = 'skipped';
  return result;
}

function normalizeStep(step: string | null | undefined): StepKey | null {
  if (!step) return null;
  if (step === 'fetch' || step === 'fetching') return 'fetch';
  if (step === 'extract' || step === 'extracting' || step === 'extraction')
    return 'extract';
  if (step === 'images' || step === 'preparing_images' || step === 'image')
    return 'images';
  return null;
}

// ---------------------------------------------------------------------------
// Polling backoff
// ---------------------------------------------------------------------------

export const POLL_INITIAL_MS = 1000;
export const POLL_MAX_MS = 5000;
export const POLL_FACTOR = 1.5;
/** After this long the page stops polling and offers a manual refresh. */
export const POLL_GIVE_UP_MS = 5 * 60_000;

export function nextPollDelay(previousMs: number | null): number {
  if (previousMs === null) return POLL_INITIAL_MS;
  return Math.min(POLL_MAX_MS, Math.round(previousMs * POLL_FACTOR));
}

// ---------------------------------------------------------------------------
// Failure guidance
// ---------------------------------------------------------------------------

export type SuggestedAction =
  | 'paste_text_or_screenshot'
  | 'check_link'
  | 'retry'
  | 'add_more_detail'
  | 'bind_openai_key'
  | 'adjust_spend_guardrail';

const KNOWN_ACTIONS: SuggestedAction[] = [
  'paste_text_or_screenshot',
  'check_link',
  'retry',
  'add_more_detail',
  'bind_openai_key',
  'adjust_spend_guardrail',
];

export function suggestedAction(
  failure: IntakeDetail['failure'] | undefined | null
): SuggestedAction {
  const action = failure?.suggested_action as SuggestedAction | undefined;
  if (action && KNOWN_ACTIONS.includes(action)) return action;
  return failure?.retryable ? 'retry' : 'paste_text_or_screenshot';
}

/** Whether the guidance offers the inline "add text / screenshots" box. */
export function offersSupplement(action: SuggestedAction): boolean {
  return (
    action === 'paste_text_or_screenshot' ||
    action === 'add_more_detail' ||
    action === 'check_link'
  );
}

// ---------------------------------------------------------------------------
// Editable card
// ---------------------------------------------------------------------------

export type MustMatchType =
  | 'color'
  | 'logo'
  | 'visual'
  | 'material'
  | 'text'
  | 'other';
export const MUST_MATCH_TYPES: MustMatchType[] = [
  'color',
  'logo',
  'visual',
  'material',
  'text',
  'other',
];
export type Severity = 'blocking' | 'warning';

export interface SpecRow {
  key: string;
  value: string;
}
export interface AudienceDraft {
  who: string;
  why: string;
  pain: string;
}
export interface MustMatchDraft {
  /** Present for AI-proposed items; kept so the server keeps `proposed_by`. */
  id: string | null;
  type: MustMatchType;
  description: string;
  severity: Severity;
  proposedByAi: boolean;
}

export interface CardDraft {
  name: string;
  description: string;
  specs: SpecRow[];
  sellingPoints: string[];
  audiences: AudienceDraft[];
  primaryAudience: number;
  mustMatch: MustMatchDraft[];
  /** Selectable candidate images the user keeps, in display order. */
  includedImageIds: string[];
  mainImageId: string | null;
  selectedVariant: string | null;
}

export const MAX_SELLING_POINTS = 10;
export const MAX_AUDIENCES = 5;
export const MAX_MUST_MATCH = 20;
export const MAX_BOUND_IMAGES = 8;

export function selectableImages(card: IntakeCard): IntakeImage[] {
  return (card.images ?? [])
    .filter((image) => image.selectable)
    .sort((a, b) => (a.rank ?? 999) - (b.rank ?? 999));
}

function asMustMatchType(value: string | null | undefined): MustMatchType {
  return MUST_MATCH_TYPES.includes(value as MustMatchType)
    ? (value as MustMatchType)
    : 'visual';
}

function mustMatchDraftFrom(item: MustMatchView): MustMatchDraft {
  return {
    id: item.id ?? null,
    type: asMustMatchType(item.type),
    description: item.description ?? '',
    severity: item.severity === 'warning' ? 'warning' : 'blocking',
    proposedByAi: item.proposed_by === 'ai',
  };
}

// ---------------------------------------------------------------------------
// Variants (Issue #192): models, sizes, powers and colour editions
// ---------------------------------------------------------------------------

export function findVariant(
  card: IntakeCard,
  label: string | null | undefined
): IntakeVariant | null {
  if (!label) return null;
  return (card.model_variants ?? []).find((v) => v.label === label) ?? null;
}

/** The variant's own images among the selectable ones, best ranked first. */
export function variantImages(
  card: IntakeCard,
  variant: IntakeVariant
): IntakeImage[] {
  const own = new Set(variant.image_ids ?? []);
  return selectableImages(card).filter((image) => own.has(image.image_id));
}

/** Whether the card ties any image to a variant (shows "通用" / 款式 tags). */
export function hasVariantImages(card: IntakeCard): boolean {
  return (card.model_variants ?? []).some(
    (variant) => (variant.image_ids ?? []).length > 0
  );
}

/** What the variant changes, for its choice card: specs then appearance.
 * `formatSpec` writes one spec in the interface language. */
export function variantDifferences(
  variant: IntakeVariant,
  formatSpec: (key: string, value: string) => string = (key, value) =>
    `${key}: ${value}`
): string[] {
  return [
    ...Object.entries(variant.specs ?? {}).map(([key, value]) =>
      formatSpec(key, value)
    ),
    ...(variant.appearance ?? []).map((item) => item.description),
  ].filter((text) => text.trim().length > 0);
}

function variantDefaultImages(
  card: IntakeCard,
  variant: IntakeVariant
): string[] {
  const selectable = selectableImages(card).map((image) => image.image_id);
  if (variant.default_image_ids) {
    return variant.default_image_ids
      .filter((id) => selectable.includes(id))
      .slice(0, MAX_BOUND_IMAGES);
  }
  const others = new Set(
    (card.model_variants ?? [])
      .filter((other) => other.label !== variant.label)
      .flatMap((other) => other.image_ids ?? [])
  );
  return selectable.filter((id) => !others.has(id)).slice(0, MAX_BOUND_IMAGES);
}

/**
 * Switch the card to one variant (front-flow contract §3.1, Issue #192):
 *
 * - images: preselect the variant's own and the general images; another
 *   variant's images start unticked (the user can tick them back);
 * - must-match: drop the previous variant's appearance items, bring back the
 *   shared items it had replaced, replace the shared items the new variant
 *   supersedes (its own colour instead of "all colour options") and add its
 *   appearance;
 * - specs: the variant's differing specs replace or join the shared ones.
 *
 * Items and rows the user added stay; switching back and forth is stable.
 */
export function selectVariant(
  draft: CardDraft,
  card: IntakeCard,
  label: string
): CardDraft {
  const next = findVariant(card, label);
  if (!next) return draft;
  const previous = findVariant(card, draft.selectedVariant);
  if (previous && previous.label === next.label) return draft;

  const shared = card.specs ?? {};
  const previousSpecs = previous?.specs ?? {};
  const nextSpecs = next.specs ?? {};
  const specs: SpecRow[] = draft.specs.flatMap((row) => {
    if (row.key in nextSpecs)
      return [{ key: row.key, value: nextSpecs[row.key] }];
    if (row.key in previousSpecs) {
      return row.key in shared
        ? [{ key: row.key, value: shared[row.key] }]
        : [];
    }
    return [row];
  });
  for (const [key, value] of Object.entries(nextSpecs)) {
    if (!specs.some((row) => row.key === key)) specs.push({ key, value });
  }

  const previousAppearance = new Set(
    (previous?.appearance ?? []).map((item) => item.id).filter(Boolean)
  );
  const nextReplaced = new Set(next.replaces_must_match_ids ?? []);
  let mustMatch = draft.mustMatch.filter(
    (item) => !(item.id && previousAppearance.has(item.id))
  );
  const restored = (previous?.replaces_must_match_ids ?? [])
    .filter(
      (id) => !nextReplaced.has(id) && !mustMatch.some((item) => item.id === id)
    )
    .map((id) => (card.must_match ?? []).find((item) => item.id === id))
    .filter((item): item is MustMatchView => Boolean(item))
    .map(mustMatchDraftFrom);
  mustMatch = [...restored, ...mustMatch].filter(
    (item) => !(item.id && nextReplaced.has(item.id))
  );
  for (const item of next.appearance ?? []) {
    if (!item.id || !mustMatch.some((existing) => existing.id === item.id))
      mustMatch.push(mustMatchDraftFrom(item));
  }

  const included = variantDefaultImages(card, next);
  const mainImageId =
    draft.mainImageId && included.includes(draft.mainImageId)
      ? draft.mainImageId
      : (included[0] ?? null);

  return {
    ...draft,
    specs,
    mustMatch: mustMatch.slice(0, MAX_MUST_MATCH),
    includedImageIds: included,
    mainImageId,
    selectedVariant: next.label,
  };
}

/** The AI's draft as the editor's starting state. */
export function cardDraftFrom(card: IntakeCard): CardDraft {
  const selectable = selectableImages(card).map((image) => image.image_id);
  const defaults = (card.default_image_ids ?? []).filter((id) =>
    selectable.includes(id)
  );
  const included = (defaults.length > 0 ? defaults : selectable).slice(
    0,
    MAX_BOUND_IMAGES
  );
  const variants = card.model_variants ?? [];
  const base: CardDraft = {
    name: card.name ?? '',
    description: card.description ?? '',
    specs: Object.entries(card.specs ?? {}).map(([key, value]) => ({
      key,
      value,
    })),
    sellingPoints: [...(card.selling_points ?? [])],
    audiences: (card.audiences ?? []).map((audience) => ({
      who: audience.who ?? '',
      why: audience.why ?? '',
      pain: audience.pain ?? '',
    })),
    primaryAudience: 0,
    mustMatch: (card.must_match ?? []).map(mustMatchDraftFrom),
    includedImageIds: included,
    mainImageId: included[0] ?? null,
    selectedVariant: null,
  };
  // One variant is chosen for the user; several wait for "选择款式".
  if (!card.requires_variant_selection && variants.length === 1) {
    return selectVariant(base, card, variants[0].label);
  }
  return base;
}

/** Confirm stays unavailable until a variant is chosen on a multi-variant card. */
export function needsVariantChoice(
  draft: CardDraft,
  card: IntakeCard
): boolean {
  return Boolean(card.requires_variant_selection) && !draft.selectedVariant;
}

export type CardProblem =
  | 'name_required'
  | 'description_required'
  | 'variant_required'
  | 'audience_who_required'
  | 'must_match_description_required'
  | 'too_many_selling_points'
  | 'too_many_audiences'
  | 'too_many_must_match';

/** Everything that would make the server refuse the confirm, in UI terms. */
export function validateCard(
  draft: CardDraft,
  card: IntakeCard
): CardProblem[] {
  const problems: CardProblem[] = [];
  if (!draft.name.trim()) problems.push('name_required');
  if (!draft.description.trim()) problems.push('description_required');
  if (card.requires_variant_selection && !draft.selectedVariant)
    problems.push('variant_required');
  if (draft.audiences.some((audience) => !audience.who.trim()))
    problems.push('audience_who_required');
  if (draft.mustMatch.some((item) => !item.description.trim()))
    problems.push('must_match_description_required');
  if (cleanList(draft.sellingPoints).length > MAX_SELLING_POINTS)
    problems.push('too_many_selling_points');
  if (draft.audiences.length > MAX_AUDIENCES)
    problems.push('too_many_audiences');
  if (draft.mustMatch.length > MAX_MUST_MATCH)
    problems.push('too_many_must_match');
  return problems;
}

function cleanList(items: string[]): string[] {
  return items.map((item) => item.trim()).filter((item) => item.length > 0);
}

/**
 * The confirm body. Every card field is sent explicitly (an omitted field
 * would silently keep the AI's value); the main image goes first in
 * `image_ids` so it becomes the first-priority reference binding.
 */
export function buildConfirmRequest(
  draft: CardDraft,
  card: IntakeCard
): ConfirmRequest {
  const specs: Record<string, string> = {};
  for (const row of draft.specs) {
    const key = row.key.trim();
    const value = row.value.trim();
    if (key && value) specs[key] = value;
  }
  const audiences = draft.audiences.map((audience) => ({
    who: audience.who.trim(),
    why: audience.why.trim(),
    pain: audience.pain.trim(),
  }));
  const body: ConfirmRequest = {
    card: {
      name: draft.name.trim(),
      description: draft.description.trim(),
      specs,
      selling_points: cleanList(draft.sellingPoints),
      audiences,
      must_match: draft.mustMatch.map((item) => ({
        ...(item.id ? { id: item.id } : {}),
        type: item.type,
        description: item.description.trim(),
        severity: item.severity,
      })),
    },
  };
  if (audiences.length > 0 && body.card) {
    body.card.primary_audience_index = Math.min(
      Math.max(draft.primaryAudience, 0),
      audiences.length - 1
    );
  }
  if (draft.selectedVariant) body.selected_variant = draft.selectedVariant;

  const selectable = new Set(
    selectableImages(card).map((image) => image.image_id)
  );
  if (selectable.size > 0) {
    const kept = draft.includedImageIds.filter((id) => selectable.has(id));
    const main =
      draft.mainImageId && kept.includes(draft.mainImageId)
        ? draft.mainImageId
        : null;
    const ordered = main ? [main, ...kept.filter((id) => id !== main)] : kept;
    body.image_ids = ordered.slice(0, MAX_BOUND_IMAGES);
  }
  return body;
}
