import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useOperation } from '@/hooks/domain/useOperation';
import { useWorkspace } from './WorkspaceProvider';
import {
  DomainProblemError,
  errorMessage,
  type DomainProblem,
} from '@/lib/i18n/domain-errors';

export function MarketingCallRecovery({
  call,
  kind,
  onResolved,
}: {
  call: { id: string; state: string; maximum_cost?: string };
  kind: 'planning' | 'ai_qc';
  onResolved: () => void;
}) {
  const { t } = useTranslation('common');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const storageKey = `marketing-recovery:${current.workspaceId}:${call.id}`;
  const [operationId, setOperationId] = useState<string | null>(null);
  const [note, setNote] = useState('');
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [reason, setReason] = useState<DomainProblem | null>(null);
  const action = useRef<string | null>(null);
  const completed = useRef<string | null>(null);
  const { operation, problem } = useOperation({
    operationRecordId: operationId,
    actionInstanceId: `resolve:${kind}:${call.id}`,
    purpose: 'marketing-call-recovery',
  });
  useEffect(() => {
    try {
      setOperationId(sessionStorage.getItem(storageKey));
    } catch {
      /* Storage is optional. */
    }
  }, [storageKey]);
  useEffect(() => {
    if (
      !operationId ||
      completed.current === operationId ||
      !operation ||
      !['succeeded', 'failed', 'cancelled'].includes(operation.state)
    )
      return;
    completed.current = operationId;
    action.current = null;
    try {
      sessionStorage.removeItem(storageKey);
    } catch {
      /* Storage is optional. */
    }
    setOperationId(null);
    if (operation.state !== 'succeeded')
      setError(t('marketing-recovery-failed'));
    onResolved();
  }, [operation, operationId, onResolved, storageKey, t]);

  if (call.state !== 'uncertain') return null;
  if (current.role !== 'OWNER')
    return <p role="note">{t('marketing-recovery-owner-required')}</p>;
  async function resolve() {
    setBusy(true);
    setError('');
    setReason(null);
    action.current ??= crypto.randomUUID();
    try {
      const result = await gateway.resolveMarketingCall({
        workspaceSlug: current.slug,
        callId: call.id,
        actionInstanceId: action.current,
        body: {
          kind,
          maximum_cost: call.maximum_cost ?? '0',
          acknowledge_estimated_cost: true,
          note,
        },
      });
      if (result.kind === 'problem')
        throw new DomainProblemError(result.problem);
      if (!('data' in result) || !result.data)
        throw new Error('marketing-recovery-failed');
      const id = result.data.operation_id;
      try {
        sessionStorage.setItem(storageKey, id);
      } catch {
        /* Storage is optional. */
      }
      setOperationId(id);
    } catch (e) {
      setError(t('marketing-recovery-failed'));
      if (e instanceof DomainProblemError) setReason(e.problem);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      aria-label={t('marketing-recovery-title')}
      className="space-y-3 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm"
    >
      <h4 className="font-semibold">{t('marketing-recovery-title')}</h4>
      <p>
        {t('marketing-recovery-explanation', {
          amount: call.maximum_cost ?? '0',
        })}
      </p>
      <label className="block space-y-1">
        <span>{t('marketing-recovery-note')}</span>
        <textarea
          className="block w-full rounded border border-gray-300 bg-white p-2"
          value={note}
          maxLength={2000}
          disabled={busy || !!operationId}
          onChange={(e) => setNote(e.target.value)}
        />
      </label>
      <label className="flex items-start gap-2">
        <input
          type="checkbox"
          checked={acknowledged}
          disabled={busy || !!operationId}
          onChange={(e) => setAcknowledged(e.target.checked)}
        />
        <span>{t('marketing-recovery-acknowledge')}</span>
      </label>
      <button
        className="rounded border border-gray-400 bg-white px-3 py-2 disabled:opacity-40"
        disabled={
          busy ||
          !!operationId ||
          !acknowledged ||
          !note.trim() ||
          call.maximum_cost === undefined
        }
        onClick={() => void resolve()}
      >
        {t('marketing-recovery-close')}
      </button>
      {operationId && <p role="status">{t('marketing-recovery-pending')}</p>}
      {(error || problem) && (
        <p role="alert">
          {error || t('marketing-recovery-failed')}
          {(reason ?? problem) && (
            <span className="block">{errorMessage(t, reason ?? problem)}</span>
          )}
        </p>
      )}
    </section>
  );
}
