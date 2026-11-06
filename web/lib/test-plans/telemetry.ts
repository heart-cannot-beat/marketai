/**
 * Front-end telemetry for the guided ad test (Issue #165):
 * `test_plan_started`, `test_plan_revised`, `test_plan_run_started`,
 * `test_plan_failed`.
 *
 * Same shape as `lib/products/telemetry.ts`: bounded labels only -- never
 * ids, product names, direction titles or typed text. Events go to an
 * optional `window.__TEST_PLAN_TELEMETRY__` sink so the host (or a test)
 * decides where they land.
 */

export type TestPlanEventName =
  | 'test_plan_started'
  | 'test_plan_revised'
  | 'test_plan_run_started'
  | 'test_plan_failed';

export interface TestPlanEvent {
  event: TestPlanEventName;
  goal?: string;
  scale?: string;
  /** Every question left at its default ("按默认开始"). */
  usedDefaults?: boolean;
  stage?: string;
  /** Which revise operations one save carried. */
  changes?: string[];
  failureCode?: string;
  /** Where the failure happened. */
  step?: 'create' | 'revise' | 'run' | 'drafting';
  executionMode?: string;
  replayed?: boolean;
}

const EVENTS = new Set<TestPlanEventName>([
  'test_plan_started',
  'test_plan_revised',
  'test_plan_run_started',
  'test_plan_failed',
]);
const GOALS = new Set(['explore', 'optimize']);
const SCALES = new Set(['sample', 'small', 'full']);
const STAGES = new Set(['sample', 'full']);
const CHANGES = new Set(['remove', 'restore', 'edit', 'add', 'scale']);
const STEPS = new Set(['create', 'revise', 'run', 'drafting']);
const MODES = new Set(['mock', 'byok', 'sandbox']);
const CODE_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;

export function sanitizeTestPlanEvent(
  event: TestPlanEvent
): TestPlanEvent | null {
  if (!EVENTS.has(event.event)) return null;
  const out: TestPlanEvent = { event: event.event };
  if (event.goal) out.goal = GOALS.has(event.goal) ? event.goal : 'unknown';
  if (event.scale) {
    out.scale = SCALES.has(event.scale) ? event.scale : 'unknown';
  }
  if (event.stage) {
    out.stage = STAGES.has(event.stage) ? event.stage : 'unknown';
  }
  if (typeof event.usedDefaults === 'boolean') {
    out.usedDefaults = event.usedDefaults;
  }
  if (event.changes) {
    out.changes = Array.from(
      new Set(event.changes.filter((change) => CHANGES.has(change)))
    ).sort();
  }
  if (event.failureCode) {
    out.failureCode = CODE_PATTERN.test(event.failureCode)
      ? event.failureCode
      : 'unknown';
  }
  if (event.step && STEPS.has(event.step)) out.step = event.step;
  if (event.executionMode) {
    out.executionMode = MODES.has(event.executionMode)
      ? event.executionMode
      : 'unknown';
  }
  if (typeof event.replayed === 'boolean') out.replayed = event.replayed;
  return out;
}

export function emitTestPlanEvent(event: TestPlanEvent): void {
  const sanitized = sanitizeTestPlanEvent(event);
  if (!sanitized) return;
  if (
    typeof window !== 'undefined' &&
    typeof (window as any).__TEST_PLAN_TELEMETRY__ === 'function'
  ) {
    try {
      (window as any).__TEST_PLAN_TELEMETRY__(sanitized);
    } catch {
      // A broken sink never breaks the flow.
    }
  }
}
