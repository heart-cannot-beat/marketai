import { formatNumber } from '@/lib/i18n/format';

/**
 * The Workspace SpendGuardrail as Settings reads and changes it (#126).
 *
 * `GET /api/v1/spend-guardrail` (`GuardrailAdmin.get`,
 * app/entitlements/guardrail_admin.py) answers with the four limits as
 * decimal strings in the Workspace budget currency; the OpenAPI document
 * types the body as `unknown`, so the shape is pinned and checked here.
 * Contract: docs/architecture/subscription-entitlement-usage-cost-ownership.md
 * §11 (the limits, lower freely, raise through preview and confirm) and §12
 * (Members read, the Owner manages).
 */

/** Settings' spending-limits section: where a guardrail refusal points (#126). */
export const SPEND_GUARDRAIL_ANCHOR = 'spend-guardrail';

export const LIMIT_FIELDS = [
  'per_operation_limit',
  'rolling_24h_limit',
  'period_limit',
  'outstanding_exposure_limit',
] as const;

export type LimitField = (typeof LIMIT_FIELDS)[number];

export type GuardrailLimits = Record<LimitField, string>;

export interface SpendGuardrail extends GuardrailLimits {
  workspace_id: string;
  revision: number;
  budget_currency: string;
}

/** `POST /api/v1/spend-guardrail/preview`'s answer. */
export interface SpendGuardrailPreview {
  preview_token: string;
  current_revision: number;
  old_limits: GuardrailLimits;
  proposed_limits: GuardrailLimits;
  expires_at: string;
}

const isLimits = (value: unknown): value is GuardrailLimits =>
  typeof value === 'object' &&
  value !== null &&
  LIMIT_FIELDS.every(
    (field) => typeof (value as Record<string, unknown>)[field] === 'string'
  );

export function isSpendGuardrail(value: unknown): value is SpendGuardrail {
  return (
    isLimits(value) &&
    typeof (value as SpendGuardrail).revision === 'number' &&
    typeof (value as SpendGuardrail).budget_currency === 'string'
  );
}

export function isSpendGuardrailPreview(
  value: unknown
): value is SpendGuardrailPreview {
  const preview = value as SpendGuardrailPreview;
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof preview.preview_token === 'string' &&
    typeof preview.current_revision === 'number' &&
    isLimits(preview.old_limits) &&
    isLimits(preview.proposed_limits)
  );
}

/** A non-negative decimal with at most 12 places (`Numeric(24, 12)`). */
const DECIMAL = /^\d{1,12}(\.\d{1,12})?$/;

export const isLimitValue = (value: string): boolean =>
  DECIMAL.test(value.trim());

/** `"5.000000000000"` -> `"5"`, `"2.500"` -> `"2.5"`: what an input starts with. */
export function plainDecimal(value: string): string {
  const trimmed = value.trim();
  if (!trimmed.includes('.')) return trimmed;
  return trimmed.replace(/0+$/, '').replace(/\.$/, '');
}

/** Compares two non-negative decimal strings without floating point. */
export function compareDecimal(a: string, b: string): number {
  const [aInt, aFrac = ''] = plainDecimal(a).split('.');
  const [bInt, bFrac = ''] = plainDecimal(b).split('.');
  const aWhole = aInt.replace(/^0+(?=\d)/, '');
  const bWhole = bInt.replace(/^0+(?=\d)/, '');
  if (aWhole.length !== bWhole.length) {
    return aWhole.length < bWhole.length ? -1 : 1;
  }
  if (aWhole !== bWhole) return aWhole < bWhole ? -1 : 1;
  const width = Math.max(aFrac.length, bFrac.length);
  const aPart = aFrac.padEnd(width, '0');
  const bPart = bFrac.padEnd(width, '0');
  if (aPart === bPart) return 0;
  return aPart < bPart ? -1 : 1;
}

/**
 * What saving `draft` over `current` is: `lower` when no limit goes up (the
 * Owner's save confirms it), `raise` when any limit goes up (the Owner
 * confirms the server's preview a second time), `unchanged` when every
 * limit stays, `invalid` while a limit is not a decimal of 0 or more.
 */
export type ChangeKind = 'lower' | 'raise' | 'unchanged' | 'invalid';

export function changeKind(
  current: GuardrailLimits,
  draft: GuardrailLimits
): ChangeKind {
  if (!LIMIT_FIELDS.every((field) => isLimitValue(draft[field]))) {
    return 'invalid';
  }
  const comparisons = LIMIT_FIELDS.map((field) =>
    compareDecimal(draft[field], current[field])
  );
  if (comparisons.some((result) => result > 0)) return 'raise';
  if (comparisons.some((result) => result < 0)) return 'lower';
  return 'unchanged';
}

export function draftFrom(limits: GuardrailLimits): GuardrailLimits {
  return {
    per_operation_limit: plainDecimal(limits.per_operation_limit),
    rolling_24h_limit: plainDecimal(limits.rolling_24h_limit),
    period_limit: plainDecimal(limits.period_limit),
    outstanding_exposure_limit: plainDecimal(limits.outstanding_exposure_limit),
  };
}

/** An amount in the budget currency; the code itself when Intl does not know it. */
export function formatLimit(
  locale: string,
  value: string,
  currency: string
): string {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return `${value} ${currency}`;
  try {
    return formatNumber(locale, amount, {
      style: 'currency',
      currency,
      minimumFractionDigits: 0,
      maximumFractionDigits: 4,
    });
  } catch {
    return `${plainDecimal(value)} ${currency}`;
  }
}

/** The field a `spend_guardrail_limit_above_platform` refusal names. */
export function refusedField(
  params: Record<string, unknown> | null | undefined
): LimitField | null {
  const field = params?.field;
  return typeof field === 'string' &&
    (LIMIT_FIELDS as readonly string[]).includes(field)
    ? (field as LimitField)
    : null;
}
