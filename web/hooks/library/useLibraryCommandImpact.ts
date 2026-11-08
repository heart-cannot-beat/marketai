import { useCallback, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import {
  toLibraryFinding,
  type LibraryFinding,
} from '@/lib/library/validation-findings';

type CommandImpactData = components['schemas']['CommandImpactProjection'];

export type ImpactKind = 'publish' | 'withdraw' | 'archive';

export interface ImpactAffectedSummary {
  id: string;
  display_name: string;
  entry_type: string;
  effect: string;
}

export interface ImpactProjection {
  impactId: string;
  impactHash: string;
  commandKind: ImpactKind;
  targetId: string;
  targetRevision: number;
  contentHash: string | null;
  expiry: number;
  affectedSummaries: ImpactAffectedSummary[];
  /** Validation findings that stop the command; worded by `FindingText`. */
  blocking: LibraryFinding[];
  /**
   * Non-blocking validation warnings. Each `code` is what FastAPI expects
   * back in `acknowledged_warnings`.
   */
  warnings: LibraryFinding[];
}

export interface UseLibraryCommandImpactResult {
  impact: ImpactProjection | null;
  isLoading: boolean;
  problem: ProblemDetails | undefined;
  isStale: boolean;
  fetch: (kind: ImpactKind, targetId: string) => Promise<void>;
  clear: () => void;
  confirmed: boolean;
  confirm: () => void;
  resetConfirmation: () => void;
}

export function useLibraryCommandImpact(): UseLibraryCommandImpactResult {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();

  const [impact, setImpact] = useState<ImpactProjection | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [problem, setProblem] = useState<ProblemDetails | undefined>();
  const [isStale, setIsStale] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const fetchKeyRef = useRef('');

  const fetch = useCallback(
    async (kind: ImpactKind, targetId: string): Promise<void> => {
      const key = `${kind}:${targetId}`;
      fetchKeyRef.current = key;
      setIsLoading(true);
      setProblem(undefined);
      setConfirmed(false);
      setIsStale(false);

      try {
        const actionInstanceId = newActionId();
        const result =
          kind === 'publish'
            ? await gateway.previewLibraryPublishImpact({
                workspaceSlug: current.slug,
                actionInstanceId,
                entryId: targetId,
              })
            : kind === 'archive'
              ? await gateway.previewLibraryArchiveImpact({
                  workspaceSlug: current.slug,
                  actionInstanceId,
                  entryId: targetId,
                })
              : await gateway.previewLibraryWithdrawImpact({
                  workspaceSlug: current.slug,
                  actionInstanceId,
                  versionId: targetId,
                });

        if (fetchKeyRef.current !== key) return;

        if (result.kind === 'reconciliation_required') {
          setProblem(
            localProblem(
              'impact_reconciliation_required',
              'The impact preview outcome is uncertain. Refresh it before confirming.'
            )
          );
          setImpact(null);
          setIsStale(true);
        } else if (result.kind === 'problem') {
          setProblem(result.problem);
          setImpact(null);
        } else {
          setImpact(toImpactProjection(kind, targetId, result.data));
          setProblem(undefined);
        }
      } catch {
        if (fetchKeyRef.current !== key) return;
        setProblem({
          type: 'network_error',
          title: 'Network error',
          status: 0,
          detail: 'Failed to fetch impact projection',
          instance: '',
          code: 'network_error',
          trace_id: '',
          retryable: true,
          field_errors: [],
        });
        setImpact(null);
      } finally {
        if (fetchKeyRef.current === key) {
          setIsLoading(false);
        }
      }
    },
    [gateway, current.slug]
  );

  const clear = useCallback(() => {
    fetchKeyRef.current = '';
    setImpact(null);
    setProblem(undefined);
    setConfirmed(false);
    setIsStale(false);
  }, []);

  const confirm = useCallback(() => {
    if (impact && !isStale) {
      setConfirmed(true);
    }
  }, [impact, isStale]);

  const resetConfirmation = useCallback(() => {
    setConfirmed(false);
    setIsStale(true);
  }, []);

  return {
    impact,
    isLoading,
    problem,
    isStale,
    fetch,
    clear,
    confirmed,
    confirm,
    resetConfirmation,
  };
}

function newActionId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `impact_${Math.random().toString(36).slice(2)}`;
}

function localProblem(code: string, detail: string): ProblemDetails {
  return {
    type: 'client_error',
    title: code,
    status: 0,
    detail,
    instance: '',
    code,
    trace_id: '',
    retryable: true,
    field_errors: [],
  };
}

function toImpactProjection(
  kind: ImpactKind,
  targetId: string,
  data: CommandImpactData
): ImpactProjection {
  const projection = data.projection ?? {};
  const dependents = Array.isArray(projection.dependents)
    ? projection.dependents
    : [];
  const blocking = Array.isArray(projection.blocking)
    ? projection.blocking
    : [];
  const warnings = Array.isArray(projection.warnings)
    ? projection.warnings
    : [];

  return {
    impactId: data.impact_id,
    impactHash: data.impact_hash,
    commandKind: kind,
    targetId,
    targetRevision: data.target_revision ?? 0,
    contentHash: data.content_hash || null,
    expiry: Date.parse(data.expires_at),
    affectedSummaries: dependents.map((value, index) => {
      const dependent = asRecord(value);
      const entryId = stringValue(dependent.entry_id, `dependent-${index}`);
      return {
        id: stringValue(dependent.version_id, entryId),
        display_name: stringValue(dependent.display_name, entryId),
        entry_type: stringValue(dependent.entry_type, 'unknown'),
        effect:
          dependent.will_block === true
            ? 'blocked'
            : stringValue(dependent.eligibility, 'affected'),
      };
    }),
    blocking: blocking.map(toLibraryFinding),
    warnings: warnings.map(toLibraryFinding),
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object'
    ? (value as Record<string, unknown>)
    : {};
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === 'string' && value.length > 0 ? value : fallback;
}
