import { useCallback, useEffect, useMemo, useState } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useOperation } from '@/hooks/domain/useOperation';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

type ExportManifest = components['schemas']['ExportManifest'];
type ExportPackageDetail = components['schemas']['ExportPackageDetail'];

export type ExportStep = 'configure' | 'processing' | 'ready' | 'error';

export type ExportSelector = components['schemas']['ExportSelectorInput'];

export interface UseExportFlowOptions {
  initialPackageId?: string;
}

export interface UseExportFlowResult {
  step: ExportStep;
  packageId: string | undefined;
  operationId: string | undefined;
  packageDetail: ExportPackageDetail | undefined;
  manifest: ExportManifest | undefined;
  problem: ProblemDetails | undefined;
  isExporting: boolean;
  startExport: (selector: ExportSelector) => Promise<void>;
  reset: () => void;
}

export function useExportFlow(
  options?: UseExportFlowOptions
): UseExportFlowResult {
  const exportCommand = useDomainCommand('create_generation_export_package');
  const [packageId, setPackageId] = useState<string | undefined>(
    options?.initialPackageId
  );
  const [operationId, setOperationId] = useState<string | undefined>();
  const [problem, setProblem] = useState<ProblemDetails>();
  const [isExporting, setIsExporting] = useState(false);

  const packageQuery = useDomainQuery(
    'get_generation_export_package',
    packageId ? ({ export_package_id: packageId } as any) : undefined,
    { enabled: !!packageId }
  );
  const packageDetail = packageQuery.data as ExportPackageDetail | undefined;
  const packageState = packageDetail?.state;
  const refreshPackage = packageQuery.refresh;
  const trackedOperationId = operationId ?? packageDetail?.operation_id ?? null;
  const operation = useOperation({
    operationRecordId: trackedOperationId,
    purpose: 'export',
    actionInstanceId: `export:${packageId ?? 'pending'}`,
  });

  const manifestQuery = useDomainQuery(
    'get_generation_export_manifest',
    packageId ? ({ export_package_id: packageId } as any) : undefined,
    { enabled: !!packageId && packageDetail?.state === 'ready' }
  );

  useEffect(() => {
    if (
      !packageId ||
      packageState === 'ready' ||
      packageState === 'failed' ||
      packageState === 'expired'
    ) {
      return;
    }

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const pollPackage = async () => {
      await refreshPackage();
      if (!cancelled) timer = setTimeout(pollPackage, 2000);
    };
    timer = setTimeout(pollPackage, operation.isTerminal ? 0 : 1000);

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [operation.isTerminal, packageId, packageState, refreshPackage]);

  const step = useMemo<ExportStep>(() => {
    if (!packageId) return 'configure';
    if (
      problem ||
      packageQuery.problem ||
      operation.problem ||
      manifestQuery.problem ||
      packageDetail?.state === 'failed' ||
      packageDetail?.state === 'expired' ||
      operation.operation?.state === 'failed' ||
      operation.operation?.state === 'cancelled'
    ) {
      return 'error';
    }
    if (packageDetail?.state === 'ready' && manifestQuery.data) return 'ready';
    return 'processing';
  }, [
    packageId,
    problem,
    packageQuery.problem,
    packageDetail?.state,
    operation.problem,
    operation.operation?.state,
    manifestQuery.problem,
    manifestQuery.data,
  ]);

  const startExport = useCallback(
    async (selector: ExportSelector) => {
      setProblem(undefined);
      setIsExporting(true);

      const actionInstanceId = `export:${Date.now()}`;
      const outcome = await exportCommand.execute({
        body: { selector } as any,
        actionInstanceId,
      });

      setIsExporting(false);

      if (outcome.kind === 'problem') {
        setProblem(outcome.problem);
        return;
      }

      if (outcome.kind === 'success') {
        const data = outcome.data as any;
        setPackageId(data.package_id);
        setOperationId(data.operation_id);
      }
    },
    [exportCommand]
  );

  const reset = useCallback(() => {
    setPackageId(undefined);
    setOperationId(undefined);
    setProblem(undefined);
    exportCommand.reset();
  }, [exportCommand]);

  return {
    step,
    packageId,
    operationId: trackedOperationId ?? undefined,
    packageDetail,
    manifest: manifestQuery.data as ExportManifest | undefined,
    problem:
      problem ??
      packageQuery.problem ??
      operation.problem ??
      manifestQuery.problem,
    isExporting,
    startExport,
    reset,
  };
}
