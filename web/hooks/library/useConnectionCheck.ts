import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useOperation } from '@/hooks/domain/useOperation';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { DomainOperationMap } from '@/lib/domain-client/generated/operation-map';
import {
  errorCodeMessage,
  errorMessage,
  errorMessageOf,
} from '@/lib/i18n/domain-errors';

export type ConnectionCheck =
  DomainOperationMap['get_library_data_source_connection_check']['successResponses'][200];

/** `OperationResultRef.resource_type` of a finished check
 * (`app/library/connection_checks.py` `RESULT_RESOURCE_TYPE`). */
const CHECK_RESULT = 'data_source_connection_check';

/** Terminal Operation states in which no check result exists. */
const UNFINISHED = new Set(['failed', 'cancelled', 'partially_succeeded']);

export type ConnectionCheckView =
  | { step: 'idle' }
  | { step: 'checking' }
  | { step: 'done'; check: ConnectionCheck }
  | { step: 'uncertain' }
  | { step: 'error'; message: string };

interface Started {
  actionInstanceId: string;
  operationId: string | null;
}

/**
 * Starts a data source's connection check and follows it to its result
 * (#383): the `202` names an Operation, `useOperation` polls it to a terminal
 * state, and a `succeeded` Operation's result names the check, read back for
 * its conclusion. The Operation only says the check ran; what it found is in
 * the check. `uncertain` stops the poll, since nothing will settle it.
 */
export function useConnectionCheck(dataSourceEntryId: string): {
  view: ConnectionCheckView;
  check: () => Promise<void>;
} {
  const { t } = useTranslation('library');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const [started, setStarted] = useState<Started | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState(false);

  const polled = useOperation({
    operationRecordId: started?.operationId ?? null,
    purpose: 'connection-check',
    actionInstanceId: started?.actionInstanceId ?? 'connection-check:idle',
  });
  // The previous check's Operation until the poll of a new one resets.
  const operation =
    started?.operationId &&
    polled.operation?.operation_id === started.operationId
      ? polled.operation
      : undefined;
  const pollProblem = started?.operationId ? polled.problem : undefined;

  useEffect(() => {
    if (operation?.state !== 'uncertain') return;
    setUncertain(true);
    setStarted((value) => value && { ...value, operationId: null });
  }, [operation?.state]);

  const checkId =
    operation?.state === 'succeeded' &&
    operation.result?.resource_type === CHECK_RESULT
      ? operation.result.resource_id
      : null;
  const read = useDomainQuery(
    'get_library_data_source_connection_check',
    checkId
      ? { data_source_entry_id: dataSourceEntryId, check_id: checkId }
      : undefined,
    { enabled: checkId !== null }
  );

  const check = useCallback(async () => {
    const actionInstanceId = newActionId();
    setFailure(null);
    setUncertain(false);
    setStarted({ actionInstanceId, operationId: null });
    try {
      const result = await gateway.checkLibraryDataSourceConnection({
        workspaceSlug: current.slug,
        actionInstanceId,
        dataSourceEntryId,
        body: {},
      });
      if (result.kind === 'reconciliation_required') {
        setFailure(t('data-source-ops.check-not-confirmed'));
      } else if (result.kind === 'problem') {
        setFailure(errorMessage(t, result.problem));
      } else {
        setStarted({ actionInstanceId, operationId: result.data.operation_id });
      }
    } catch (error) {
      setFailure(errorMessageOf(t, error));
    }
  }, [gateway, current.slug, dataSourceEntryId, t]);

  let view: ConnectionCheckView;
  if (failure !== null) view = { step: 'error', message: failure };
  else if (uncertain) view = { step: 'uncertain' };
  else if (!started) view = { step: 'idle' };
  else if (pollProblem)
    view = { step: 'error', message: errorMessage(t, pollProblem) };
  else if (read.problem)
    view = { step: 'error', message: errorMessage(t, read.problem) };
  else if (read.data) view = { step: 'done', check: read.data };
  else if (
    operation &&
    // A check that could not run, or a succeeded one naming no check.
    (UNFINISHED.has(operation.state) ||
      (operation.state === 'succeeded' && checkId === null))
  )
    view = {
      step: 'error',
      message: errorCodeMessage(t, 'connection_check_failed'),
    };
  else view = { step: 'checking' };

  return { view, check };
}

function newActionId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? `connection-check:${crypto.randomUUID()}`
    : `connection-check:${Math.random().toString(36).slice(2)}`;
}
