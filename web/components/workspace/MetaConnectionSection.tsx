import { useState, useCallback } from 'react';
import { useTranslation } from 'next-i18next';
import { Card } from '@/components/shared';
import { newActionId } from '@/components/generation/shared/newActionId';
import {
  ModeProvenance,
  type ProvenanceViewModel,
} from '@/components/workspace/ModeProvenance';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

const META_MODE = 'customer_sandbox';

/** Failure banners the section shows: `settings:meta-connection.<key>`. */
type FailureMessage =
  | 'list-unavailable'
  | 'create-refused'
  | 'create-unresolved';

interface ConnectionProblemProps {
  message: FailureMessage;
  problem?: ProblemDetails;
  testId: string;
}

// Code and trace id make a refused request traceable in the web/api logs.
function ConnectionProblem({
  message,
  problem,
  testId,
}: ConnectionProblemProps) {
  const { t } = useTranslation('settings');
  const reference = problem
    ? [problem.code, problem.trace_id && t('trace', { id: problem.trace_id })]
        .filter(Boolean)
        .join(' · ')
    : '';
  return (
    <div
      className="alert alert-error items-start text-sm"
      role="alert"
      data-testid={testId}
    >
      <div>
        <div className="font-semibold">{t(`meta-connection.${message}`)}</div>
        {reference && (
          <div className="mt-1 text-xs opacity-75">{reference}</div>
        )}
      </div>
    </div>
  );
}

function modeToProvenance(mode: string): ProvenanceViewModel {
  if (mode === 'customer_sandbox') return { mode: 'sandbox' };
  if (mode === 'controlled_mock') return { mode: 'controlled-mock' };
  return { mode: 'sandbox' };
}

export function MetaConnectionSection() {
  const { t } = useTranslation('settings');
  const [showCreate, setShowCreate] = useState(false);
  const [selectedMode, setSelectedMode] = useState<string>(META_MODE);
  const [createFailure, setCreateFailure] = useState<{
    message: FailureMessage;
    problem?: ProblemDetails;
  } | null>(null);

  const {
    data: listData,
    problem: listProblem,
    isLoading,
    refresh,
  } = useDomainQuery('list_meta_connections', { meta_mode: META_MODE });

  const { execute, isPending } = useDomainCommand('create_meta_connection');

  const activeConnection =
    listData && 'items' in (listData as any)
      ? (listData as any).items?.find(
          (c: { state: string }) => c.state === 'active'
        )
      : null;

  const handleCreate = useCallback(async () => {
    setCreateFailure(null);
    const outcome = await execute({
      // Not crypto.randomUUID(): browsers expose it only in secure contexts,
      // and a Workspace served over plain HTTP from a non-localhost host (the
      // golden-path topology's http://web:4002) is not one, so the click threw
      // before any request was sent.
      actionInstanceId: newActionId(),
      body: { meta_mode: selectedMode },
    }).catch(() => null);

    if (outcome === null || outcome.kind === 'reconciliation') {
      // No definitive answer (no response at all, or an intent past its safe
      // retry window): the connection may exist, so re-read before a retry.
      setCreateFailure({ message: 'create-unresolved' });
      await refresh();
      return;
    }
    if (outcome.kind === 'problem') {
      setCreateFailure({
        message: 'create-refused',
        problem: outcome.problem,
      });
      return;
    }
    setShowCreate(false);
    await refresh();
  }, [execute, selectedMode, refresh]);

  return (
    <div data-testid="meta-connection-section">
      <Card>
        <Card.Body>
          <Card.Header>
            <Card.Title>{t('meta-connection.title')}</Card.Title>
            <Card.Description>
              {t('meta-connection.description')}
            </Card.Description>
          </Card.Header>

          {isLoading && (
            <p className="text-sm text-gray-500">
              {t('meta-connection.loading')}
            </p>
          )}

          {!isLoading && listProblem && !activeConnection && (
            <div className="flex flex-col items-start gap-2">
              <ConnectionProblem
                message="list-unavailable"
                problem={listProblem}
                testId="meta-connection-list-problem"
              />
              <button
                type="button"
                className="btn btn-outline btn-sm"
                onClick={() => void refresh()}
              >
                {t('meta-connection.retry')}
              </button>
            </div>
          )}

          {activeConnection && (
            <div className="flex flex-col gap-3">
              <div
                className="flex items-center gap-2"
                data-testid="meta-connection-status-active"
              >
                <span className="badge badge-sm badge-success">
                  {t('meta-connection.active')}
                </span>
                <ModeProvenance
                  provenance={modeToProvenance(activeConnection.meta_mode)}
                />
              </div>
              <dl className="grid grid-cols-2 gap-2 text-sm">
                <dt className="text-gray-500">
                  {t('meta-connection.connection-id')}
                </dt>
                <dd className="font-mono text-xs">{activeConnection.id}</dd>
                <dt className="text-gray-500">
                  {t('meta-connection.ad-account')}
                </dt>
                <dd className="font-mono text-xs">
                  {activeConnection.ad_account_id}
                </dd>
              </dl>
            </div>
          )}

          {!isLoading && !listProblem && !activeConnection && !showCreate && (
            <button
              type="button"
              data-testid="create-meta-connection"
              className="btn btn-primary btn-sm"
              onClick={() => setShowCreate(true)}
            >
              {t('meta-connection.create')}
            </button>
          )}

          {showCreate && !activeConnection && (
            <div className="flex flex-col gap-3">
              <label className="text-sm font-medium" htmlFor="meta-mode-select">
                {t('meta-connection.mode')}
              </label>
              <select
                id="meta-mode-select"
                data-testid="meta-connection-mode-select"
                className="select select-bordered select-sm w-full max-w-xs"
                value={selectedMode}
                onChange={(e) => setSelectedMode(e.target.value)}
              >
                <option value="customer_sandbox">
                  {t('meta-connection.mode-customer_sandbox')}
                </option>
              </select>
              {createFailure && (
                <ConnectionProblem
                  message={createFailure.message}
                  problem={createFailure.problem}
                  testId="meta-connection-create-problem"
                />
              )}
              <div className="flex gap-2">
                <button
                  type="button"
                  data-testid="confirm-meta-connection"
                  className="btn btn-primary btn-sm"
                  disabled={isPending}
                  onClick={handleCreate}
                >
                  {isPending
                    ? t('meta-connection.confirming')
                    : t('meta-connection.confirm')}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => {
                    setShowCreate(false);
                    setCreateFailure(null);
                  }}
                >
                  {t('meta-connection.cancel')}
                </button>
              </div>
            </div>
          )}
        </Card.Body>
      </Card>
    </div>
  );
}
