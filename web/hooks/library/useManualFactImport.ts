import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useOperation } from '@/hooks/domain/useOperation';
import { useUpload } from '@/hooks/domain/useUpload';
import { useLibraryCache } from '@/hooks/library/useLibraryCache';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { errorMessage, errorMessageOf } from '@/lib/i18n/domain-errors';

/** `OperationResultRef.resource_type` of a finished import
 * (`app/worker_runtime.py` `_library_manual_fact_import`). */
const FACT_SET_RESULT = 'manual_fact_set_draft';

/** Terminal Operation states in which the step did not happen. */
const UNFINISHED = new Set(['failed', 'cancelled', 'partially_succeeded']);

export type ManualFactImportView =
  | { step: 'idle' }
  /** Hashing, transferring and finalizing the file. */
  | { step: 'uploading' }
  /** The upload's verification Operation. */
  | { step: 'verifying' }
  /** The import's Operation. */
  | { step: 'importing' }
  /** `factSetId` is the new Manual Fact Set draft, when the result names it. */
  | { step: 'done'; factSetId: string | null }
  | { step: 'uncertain' }
  | { step: 'error'; message: string };

interface Started {
  /** One per chosen file. The upload derives its own keys from it; the
   * import is sent as `<id>:import`, so sending it again replays it. */
  actionInstanceId: string;
  leg: 'upload' | 'import';
  uploadIntentId: string | null;
  operationId: string | null;
}

/**
 * Imports a facts file into a new Manual Fact Set draft of a data source
 * (#14). The file is uploaded, its verification Operation and then the
 * import's Operation are followed with one `useOperation`, and a `succeeded`
 * import names the draft it created. `uncertain` stops the poll: nothing will
 * settle it, and the page says how to check instead.
 */
