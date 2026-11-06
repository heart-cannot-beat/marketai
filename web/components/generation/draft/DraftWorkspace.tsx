import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { DraftComposer, type DraftComposerDraft } from './DraftComposer';
import { ProductName } from '@/components/products/ProductName';
import { newActionId } from '../shared/newActionId';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface DraftWorkspaceProps {
  draftId: string;
  previewId: string | null;
  children?: ReactNode;
  /**
   * Called with the new preview's id once `create_generation_preview`
   * succeeds. Navigation (updating the page's `?preview=` query param) is
   * the page shell's responsibility, not this component's -- `DraftWorkspace`
   * deliberately never calls `useRouter()` itself, so it can be rendered (and
   * tested) without a Next.js router context.
   */
  onPreviewCreated?: (previewId: string) => void;
}

/**
 * Root component for the draft detail page (spec: Task 5). Loads the draft
 * via `get_generation_draft`, renders the CAS-editing `DraftComposer`, and
 * exposes a `children` slot for the preview panel a later task wires in.
 */
export function DraftWorkspace({
  draftId,
  previewId,
  children,
  onPreviewCreated,
}: DraftWorkspaceProps) {
  const { t } = useTranslation('common');
  const {
    data: draft,
    isLoading,
    problem,
    refresh,
  } = useDomainQuery(
    'get_generation_draft',
    // `QueryParams<'get_generation_draft'>` intersects `pathParams` with a
    // `Record<string, never>` `queryParams` (this operation has none), which
    // collapses `draft_id` to `never` -- the same cast `LibraryEntryDetail`
    // uses for the identical `get_library_entry` shape.
    { draft_id: draftId } as any
  );
  const { execute, isPending } = useDomainCommand('create_generation_preview');
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [draftMutationPending, setDraftMutationPending] = useState(false);
  const latestDraftRevisionRef = useRef<number | null>(null);
  const mutationCompletionRef = useRef<Promise<void>>(Promise.resolve());
  const resolveMutationRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    if (draft?.revision !== undefined) {
      latestDraftRevisionRef.current = draft.revision;
    }
  }, [draft?.revision]);

  const handleDraftMutated = useCallback(
    async (revision?: number) => {
      if (revision !== undefined) {
        latestDraftRevisionRef.current = revision;
      }
      await refresh();
    },
    [refresh]
  );

  const handleMutationPendingChange = useCallback((pending: boolean) => {
    setDraftMutationPending(pending);
    if (pending) {
      mutationCompletionRef.current = new Promise<void>((resolve) => {
        resolveMutationRef.current = resolve;
      });
      return;
    }
    resolveMutationRef.current?.();
    resolveMutationRef.current = null;
  }, []);

  const canPreview =
    draft?.allowed_actions?.some(
      (action) => action.action === 'preview' && action.allowed
    ) ?? false;

  async function handleCreatePreview() {
    if (!draft) return;
    // React may batch the child mutation's pending-state render with its
    // completion. The promise/ref pair is updated synchronously, so an
    // immediate click still waits for the PATCH and uses its returned CAS
    // revision instead of racing with stale rendered data.
    await mutationCompletionRef.current;
    setPreviewError(null);
    const outcome = await execute({
      pathParams: { draft_id: draftId },
      actionInstanceId: newActionId(),
      ifMatch: latestDraftRevisionRef.current ?? draft.revision,
    });

    if (outcome.kind === 'success') {
      onPreviewCreated?.(outcome.data.id);
    } else if (outcome.kind === 'problem') {
      setPreviewError(errorMessage(t, outcome.problem));
    }
  }

  if (isLoading) {
    return (
      <div className="flex justify-center mt-12" role="status">
        <span className="loading loading-spinner loading-lg" />
      </div>
    );
  }

  if (!draft) {
    return (
      <div className="mt-8 text-center text-base-content/60">
        {problem
          ? t('generation-draft-load-error')
          : t('generation-draft-not-found')}
      </div>
    );
  }

  // `GenerationDraftDetail` (`lib/domain-client/generated/domain-v1.d.ts`)
  // has no `title` field, only `product_id` -- `DraftComposerDraft`'s
  // `title` is an optional extension a future list/summary view may attach.
  // Without it the heading is the product's name, never the `prod_` id.
  const explicitTitle = (draft as DraftComposerDraft).title;
  const title = explicitTitle ?? <ProductName productId={draft.product_id} />;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold outline-none">{title}</h1>
        {!previewId && canPreview && (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={isPending || draftMutationPending}
            onClick={handleCreatePreview}
          >
            {isPending
              ? t('generation-draft-preview-generating')
              : t('generation-draft-preview-generate')}
          </button>
        )}
      </div>

      {previewError && (
        <div className="alert alert-error" role="alert">
          <span>{previewError}</span>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <DraftComposer
            draft={draft}
            onDraftMutated={handleDraftMutated}
            onMutationPendingChange={handleMutationPendingChange}
          />
        </div>
        <div className="lg:col-span-1">{children}</div>
      </div>
    </div>
  );
}
