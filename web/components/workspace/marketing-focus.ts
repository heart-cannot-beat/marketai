import { createContext, useCallback, useContext } from 'react';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { BoardExecution } from './MarketingBoard';

type Workflow = components['schemas']['WorkflowEnvelope'];
type Round = components['schemas']['MarketingRound'];

/**
 * The test page's one main action (J-32). Inside a test page only the button
 * that commits this action keeps the filled style; every other button reads
 * as secondary. Outside a test page (no provider) components keep their own
 * emphasis, so the standalone Generate workbench is unchanged.
 */
const MainAction = createContext<string | null | undefined>(undefined);
export const MainActionProvider = MainAction.Provider;

export function useEmphasis() {
  const main = useContext(MainAction);
  return useCallback(
    (action: string | readonly string[], primary: string, secondary: string) =>
      main === undefined ||
      (main !== null && ([] as readonly string[]).concat(action).includes(main))
        ? primary
        : secondary,
    [main]
  );
}

/** The round step the page asks for next, from server state only. */
export type RoundStep =
  | 'freeze'
  | 'generate'
  | 'ai_qc'
  | 'single_qc'
  | 'paired_qc'
  | 'review'
  | 'revise'
  | 'export'
  | 'retry_export'
  | 'download'
  | 'bind'
  | 'import'
  | 'evaluate'
  | 'close'
  | 'next_plan'
  | 'wait'
  | 'done';

export interface RoundFocus {
  step: RoundStep;
  /** How many board images the step is about (0 when it is round-wide). */
  count: number;
  /** The first image the step applies to, for opening its drawer. */
  executionKey: string | null;
}

/** Board actions in the order a round works through them (`single_qc` and
 * `paired_qc` are the split AI checks of #229). */
const ORDER = [
  'generate',
  'ai_qc',
  'single_qc',
  'paired_qc',
  'review',
  'revise',
  'export',
  'bind',
  'import',
  'evaluate',
] as const;

/** The board's tile identity (`MarketingBoard.executionKey`), kept here so the
 * focus helpers stay free of the board's runtime. */
function executionKey(e: BoardExecution) {
  return e.asset_version_id ?? `${e.combination_id}#${e.execution_index}`;
}

export function boardExecutions(
  round: Round | null | undefined
): BoardExecution[] {
  return (round?.board?.tree.angles ?? []).flatMap((angle) =>
    angle.expressions.flatMap((expression) =>
      expression.combinations.flatMap((combination) => combination.executions)
    )
  );
}

/** Which section the page shows after an ended round (#367). */
export interface AfterRound {
  /** Planning the next round, while the project holds the plan it froze. */
  nextPlan: boolean;
  /** Starting a round from a confirmed plan the round did not freeze. */
  freeze: boolean;
}

/**
 * Reads the round's next step from the workflow's `next_action` and the
 * board's per-image `next_action`: the page never infers comparisons or gaps
 * of its own. Once the round has ended, the step is the section the page
 * shows after it (`after`); with both shown, planning the next round comes
 * first, so a failed plan request is retried rather than the round repeated.
 */
export function roundFocus(
  workflow: Workflow | undefined,
  round: Round | null | undefined,
  after: AfterRound
): RoundFocus | null {
  if (!workflow) return null;
  const none = { count: 0, executionKey: null };
  if (!round)
    return workflow.next_action === 'freeze_round'
      ? { step: 'freeze', ...none }
      : null;
  if (round.state === 'closed')
    return {
      step: after.nextPlan ? 'next_plan' : after.freeze ? 'freeze' : 'done',
      ...none,
    };
  if (round.handoff?.state === 'failed')
    return { step: 'retry_export', ...none };
  if (round.handoff && round.handoff.state !== 'ready')
    return { step: 'wait', ...none };
  const executions = boardExecutions(round);
  for (const action of ORDER) {
    const matching = executions.filter((e) => e.next_action === action);
    if (!matching.length) continue;
    if (action === 'bind' && !round.bindings.length)
      return { step: 'download', count: matching.length, executionKey: null };
    return {
      step: action,
      count: matching.length,
      executionKey: executionKey(matching[0]),
    };
  }
  if (round.state === 'evaluated') return { step: 'close', ...none };
  return { step: 'wait', ...none };
}

/** Where each round step is committed on the page. */
export const STEP_TARGET: Partial<Record<RoundStep, string>> = {
  freeze: 'marketing-freeze',
  generate: 'marketing-generation-editor',
  ai_qc: 'marketing-ai-qc',
  single_qc: 'marketing-ai-qc',
  paired_qc: 'marketing-ai-qc',
  export: 'marketing-export',
  retry_export: 'marketing-handoff',
  download: 'marketing-handoff',
  bind: 'marketing-binding',
  import: 'marketing-import',
  evaluate: 'marketing-evaluate',
  close: 'marketing-close',
  next_plan: 'marketing-next-plan',
};

/** Scrolls to a step's section and moves focus there, opening folded parents. */
export function goTo(id: string) {
  const target = document.getElementById(id);
  if (!target) return false;
  let parent = target.parentElement;
  while (parent) {
    if (parent instanceof HTMLDetailsElement) parent.open = true;
    parent = parent.parentElement;
  }
  if (target instanceof HTMLDetailsElement) target.open = true;
  target.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
  if (target.tabIndex < 0 && !target.hasAttribute('tabindex'))
    target.setAttribute('tabindex', '-1');
  target.focus?.({ preventScroll: true });
  return true;
}
