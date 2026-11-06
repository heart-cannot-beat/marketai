import { useTranslation } from 'next-i18next';
import { useRef, useState, useCallback } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useUpload } from '@/hooks/domain/useUpload';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useLibraryCache } from '@/hooks/library/useLibraryCache';
import { DomainProblemError, errorMessage } from '@/lib/i18n/domain-errors';

export type ImportState =
  | 'idle'
  | 'uploading'
  | 'processing'
  | 'complete'
  | 'error';

interface RowValidationError {
  row: number;
  field: string;
  message: string;
}

interface ManualFactImportPanelProps {
  dataSourceEntryId: string;
}

export function ManualFactImportPanel({
  dataSourceEntryId,
}: ManualFactImportPanelProps) {
  const { t } = useTranslation('library');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const upload = useUpload('library_manual_fact_import');
  const { invalidate } = useLibraryCache();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [state, setState] = useState<ImportState>('idle');
  const [rowErrors, setRowErrors] = useState<RowValidationError[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [importedCount, setImportedCount] = useState(0);

  const handleImport = useCallback(
    async (file: File) => {
      setState('uploading');
      setError(null);
      setRowErrors([]);

      try {
        const uploadResult = await upload.startUpload({
          file,
          ownerKind: 'data_source',
          ownerId: dataSourceEntryId,
          actionInstanceId: newActionId('manual-fact-upload'),
        });
        if (uploadResult.kind === 'problem') {
          throw new DomainProblemError(uploadResult.problem);
        }
        if (uploadResult.kind === 'transfer_error') {
          throw new Error(uploadResult.error.message);
        }
        if (uploadResult.kind === 'reconciliation_required') {
          throw new Error('Import upload outcome requires reconciliation');
        }

        setState('processing');
        await waitForOperation(
          gateway,
          current.slug,
          uploadResult.operationRecordId
        );

        const result = await gateway.importLibraryManualFacts({
          workspaceSlug: current.slug,
          actionInstanceId: newActionId('manual-fact-import'),
          dataSourceEntryId,
          body: {
            upload_intent_id: uploadResult.uploadIntentId,
            purpose: 'library_manual_fact_import',
          },
        });

        if (result.kind === 'reconciliation_required') {
          throw new Error('Import outcome requires reconciliation');
        }

        if (result.kind === 'problem') {
          throw new DomainProblemError(result.problem);
        }

        await waitForOperation(gateway, current.slug, result.data.operation_id);
        // The import creates a sibling Manual Fact Set; the selected Data
        // Source itself is unchanged. Revalidating its detail cache here
        // unmounts this panel and erases the success state before users can
        // see it, so refresh only the entry list that gained the new item.
        await invalidate({ resources: ['entryList'] });

        setRowErrors([]);
        setImportedCount(0);
        setState('complete');
      } catch (err) {
        setState('error');
        setError(
          err instanceof DomainProblemError
            ? errorMessage(t, err.problem)
            : (err as Error).message
        );
      }
    },
    [current.slug, dataSourceEntryId, gateway, invalidate, upload, t]
  );

  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file) handleImport(file);
    },
    [handleImport]
  );

  const reset = useCallback(() => {
    setState('idle');
    setRowErrors([]);
    setError(null);
    setImportedCount(0);
    if (fileInputRef.current) fileInputRef.current.value = '';
  }, []);

  if (state === 'complete') {
    return (
      <div className="rounded-lg border border-green-200 bg-green-50 p-4 dark:border-green-800 dark:bg-green-900/20">
        <p className="text-sm text-green-700 dark:text-green-300">
          {t('data-source-ops.import-complete', {
            count: String(importedCount),
          })}
        </p>
        {rowErrors.length > 0 && (
          <div className="mt-2">
            <p className="text-xs font-medium text-amber-700 dark:text-amber-400">
              {t('data-source-ops.import-row-errors', {
                count: String(rowErrors.length),
              })}
            </p>
            <ul className="mt-1 list-disc pl-4 text-xs text-amber-600 dark:text-amber-400">
              {rowErrors.slice(0, 10).map((re, i) => (
                <li key={i}>
                  {t('data-source-ops.row-error', {
                    row: String(re.row),
                    field: re.field,
                    message: re.message,
                  })}
                </li>
              ))}
            </ul>
          </div>
        )}
        <button
          type="button"
          className="btn btn-ghost btn-xs mt-3"
          onClick={reset}
        >
          {t('data-source-ops.done')}
        </button>
      </div>
    );
  }

  if (state === 'error') {
    return (
      <div
        className="rounded-lg border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-900/20"
        role="alert"
      >
        <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
        <button
          type="button"
          className="btn btn-ghost btn-xs mt-2"
          onClick={reset}
        >
          {t('data-source-ops.retry')}
        </button>
      </div>
    );
  }

  if (state === 'uploading' || state === 'processing') {
    return (
      <div
        className="rounded-lg border border-gray-200 p-4 dark:border-gray-700"
        aria-busy="true"
      >
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {state === 'uploading'
            ? t('data-source-ops.importing')
            : t('data-source-ops.processing')}
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
      <p className="text-sm text-gray-700 dark:text-gray-300">
        {t('data-source-ops.import-description')}
      </p>
      <button
        type="button"
        className="btn btn-primary btn-sm mt-3"
        onClick={() => fileInputRef.current?.click()}
      >
        {t('data-source-ops.select-file')}
      </button>
      <input
        ref={fileInputRef}
        type="file"
        accept=".csv,.json"
        className="hidden"
        onChange={handleFileChange}
        aria-label={t('data-source-ops.file-input')}
      />
    </div>
  );
}

const OPERATION_POLL_INTERVAL_MS = 1000;
const OPERATION_MAX_POLLS = 120;

async function waitForOperation(
  gateway: ReturnType<typeof useDomainGateway>,
  workspaceSlug: string,
  operationId: string
): Promise<void> {
  for (let attempt = 0; attempt < OPERATION_MAX_POLLS; attempt += 1) {
    const result = await gateway.pollOperation({ workspaceSlug, operationId });
    if (result.kind === 'problem') {
      throw new DomainProblemError(result.problem);
    }
    if (result.kind === 'not_modified') {
      await delay(OPERATION_POLL_INTERVAL_MS);
      continue;
    }
    if (result.data.state === 'succeeded') return;
    if (
      result.data.state === 'failed' ||
      result.data.state === 'cancelled' ||
      result.data.state === 'uncertain'
    ) {
      throw new Error('Import operation did not complete successfully');
    }
    await delay(OPERATION_POLL_INTERVAL_MS);
  }
  throw new Error('Import operation timed out');
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function newActionId(prefix: string): string {
  const suffix =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2);
  return `${prefix}:${suffix}`;
}
