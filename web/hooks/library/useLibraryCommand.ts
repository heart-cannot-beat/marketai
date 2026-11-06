import { useCallback, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import {
  useDomainCommand,
  type CommandOperationId,
  type ExecuteParams,
} from '@/hooks/domain/useDomainCommand';
import type {
  CommandOutcome,
  ProblemDetails,
  ResponseMeta,
} from '@/lib/domain-client/result-types';
import type { ReconciliationRequired } from '@/lib/domain-client/intent';
import {
  type LibraryCommandKind,
  getDefinition,
  stableParamHash,
} from './command-definitions';

type RunParams = Record<string, unknown>;

const EMPTY_META: ResponseMeta = { etag: null, retryAt: null, location: null };

function newActionId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `action_${Math.random().toString(36).slice(2)}`;
}

function localProblem(
  code: string,
  detail: string
): CommandOutcome<CommandOperationId> {
  return {
    kind: 'problem',
    problem: {
      type: 'client_error',
      title: code,
      status: 0,
      detail,
      instance: '',
      code,
      trace_id: '',
      retryable: false,
      field_errors: [],
    },
    meta: EMPTY_META,
  };
}

export interface UseLibraryCommandResult {
  run: (params: RunParams) => Promise<CommandOutcome<CommandOperationId>>;
  retry: () => Promise<CommandOutcome<CommandOperationId>>;
  reset: () => void;
  dismiss: () => void;
  isPending: boolean;
  problem: ProblemDetails | undefined;
  reconciliation: ReconciliationRequired | undefined;
}

type PendingState = {
  actionInstanceId: string;
  paramHash: string;
  promise: Promise<CommandOutcome<CommandOperationId>>;
};

export function useLibraryCommand(
  commandKind: LibraryCommandKind
): UseLibraryCommandResult {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const def = getDefinition(commandKind);
  const domainCmd = useDomainCommand(def.operationId);

  const pendingRef = useRef<PendingState | null>(null);
  const [dismissed, setDismissed] = useState(false);

  const buildExecuteParams = useCallback(
    (
      actionInstanceId: string,
      params: RunParams
    ): ExecuteParams<CommandOperationId> => {
      const pathParams: Record<string, string> = {};
      if (def.targetField && params[def.targetField] !== undefined) {
        pathParams[def.targetField] = String(params[def.targetField]);
      }

      const { body, ifMatch } = params as {
        body?: unknown;
        ifMatch?: string | number;
        [k: string]: unknown;
      };

      const hasPathParams = Object.keys(pathParams).length > 0;
      return {
        actionInstanceId,
        ...(hasPathParams ? { pathParams } : {}),
        ...(body !== undefined ? { body } : {}),
        ...(ifMatch !== undefined ? { ifMatch } : {}),
      } as ExecuteParams<CommandOperationId>;
    },
    [def.targetField]
  );

  const run = useCallback(
    async (params: RunParams): Promise<CommandOutcome<CommandOperationId>> => {
      const hash = stableParamHash(params);

      if (pendingRef.current) {
        if (pendingRef.current.paramHash === hash) {
          return pendingRef.current.promise;
        }
        return localProblem(
          'command_already_pending',
          `A ${commandKind} command is already in flight with different parameters.`
        );
      }

      const actionInstanceId = newActionId();
      const executeParams = buildExecuteParams(actionInstanceId, params);

      const promise = domainCmd.execute(executeParams).then((outcome) => {
        pendingRef.current = null;

        if (outcome.kind === 'reconciliation') {
          return refreshOutcome(actionInstanceId, outcome);
        }
        return outcome;
      });

      pendingRef.current = { actionInstanceId, paramHash: hash, promise };
      setDismissed(false);
      return promise;
    },
    [commandKind, domainCmd, buildExecuteParams] // eslint-disable-line react-hooks/exhaustive-deps
  );

  const refreshOutcome = useCallback(
    async (
      actionInstanceId: string,
      fallback: CommandOutcome<CommandOperationId>
    ): Promise<CommandOutcome<CommandOperationId>> => {
      const lookupResult = await gateway.getLibraryCommandOutcome({
        workspaceSlug: current.slug,
        commandIdentity: actionInstanceId,
        commandKind,
      });

      if (lookupResult.kind === 'problem') {
        if (lookupResult.status === 404) {
          return fallback;
        }
        return {
          kind: 'problem',
          problem: lookupResult.problem,
          meta: lookupResult.meta,
        };
      }

      return {
        kind: 'success' as const,
        data: lookupResult.data,
        meta: lookupResult.meta,
      } as CommandOutcome<CommandOperationId>;
    },
    [gateway, current.slug, commandKind]
  );

  const retry = useCallback(async (): Promise<
    CommandOutcome<CommandOperationId>
  > => {
    return domainCmd.retry();
  }, [domainCmd]);

  const reset = useCallback(() => {
    domainCmd.reset();
    pendingRef.current = null;
    setDismissed(false);
  }, [domainCmd]);

  const dismiss = useCallback(() => {
    setDismissed(true);
  }, []);

  return {
    run,
    retry,
    reset,
    dismiss,
    isPending: domainCmd.isPending,
    problem: domainCmd.problem,
    reconciliation: dismissed ? undefined : domainCmd.reconciliation,
  };
}