export function useManualFactImport(dataSourceEntryId: string): {
  view: ManualFactImportView;
  importFile: (file: File) => Promise<void>;
  reset: () => void;
} {
  const { t } = useTranslation('library');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const { startUpload } = useUpload('library_manual_fact_import');
  const { invalidate } = useLibraryCache();
  const [started, setStarted] = useState<Started | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState(false);
  // The file being imported: a reply about an earlier one is dropped.
  const actionRef = useRef<string | null>(null);
  // The file whose import was sent, so a re-render never sends it twice.
  const importSentRef = useRef<string | null>(null);

  const polled = useOperation({
    operationRecordId: started?.operationId ?? null,
    purpose: `manual-fact-${started?.leg ?? 'import'}`,
    actionInstanceId: started
      ? `${started.actionInstanceId}:${started.leg}`
      : 'manual-fact-import:idle',
  });
  // The previous leg's Operation until the poll of the next one resets.
  const operation =
    started?.operationId &&
    polled.operation?.operation_id === started.operationId
      ? polled.operation
      : undefined;
  const pollProblem = started?.operationId ? polled.problem : undefined;
  const state = operation?.state;

  useEffect(() => {
    if (state !== 'uncertain') return;
    setUncertain(true);
    setStarted((value) => value && { ...value, operationId: null });
  }, [state]);

  const sendImport = useCallback(
    async (actionInstanceId: string, uploadIntentId: string) => {
      try {
        const result = await gateway.importLibraryManualFacts({
          workspaceSlug: current.slug,
          actionInstanceId: `${actionInstanceId}:import`,
          dataSourceEntryId,
          body: {
            upload_intent_id: uploadIntentId,
            purpose: 'library_manual_fact_import',
          },
        });
        if (actionRef.current !== actionInstanceId) return;
        if (result.kind === 'reconciliation_required') {
          setFailure(t('data-source-ops.import-not-confirmed'));
        } else if (result.kind === 'problem') {
          setFailure(errorMessage(t, result.problem));
        } else {
          setStarted({
            actionInstanceId,
            leg: 'import',
            uploadIntentId,
            operationId: result.data.operation_id,
          });
        }
      } catch (error) {
        if (actionRef.current === actionInstanceId) {
          setFailure(errorMessageOf(t, error));
        }
      }
    },
    [gateway, current.slug, dataSourceEntryId, t]
  );

  // The file passed its checks: import it.
  const verified =
    started?.leg === 'upload' && state === 'succeeded' ? started : null;
  useEffect(() => {
    if (!verified?.uploadIntentId) return;
    const { actionInstanceId, uploadIntentId } = verified;
    if (importSentRef.current === actionInstanceId) return;
    importSentRef.current = actionInstanceId;
    setStarted({
      actionInstanceId,
      leg: 'import',
      uploadIntentId,
      operationId: null,
    });
    void sendImport(actionInstanceId, uploadIntentId);
  }, [verified, sendImport]);

  const imported =
    started?.leg === 'import' && state === 'succeeded'
      ? started.operationId
      : null;
  useEffect(() => {
    if (!imported) return;
    // The import creates a sibling Manual Fact Set; the selected Data Source
    // itself is unchanged. Revalidating its detail cache here unmounts this
    // panel and erases the success state before users can see it, so refresh
    // only the entry list that gained the new item.
    void invalidate({ resources: ['entryList'] });
  }, [imported, invalidate]);

  const importFile = useCallback(
    async (file: File) => {
      const actionInstanceId = newActionId();
      actionRef.current = actionInstanceId;
      setFailure(null);
      setUncertain(false);
      setStarted({
        actionInstanceId,
        leg: 'upload',
        uploadIntentId: null,
        operationId: null,
      });
      try {
        const result = await startUpload({
          file,
          ownerKind: 'data_source',
          ownerId: dataSourceEntryId,
          actionInstanceId,
        });
        if (actionRef.current !== actionInstanceId) return;
        if (result.kind === 'accepted') {
          setStarted({
            actionInstanceId,
            leg: 'upload',
            uploadIntentId: result.uploadIntentId,
            operationId: result.operationRecordId,
          });
        } else if (result.kind === 'problem') {
          setFailure(errorMessage(t, result.problem));
        } else if (result.kind === 'transfer_error') {
          // Its `message` is a fixed English diagnostic, never shown.
          setFailure(t('data-source-ops.upload-failed'));
        } else {
          setFailure(t('data-source-ops.import-not-confirmed'));
        }
      } catch (error) {
        if (actionRef.current === actionInstanceId) {
          setFailure(errorMessageOf(t, error));
        }
      }
    },
    [startUpload, dataSourceEntryId, t]
  );

  const reset = useCallback(() => {
    actionRef.current = null;
    setStarted(null);
    setFailure(null);
    setUncertain(false);
  }, []);

  let view: ManualFactImportView;
  if (failure !== null) view = { step: 'error', message: failure };
  else if (uncertain) view = { step: 'uncertain' };
  else if (!started) view = { step: 'idle' };
  else if (pollProblem)
    view = { step: 'error', message: errorMessage(t, pollProblem) };
  else if (started.leg === 'upload') {
    if (!started.operationId) view = { step: 'uploading' };
    else if (state && UNFINISHED.has(state))
      view = { step: 'error', message: t('data-source-ops.upload-rejected') };
    else view = { step: 'verifying' };
  } else if (operation && state === 'succeeded') {
    const result = operation.result;
    view = {
      step: 'done',
      factSetId:
        result?.resource_type === FACT_SET_RESULT && result.resource_id
          ? result.resource_id
          : null,
    };
  } else if (state && UNFINISHED.has(state))
    // Problems inside rows never fail an import: they are reported on the
    // draft it creates (#23). What is left is the file as a whole.
    view = { step: 'error', message: t('data-source-ops.import-rejected') };
  else view = { step: 'importing' };

  return { view, importFile, reset };
}

function newActionId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? `manual-fact-import:${crypto.randomUUID()}`
    : `manual-fact-import:${Math.random().toString(36).slice(2)}`;
}
