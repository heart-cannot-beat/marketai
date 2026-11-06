import { useCallback, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { DomainProblemError, errorMessage } from '@/lib/i18n/domain-errors';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useLibraryCache } from './useLibraryCache';

export type DimensionDiffKind =
  | 'unchanged'
  | 'template_only'
  | 'template_removed'
  | 'locally_removed'
  | 'local_only'
  | 'conflict';

export interface DimensionDiffItem {
  dimensionId: string;
  name: string;
  kind: DimensionDiffKind;
  templateValues: string[];
  localValues: string[];
  resolution: 'accept_template' | 'keep_local' | null;
  templateValue?: Record<string, unknown> | null;
  localValue?: Record<string, unknown> | null;
}

export type ReapplyState =
  | 'idle'
  | 'loading'
  | 'diffing'
  | 'applying'
  | 'complete'
  | 'error'
  | 'stale';

export interface UseTemplateReapplyResult {
  state: ReapplyState;
  diff: DimensionDiffItem[];
  error: string | null;
  loadDiff: (templateVersionId: string, schemaEntryId: string) => Promise<void>;
  setResolution: (
    dimensionId: string,
    resolution: 'accept_template' | 'keep_local'
  ) => void;
  apply: (schemaEntryId: string) => Promise<void>;
  reset: () => void;
}

export function useTemplateReapply(): UseTemplateReapplyResult {
  const { t } = useTranslation('common');
  const message = useCallback(
    (err: unknown) =>
      err instanceof DomainProblemError
        ? errorMessage(t, err.problem)
        : (err as Error).message,
    [t]
  );
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const { invalidate } = useLibraryCache();
  const [state, setState] = useState<ReapplyState>('idle');
  const [diff, setDiff] = useState<DimensionDiffItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  const loadDiff = useCallback(
    async (templateVersionId: string, schemaEntryId: string) => {
      setState('loading');
      setError(null);

      try {
        const result = await gateway.previewLibraryTemplateReapply({
          workspaceSlug: current.slug,
          actionInstanceId: newActionId('template-preview'),
          templateVersionId,
          body: { existing_schema_version_id: schemaEntryId },
        });

        if (result.kind === 'reconciliation_required') {
          setState('stale');
          setError('Template preview outcome requires reconciliation');
          return;
        }

        if (result.kind === 'problem') {
          throw new DomainProblemError(result.problem);
        }

        const items = result.data.items ?? [];
        setDiff(items.map(toDimensionDiffItem));
        setState('diffing');
      } catch (err) {
        setState('error');
        setError(message(err));
      }
    },
    [gateway, current.slug, message]
  );

  const setResolution = useCallback(
    (dimensionId: string, resolution: 'accept_template' | 'keep_local') => {
      setDiff((prev) =>
        prev.map((d) =>
          d.dimensionId === dimensionId ? { ...d, resolution } : d
        )
      );
    },
    []
  );

  const apply = useCallback(
    async (schemaEntryId: string) => {
      const unresolved = diff.filter(
        (d) => d.kind === 'conflict' && d.resolution === null
      );
      if (unresolved.length > 0) {
        setError('All conflicts must be resolved before applying');
        return;
      }

      setState('applying');
      setError(null);

      try {
        const resolvedDimensions = diff
          .map(resolveDimension)
          .filter(
            (dimension): dimension is Record<string, unknown> =>
              dimension !== null
          );

        const result = await gateway.saveLibraryDraft({
          workspaceSlug: current.slug,
          actionInstanceId: newActionId('template-apply'),
          entryId: schemaEntryId,
          body: { content: { dimensions: resolvedDimensions } },
        });

        if (result.kind === 'reconciliation_required') {
          setState('stale');
          setError('Template reapply outcome requires reconciliation');
          return;
        }

        if (result.kind === 'problem') {
          throw new DomainProblemError(result.problem);
        }

        setState('complete');
        await invalidate({ resources: ['entry', 'events', 'internalEntries'] });
      } catch (err) {
        setState('error');
        setError(message(err));
      }
    },
    [gateway, current.slug, diff, invalidate, message]
  );

  const reset = useCallback(() => {
    setState('idle');
    setDiff([]);
    setError(null);
  }, []);

  return { state, diff, error, loadDiff, setResolution, apply, reset };
}

function newActionId(prefix: string): string {
  const suffix =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2);
  return `${prefix}:${suffix}`;
}

function displayValues(value: Record<string, unknown> | null): string[] {
  if (!value || !Array.isArray(value.values)) return [];
  return value.values.map((item) => {
    if (typeof item === 'string') return item;
    if (item && typeof item === 'object') {
      const record = item as Record<string, unknown>;
      return String(record.name ?? record.label ?? record.id ?? '');
    }
    return String(item);
  });
}

function toDimensionDiffItem(raw: Record<string, unknown>): DimensionDiffItem {
  const templateValue =
    raw.template_value && typeof raw.template_value === 'object'
      ? (raw.template_value as Record<string, unknown>)
      : null;
  const localValue =
    raw.local_value && typeof raw.local_value === 'object'
      ? (raw.local_value as Record<string, unknown>)
      : null;
  const dimensionId = String(raw.dimension_id ?? '');

  return {
    dimensionId,
    name: String(templateValue?.name ?? localValue?.name ?? dimensionId),
    kind: raw.status as DimensionDiffKind,
    templateValues: displayValues(templateValue),
    localValues: displayValues(localValue),
    resolution: null,
    templateValue,
    localValue,
  };
}

function resolveDimension(
  item: DimensionDiffItem
): Record<string, unknown> | null {
  if (item.kind === 'locally_removed' || item.kind === 'template_removed') {
    return null;
  }
  if (item.kind === 'local_only') return item.localValue ?? null;
  if (item.kind === 'conflict') {
    return item.resolution === 'accept_template'
      ? (item.templateValue ?? null)
      : (item.localValue ?? null);
  }
  return item.templateValue ?? item.localValue ?? null;
}
