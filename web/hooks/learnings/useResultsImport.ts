import { useCallback, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { DomainGateway } from '@/lib/domain-client/gateway';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { useUpload } from '@/hooks/domain/useUpload';
import { newActionId } from '@/components/generation/shared/newActionId';
import { decodeSheet, INLINE_CSV_LIMIT } from '@/lib/learnings/sheet';
import type { DomainProblem } from '@/lib/i18n/domain-errors';

export type ResultsImportView = components['schemas']['ResultsImportView'];

export type ImportPhase =
  | 'idle'
  | 'reading'
  | 'uploading'
  | 'previewing'
  | 'preview'
  | 'committing'
  | 'committed';

/** A refusal the dialog explains by its stable `code`; `problem` is the
 * domain's refusal (with its `params`), never shown as server text (#253). */
export interface ImportProblem {
  code: string;
  problem: DomainProblem | null;
}

export interface UseResultsImportResult {
  phase: ImportPhase;
  preview: ResultsImportView | null;
  problem: ImportProblem | null;
  /** Pasted table text (TSV from a spreadsheet or CSV). */
  previewText: (text: string, currency: string | null) => Promise<void>;
  /** A picked file: decoded in the browser (UTF-8 / GB18030). */
  previewFile: (file: File, currency: string | null) => Promise<void>;
  /** The ad metrics already synced from Meta for ads bound to this product. */
  previewMetaSync: () => Promise<void>;
  commit: () => Promise<ResultsImportView | null>;
  reset: () => void;
}

const OPERATION_POLL_MS = 800;
const OPERATION_POLL_LIMIT = 60;

async function waitForUpload(
  gateway: DomainGateway,
  workspaceSlug: string,
  operationId: string
): Promise<string> {
  for (let attempt = 0; attempt < OPERATION_POLL_LIMIT; attempt++) {
    const result = await gateway.pollOperation({ workspaceSlug, operationId });
    if (result.kind === 'ok') {
      const state = String((result.data as { state?: string }).state ?? '');
      if (['succeeded', 'failed', 'cancelled'].includes(state)) return state;
    }
    await new Promise((resolve) => setTimeout(resolve, OPERATION_POLL_MS));
  }
  return 'timeout';
}

type CreateBody = components['schemas']['ResultsImportCreateRequest'];

/**
 * Import ad results for one product (Issue #166): read or paste a sheet,
 * get the server's match preview, then commit. A sheet above
 * {@link INLINE_CSV_LIMIT} characters is re-encoded as UTF-8 and uploaded
 * (`results_import`); smaller ones go inline. The server decides what
 * matches and what counts -- this hook only carries the user's input.
 */
export function useResultsImport(productId: string): UseResultsImportResult {
  const { current } = useWorkspace();
  const slug = current.slug;
  const gateway = useDomainGateway();
  const { startUpload } = useUpload('results_import');
  const [phase, setPhase] = useState<ImportPhase>('idle');
  const [preview, setPreview] = useState<ResultsImportView | null>(null);
  const [problem, setProblem] = useState<ImportProblem | null>(null);
  const session = useRef(0);

  const create = useCallback(
    async (body: Omit<CreateBody, 'product_id'>, mine: number) => {
      setPhase('previewing');
      const result = await gateway.createResultsImport({
        workspaceSlug: slug,
        actionInstanceId: newActionId(),
        body: { product_id: productId, ...body } as CreateBody,
      });
      if (mine !== session.current) return;
      if (result.kind === 'created') {
        setPreview(result.data);
        setPhase(result.data.status === 'committed' ? 'committed' : 'preview');
        return;
      }
      setPhase('idle');
      setProblem(
        result.kind === 'problem'
          ? { code: result.problem.code, problem: result.problem }
          : { code: 'unconfirmed', problem: null }
      );
    },
    [gateway, productId, slug]
  );

  const previewText = useCallback(
    async (text: string, currency: string | null) => {
      const mine = ++session.current;
      setProblem(null);
      const trimmed = text.trim();
      if (!trimmed) {
        setProblem({ code: 'empty_input', problem: null });
        return;
      }
      if (trimmed.length > INLINE_CSV_LIMIT) {
        setProblem({ code: 'paste_too_large', problem: null });
        return;
      }
      await create({ source: 'csv', csv_text: trimmed, currency }, mine);
    },
    [create]
  );

  const previewFile = useCallback(
    async (file: File, currency: string | null) => {
      const mine = ++session.current;
      setProblem(null);
      setPhase('reading');
      const text = decodeSheet(await file.arrayBuffer());
      if (mine !== session.current) return;
      if (text === null) {
        setPhase('idle');
        setProblem({ code: 'file_encoding_invalid', problem: null });
        return;
      }
      if (text.length <= INLINE_CSV_LIMIT) {
        await create(
          { source: 'csv', csv_text: text, currency, file_name: file.name },
          mine
        );
        return;
      }
      setPhase('uploading');
      const utf8 = new File(
        [text],
        file.name.replace(/\.[^.]*$/, '') + '.csv',
        {
          type: 'text/csv',
        }
      );
      const uploaded = await startUpload({
        file: utf8,
        ownerKind: 'results_import',
        ownerId: productId,
        actionInstanceId: newActionId(),
      });
      if (mine !== session.current) return;
      if (uploaded.kind !== 'accepted') {
        setPhase('idle');
        setProblem({
          code:
            uploaded.kind === 'problem'
              ? uploaded.problem.code
              : 'upload_failed',
          problem: uploaded.kind === 'problem' ? uploaded.problem : null,
        });
        return;
      }
      const state = await waitForUpload(
        gateway,
        slug,
        uploaded.operationRecordId
      );
      if (mine !== session.current) return;
      if (state !== 'succeeded') {
        setPhase('idle');
        setProblem({ code: 'upload_rejected', problem: null });
        return;
      }
      await create(
        {
          source: 'csv',
          upload_intent_id: uploaded.uploadIntentId,
          currency,
          file_name: file.name,
        },
        mine
      );
    },
    [create, gateway, productId, slug, startUpload]
  );

  const previewMetaSync = useCallback(async () => {
    const mine = ++session.current;
    setProblem(null);
    await create({ source: 'meta_sync' }, mine);
  }, [create]);

  const commit = useCallback(async () => {
    if (!preview) return null;
    const mine = ++session.current;
    setProblem(null);
    setPhase('committing');
    const result = await gateway.commitResultsImport({
      workspaceSlug: slug,
      actionInstanceId: newActionId(),
      importId: preview.import_id,
    });
    if (mine !== session.current) return null;
    if (result.kind === 'ok') {
      setPreview(result.data);
      setPhase('committed');
      return result.data;
    }
    if (result.kind === 'problem' && result.problem.code === 'preview_stale') {
      // The server refreshed the preview; show it and ask again.
      const fresh = await gateway.getResultsImport({
        workspaceSlug: slug,
        importId: preview.import_id,
      });
      if (fresh.kind === 'ok') setPreview(fresh.data);
    }
    setPhase('preview');
    setProblem(
      result.kind === 'problem'
        ? { code: result.problem.code, problem: result.problem }
        : { code: 'unconfirmed', problem: null }
    );
    return null;
  }, [gateway, preview, slug]);

  const reset = useCallback(() => {
    session.current++;
    setPhase('idle');
    setPreview(null);
    setProblem(null);
  }, []);

  return {
    phase,
    preview,
    problem,
    previewText,
    previewFile,
    previewMetaSync,
    commit,
    reset,
  };
}
