import { useCallback, useRef, useState } from 'react';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import type {
  CommandOutcome,
  GenerationPreview,
  IdeasCommands,
} from './useIdeasCommands';

/**
 * One paid image of the ideas (#200, #201): a correction, an image tool, a
 * failed ad tried again, or an ad made once more. Nothing is spent until the
 * user clicks the button that carries the maximum cost, and that number is
 * the preview's (`create_generation_preview`), never a guess:
 *
 * 1. `quote(draft)` prepares the generation draft and freezes it into a
 *    preview (both free);
 * 2. `confirm()` submits that preview: the click is the confirmation.
 */
export type PaidPhase =
  | { kind: 'idle' }
  | { kind: 'quoting' }
  /** `uncertain`: a confirmation got no answer; clicking again replays it. */
  | { kind: 'quoted'; preview: GenerationPreview; uncertain?: boolean }
  | { kind: 'submitting'; preview: GenerationPreview }
  | { kind: 'started' }
  /** `problem` null: no answer arrived; asking again replays the same command. */
  | { kind: 'problem'; problem: ProblemDetails | null };

export interface PaidImage {
  phase: PaidPhase;
  quote: (draft: () => Promise<CommandOutcome<string>>) => Promise<void>;
  confirm: () => Promise<void>;
  cancel: () => void;
}

export function usePaidImage(
  commands: Pick<IdeasCommands, 'quoteDraft' | 'submitPreview'>,
  onStarted?: () => void
): PaidImage {
  const [phase, setPhase] = useState<PaidPhase>({ kind: 'idle' });
  const busy = useRef(false);
  const started = useRef(onStarted);
  started.current = onStarted;

  const fail = (outcome: CommandOutcome<unknown>) =>
    setPhase({
      kind: 'problem',
      problem: outcome.kind === 'refused' ? outcome.problem : null,
    });

  const quote = useCallback(
    async (draft: () => Promise<CommandOutcome<string>>) => {
      if (busy.current) return;
      busy.current = true;
      setPhase({ kind: 'quoting' });
      try {
        const prepared = await draft();
        if (prepared.kind !== 'done') return fail(prepared);
        const preview = await commands.quoteDraft(prepared.data);
        if (preview.kind !== 'done') return fail(preview);
        setPhase({ kind: 'quoted', preview: preview.data });
      } finally {
        busy.current = false;
      }
    },
    [commands]
  );

  const confirm = useCallback(async () => {
    if (busy.current || phase.kind !== 'quoted') return;
    busy.current = true;
    const { preview } = phase;
    setPhase({ kind: 'submitting', preview });
    try {
      const outcome = await commands.submitPreview(preview);
      if (outcome.kind === 'done') {
        setPhase({ kind: 'started' });
        started.current?.();
      } else if (outcome.kind === 'uncertain')
        // The same preview is asked for again: never a second image.
        setPhase({ kind: 'quoted', preview, uncertain: true });
      else fail(outcome);
    } finally {
      busy.current = false;
    }
  }, [commands, phase]);

  const cancel = useCallback(() => setPhase({ kind: 'idle' }), []);

  return { phase, quote, confirm, cancel };
}
