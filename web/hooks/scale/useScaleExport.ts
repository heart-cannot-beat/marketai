import { useCallback } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { newActionId } from '@/components/generation/shared/newActionId';
import type { CommandOutcome } from '@/lib/domain-client/result-types';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';

export type ScaleExport = components['schemas']['ExportResponse'];
export type ScaleExportDownloadIntent =
  components['schemas']['ExportDownloadIntentResponse'];

/**
 * Creates a durable export Operation (spec §16, §22, Issue #87 Task 5) --
 * `202`, tracked to completion via `useOperation`/`pollOperation` on the
 * returned `operation_id`, not polled here. The export's own `id` (also
 * returned immediately) is what `useCreateScaleExportDownloadIntent`
 * consumes once that Operation reaches a terminal `succeeded` state --
 * there is no separate `get_scale_export` read to re-fetch it by id.
 */
export function useCreateScaleExport() {
  const command = useDomainCommand('create_scale_export');
  const run = useCallback(
    (params: {
      metaMode: ScaleMetaMode;
      enrollmentId: string;
      episodeId: string;
      exportKind: string;
      snapshotId?: string;
      evaluationId?: string;
    }): Promise<CommandOutcome<'create_scale_export'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: { meta_mode: params.metaMode } as any,
        body: {
          enrollment_id: params.enrollmentId,
          episode_id: params.episodeId,
          export_kind: params.exportKind,
          snapshot_id: params.snapshotId ?? null,
          evaluation_id: params.evaluationId ?? null,
        },
      }),
    [command]
  );
  return { ...command, run };
}

/** Creates an auditable download intent for a completed export (spec
 * §7.12, §16) -- mirrors the Generation export-package download-intent
 * flow. `ExportDownloadIntentResponse` carries only `intent_id`/
 * `expires_at` (no `download_grant` yet, matching
 * `create_generation_export_download_intent`'s own note), so the browser's
 * job here ends at recording the intent -- the actual signed download URL
 * is a later seam. */
export function useCreateScaleExportDownloadIntent() {
  const command = useDomainCommand('create_scale_export_download_intent');
  const run = useCallback(
    (params: {
      exportId: string;
      metaMode: ScaleMetaMode;
    }): Promise<CommandOutcome<'create_scale_export_download_intent'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: {
          export_id: params.exportId,
          meta_mode: params.metaMode,
        } as any,
      }),
    [command]
  );
  return { ...command, run };
}
