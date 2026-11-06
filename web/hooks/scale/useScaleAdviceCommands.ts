import { useCallback } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { newActionId } from '@/components/generation/shared/newActionId';
import type { CommandOutcome } from '@/lib/domain-client/result-types';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';

/**
 * `AdviceDispositionRequest.kind` is validated server-side
 * (`app.scale.advice.create_disposition` rejects everything but these two,
 * mapped to 422) -- the browser never offers a third option.
 */
export type AdviceDispositionKind = 'acknowledged' | 'dismissed';

/** Records a disposition (acknowledge/dismiss) on an Advice thread (spec
 * §4.2). Gated by the thread's own `allowed_actions` (`acknowledge`/
 * `dismiss`), never by a client-side lifecycle guess. */
export function useCreateScaleAdviceDisposition() {
  const command = useDomainCommand('create_scale_advice_disposition');
  const run = useCallback(
    (params: {
      threadId: string;
      metaMode: ScaleMetaMode;
      kind: AdviceDispositionKind;
      note?: string;
      occurrenceId?: string;
    }): Promise<CommandOutcome<'create_scale_advice_disposition'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: {
          thread_id: params.threadId,
          meta_mode: params.metaMode,
        } as any,
        body: {
          kind: params.kind,
          note: params.note ?? null,
          occurrence_id: params.occurrenceId ?? null,
        },
      }),
    [command]
  );
  return { ...command, run };
}

/** Snoozes an Advice thread until a future timestamp (spec §4.2). Gated by
 * the thread's `snooze` action. */
export function useCreateScaleAdviceSnooze() {
  const command = useDomainCommand('create_scale_advice_snooze');
  const run = useCallback(
    (params: {
      threadId: string;
      metaMode: ScaleMetaMode;
      until: string;
      reason?: string;
    }): Promise<CommandOutcome<'create_scale_advice_snooze'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: {
          thread_id: params.threadId,
          meta_mode: params.metaMode,
        } as any,
        body: {
          until: params.until,
          reason: params.reason ?? null,
        },
      }),
    [command]
  );
  return { ...command, run };
}

/** Converts an Advice occurrence into a Generation draft (`refresh_creative`)
 * or a Scale-test plan draft (`consider_scale_test`) -- only the two advice
 * types `_thread_actions` marks `convert`-eligible ever reach this. */
export function useConvertScaleAdviceOccurrence() {
  const command = useDomainCommand('convert_scale_advice_occurrence');
  const run = useCallback(
    (params: {
      occurrenceId: string;
      metaMode: ScaleMetaMode;
      provider?: string;
      model?: string;
      refreshAxis?: string;
    }): Promise<CommandOutcome<'convert_scale_advice_occurrence'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: {
          occurrence_id: params.occurrenceId,
          meta_mode: params.metaMode,
        } as any,
        body: {
          provider: params.provider ?? 'scale_advice_default',
          model: params.model ?? 'scale_advice_default',
          refresh_axis: params.refreshAxis ?? null,
        },
      }),
    [command]
  );
  return { ...command, run };
}
