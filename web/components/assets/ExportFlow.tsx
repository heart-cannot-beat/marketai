import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useSWRConfig } from 'swr';
import { useExportFlow } from '@/hooks/assets/useExportFlow';
import type { ExportSelector } from '@/hooks/assets/useExportFlow';
import { ExportManifestView } from './ExportManifestView';
import { useGrantedDownload } from '@/hooks/domain/useGrantedDownload';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useAssets } from './AssetsProvider';
import {
  matchesOperation,
  matchesWorkspace,
} from '@/lib/domain-client/query-keys';
import type { DomainQueryKey } from '@/lib/domain-client/query-keys';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface ExportFlowProps {
  packageId?: string;
  onClose: () => void;
}

type Preset = 'winner_only' | 'approved_only' | 'all_successful' | 'explicit';

export function ExportFlow({
  packageId: initialPackageId,
  onClose,
}: ExportFlowProps) {
  const { t } = useTranslation('assets');
  const { selection, urlState } = useAssets();
  const flow = useExportFlow({
    initialPackageId: initialPackageId !== 'new' ? initialPackageId : undefined,
  });
  const downloadIntent = useDomainCommand(
    'create_generation_export_download_intent'
  );
  const { startDownload } = useGrantedDownload();
  const [selectedPreset, setSelectedPreset] = useState<Preset>(
    selection.count > 0 ? 'explicit' : 'approved_only'
  );
  const { mutate: globalMutate } = useSWRConfig();
  const { current } = useWorkspace();

  useEffect(() => {
    if (flow.packageId && flow.packageId !== initialPackageId) {
      urlState.setExportId(flow.packageId);
    }
  }, [flow.packageId, initialPackageId, urlState]);

  const handleStart = useCallback(async () => {
    const selector: ExportSelector = {
      kind: selectedPreset,
      version_ids:
        selectedPreset === 'explicit'
          ? Array.from(selection.selectedIds)
          : undefined,
      experiment_context_id:
        selectedPreset === 'winner_only'
          ? urlState.filters.experimentContext
          : undefined,
    };
    await flow.startExport(selector);
  }, [selectedPreset, selection, flow, urlState.filters.experimentContext]);

  const handleDownload = useCallback(async () => {
    if (!flow.packageId) return;
    const outcome = await downloadIntent.execute({
      pathParams: { export_package_id: flow.packageId },
      actionInstanceId: `export-dl:${flow.packageId}`,
    });
    if (outcome.kind === 'success') {
      const grant = (outcome.data as any).download_grant;
      await startDownload(grant);

      // Export completion -- revalidate this package's manifest so a
      // recovered/reopened `ExportFlow` (spec §14 row 17, `initialPackageId`)
      // never reads a manifest snapshot that predates the download it just
      // granted.
      globalMutate(
        (key: unknown) => {
          if (!Array.isArray(key) || key[0] !== 'domain') return false;
          const dk = key as unknown as DomainQueryKey;
          if (!matchesWorkspace(dk, current.slug)) return false;
          return matchesOperation(dk, 'get_generation_export_manifest');
        },
        undefined,
        { revalidate: true }
      );
    }
  }, [
    flow.packageId,
    downloadIntent,
    startDownload,
    globalMutate,
    current.slug,
  ]);

  return (
    <dialog className="modal modal-open" data-testid="export-dialog">
      <div className="modal-box max-w-2xl">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-bold">{t('export-heading')}</h2>
          <button
            type="button"
            className="btn btn-sm btn-ghost btn-circle"
            onClick={onClose}
            aria-label={t('close')}
          >
            {t('close-symbol')}
          </button>
        </div>

        {/* Step 1: Configure */}
        {flow.step === 'configure' && (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <label className="label cursor-pointer justify-start gap-3">
                <input
                  type="radio"
                  name="preset"
                  className="radio radio-sm"
                  checked={selectedPreset === 'approved_only'}
                  onChange={() => setSelectedPreset('approved_only')}
                  data-testid="export-preset-approved_only"
                />
                <span>{t('approved-only')}</span>
              </label>
              <label className="label cursor-pointer justify-start gap-3">
                <input
                  type="radio"
                  name="preset"
                  className="radio radio-sm"
                  checked={selectedPreset === 'winner_only'}
                  onChange={() => setSelectedPreset('winner_only')}
                  data-testid="export-preset-winner_only"
                />
                <span>{t('winners-only')}</span>
              </label>
              <label className="label cursor-pointer justify-start gap-3">
                <input
                  type="radio"
                  name="preset"
                  className="radio radio-sm"
                  checked={selectedPreset === 'all_successful'}
                  onChange={() => setSelectedPreset('all_successful')}
                  data-testid="export-preset-all_successful"
                />
                <span>{t('all-successful')}</span>
              </label>
              {selection.count > 0 && (
                <label className="label cursor-pointer justify-start gap-3">
                  <input
                    type="radio"
                    name="preset"
                    className="radio radio-sm"
                    checked={selectedPreset === 'explicit'}
                    onChange={() => setSelectedPreset('explicit')}
                    data-testid="export-preset-explicit"
                  />
                  <span>
                    {t('selected-assets', { count: selection.count })}
                  </span>
                </label>
              )}
            </div>

            {flow.problem && (
              <div role="alert" className="alert alert-error text-sm">
                <span>{errorMessage(t, flow.problem)}</span>
              </div>
            )}

            <div className="modal-action">
              <button
                type="button"
                className="btn btn-ghost"
                onClick={onClose}
                data-testid="export-cancel"
              >
                {t('cancel')}
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleStart}
                data-testid="export-start"
                disabled={flow.isExporting}
              >
                {flow.isExporting ? (
                  <span className="loading loading-spinner loading-sm" />
                ) : (
                  t('start-export')
                )}
              </button>
            </div>
          </div>
        )}

        {/* Step 2: Processing */}
        {flow.step === 'processing' && (
          <div className="flex flex-col items-center gap-4 py-8">
            <span className="loading loading-spinner loading-lg" />
            <p>{t('building-export')}</p>
            <p className="text-sm text-base-content/60">
              {t('building-export-description')}
            </p>
          </div>
        )}

        {/* Step 3: Ready */}
        {flow.step === 'ready' && !!flow.manifest && (
          <div className="flex flex-col gap-4">
            <ExportManifestView
              manifest={flow.manifest}
              packageDetail={flow.packageDetail}
            />
            <div className="modal-action">
              <button type="button" className="btn btn-ghost" onClick={onClose}>
                {t('close')}
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleDownload}
                data-testid="export-download"
              >
                {t('download-zip')}
              </button>
            </div>
          </div>
        )}

        {/* Error */}
        {flow.step === 'error' && (
          <div className="flex flex-col items-center gap-4 py-8">
            <div role="alert" className="alert alert-error">
              <span>{t('export-failed')}</span>
            </div>
            <div className="modal-action">
              <button type="button" className="btn btn-ghost" onClick={onClose}>
                {t('close')}
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={flow.reset}
              >
                {t('retry')}
              </button>
            </div>
          </div>
        )}
      </div>
    </dialog>
  );
}
