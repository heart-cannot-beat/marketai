import type { components } from '@/lib/domain-client/generated/domain-v1';

/** One external creative as `get_external_creative` returns it (Issue #168). */
export type ExternalCreative = components['schemas']['ExternalCreativeDetail'];
export type ExternalConfirmRequest =
  components['schemas']['ExternalCreativeConfirmRequest'];
export type VocabularyFactor = components['schemas']['VocabularyFactor'];

/** Who made the images -- the rights declaration's first question. */
export const MADE_BY = [
  'self',
  'designer',
  'agency',
  'creator',
  'ai_tool',
  'other',
] as const;
export type MadeBy = (typeof MADE_BY)[number];

export const ACCEPTED_TYPES = ['image/png', 'image/jpeg', 'image/webp'];
export const MAX_FILES = 10;
export const MAX_FILE_BYTES = 20 * 1024 * 1024;

/** Statuses after which the Worker has nothing left to do. */
const SETTLED = new Set([
  'ready',
  'tagging_failed',
  'confirmed',
  'rejected',
  'failed',
]);
const CONFIRMABLE = new Set(['ready', 'tagging_failed', 'confirmed']);

export function isSettled(status: string): boolean {
  return SETTLED.has(status);
}

export function isConfirmable(creative: ExternalCreative): boolean {
  return CONFIRMABLE.has(creative.status) && Boolean(creative.asset_version_id);
}

/** `null` = "none of these" for that factor. */
export type TagChoices = Record<string, string | null>;

export interface DirectionChoiceValue {
  plan_id: string;
  direction_key: string;
}

/**
 * The chips a review card starts with: the latest confirmation when there
 * is one, else every AI suggestion at or above the server's pre-select
 * confidence (a weaker guess is shown, never pre-selected).
 */
export function initialChoices(creative: ExternalCreative): TagChoices {
  const choices: TagChoices = {};
  const confirmed = creative.confirmation?.tags;
  if (confirmed && confirmed.length > 0) {
    for (const tag of confirmed) choices[tag.factor_id] = tag.value_id ?? null;
    return choices;
  }
  const threshold = creative.suggestion?.preselect_confidence ?? 0.5;
  for (const factor of creative.vocabulary.factors ?? [])
    choices[factor.factor_id] = null;
  for (const item of creative.suggestion?.factors ?? []) {
    if (item.value_id && item.confidence >= threshold) {
      choices[item.factor_id] = item.value_id;
    }
  }
  return choices;
}

export function initialDirection(
  creative: ExternalCreative
): DirectionChoiceValue | null {
  const direction = creative.confirmation?.direction;
  if (direction?.plan_id && direction.direction_key) {
    return {
      plan_id: direction.plan_id,
      direction_key: direction.direction_key,
    };
  }
  return null;
}

export function buildConfirmBody(
  creative: ExternalCreative,
  choices: TagChoices,
  direction: DirectionChoiceValue | null
): ExternalConfirmRequest {
  return {
    tags: (creative.vocabulary.factors ?? []).map((factor) => ({
      factor_id: factor.factor_id,
      value_id: choices[factor.factor_id] ?? null,
    })),
    direction,
    approve: true,
  };
}

export function directionKey(direction: DirectionChoiceValue | null): string {
  return direction ? `${direction.plan_id}::${direction.direction_key}` : '';
}

export function parseDirectionKey(value: string): DirectionChoiceValue | null {
  const [plan_id, direction_key] = value.split('::');
  return plan_id && direction_key ? { plan_id, direction_key } : null;
}

export function suggestionFor(creative: ExternalCreative, factorId: string) {
  return (creative.suggestion?.factors ?? []).find(
    (item) => item.factor_id === factorId
  );
}

/** Files the dialog refuses before any upload: type and size. */
export function screenFiles(files: File[]): {
  accepted: File[];
  refused: { name: string; reason: 'type' | 'size' }[];
} {
  const accepted: File[] = [];
  const refused: { name: string; reason: 'type' | 'size' }[] = [];
  for (const file of files) {
    if (!ACCEPTED_TYPES.includes(file.type)) {
      refused.push({ name: file.name, reason: 'type' });
    } else if (file.size > MAX_FILE_BYTES) {
      refused.push({ name: file.name, reason: 'size' });
    } else {
      accepted.push(file);
    }
  }
  return { accepted, refused };
}
