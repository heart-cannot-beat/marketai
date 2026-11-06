/**
 * Front-end telemetry for the product intake flow (Issue #162):
 * `product_intake_started`, `product_intake_confirmed`,
 * `product_intake_failed`.
 *
 * Same shape as `lib/library/telemetry.ts`: bounded labels only, never ids,
 * URLs, product names or pasted text. Events go to an optional
 * `window.__PRODUCT_TELEMETRY__` sink so the host (or a test) decides where
 * they land.
 */

export type ProductIntakeEventName =
  | 'product_intake_started'
  | 'product_intake_confirmed'
  | 'product_intake_failed';

export type IntakeInputKind = 'url' | 'images' | 'text';

export interface ProductIntakeEvent {
  event: ProductIntakeEventName;
  inputKinds?: IntakeInputKind[];
  executionMode?: string;
  /** Server failure code (fetch/extract) or confirm problem code. */
  failureCode?: string;
  suggestedAction?: string;
  stage?: 'intake' | 'confirm' | 'upload';
  replayed?: boolean;
  durationMs?: number;
}

const EVENTS = new Set<ProductIntakeEventName>([
  'product_intake_started',
  'product_intake_confirmed',
  'product_intake_failed',
]);
const INPUT_KINDS = new Set<IntakeInputKind>(['url', 'images', 'text']);
const MODES = new Set(['mock', 'byok', 'controlled_mock', 'sandbox']);
const STAGES = new Set(['intake', 'confirm', 'upload']);
const ACTIONS = new Set([
  'paste_text_or_screenshot',
  'check_link',
  'retry',
  'add_more_detail',
  'bind_openai_key',
  'adjust_spend_guardrail',
]);
/** Stable codes are short snake_case words; anything else is collapsed. */
const CODE_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;

export function sanitizeProductIntakeEvent(
  event: ProductIntakeEvent
): ProductIntakeEvent | null {
  if (!EVENTS.has(event.event)) return null;
  const out: ProductIntakeEvent = { event: event.event };
  if (event.inputKinds) {
    out.inputKinds = Array.from(
      new Set(event.inputKinds.filter((kind) => INPUT_KINDS.has(kind)))
    ).sort();
  }
  if (event.executionMode) {
    out.executionMode = MODES.has(event.executionMode)
      ? event.executionMode
      : 'unknown';
  }
  if (event.failureCode) {
    out.failureCode = CODE_PATTERN.test(event.failureCode)
      ? event.failureCode
      : 'unknown';
  }
  if (event.suggestedAction) {
    out.suggestedAction = ACTIONS.has(event.suggestedAction)
      ? event.suggestedAction
      : 'unknown';
  }
  if (event.stage && STAGES.has(event.stage)) out.stage = event.stage;
  if (typeof event.replayed === 'boolean') out.replayed = event.replayed;
  if (typeof event.durationMs === 'number' && event.durationMs >= 0) {
    out.durationMs = Math.round(event.durationMs);
  }
  return out;
}

export function emitProductIntakeEvent(event: ProductIntakeEvent): void {
  const sanitized = sanitizeProductIntakeEvent(event);
  if (!sanitized) return;
  if (
    typeof window !== 'undefined' &&
    typeof (window as any).__PRODUCT_TELEMETRY__ === 'function'
  ) {
    try {
      (window as any).__PRODUCT_TELEMETRY__(sanitized);
    } catch {
      // A broken sink never breaks the flow.
    }
  }
}
