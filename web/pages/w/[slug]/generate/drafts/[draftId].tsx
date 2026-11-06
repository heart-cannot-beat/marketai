import type { ReactElement } from 'react';
import { useRouter } from 'next/router';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { DraftWorkspace } from '@/components/generation/draft/DraftWorkspace';
import { PreviewPanel } from '@/components/generation/preview/PreviewPanel';
import { SubmitConfirmation } from '@/components/generation/preview/SubmitConfirmation';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/**
 * Fetches the frozen preview a second time (shares the `get_generation_preview`
 * SWR cache key with `PreviewPanel`, so this never doubles the network
 * request) purely to hand `SubmitConfirmation` the `preview_hash`/
 * `valid_until`/`allowed_actions` it needs but has no query of its own for
 * (mirrors `SubmitConfirmation`'s own doc comment: it must not call
 * `useDomainQuery` itself, since its fixed test contract never mocks that
 * hook). Renders nothing for a recovery preview (`purpose: "run_recovery"`)
 * -- that flow gets its own confirm component in a later task.
 */
function PreviewSubmitSlot({ previewId }: { previewId: string }) {
  const { data: preview } = useDomainQuery('get_generation_preview', {
    preview_id: previewId,
  } as any);

  if (!preview || preview.purpose !== 'initial_generation') return null;

  return (
    <SubmitConfirmation
      previewId={preview.id}
      previewHash={preview.preview_hash}
      draftRevision={preview.draft_revision}
      validUntil={preview.valid_until ?? ''}
      allowedActions={preview.allowed_actions}
      onSubmitted={() => {}}
    />
  );
}

const DraftPage: NextPageWithLayout<Props> = () => {
  const router = useRouter();
  const draftId = router.query.draftId as string;
  const previewId = (router.query.preview as string) ?? null;

  function handlePreviewCreated(previewId: string) {
    router.replace(
      { query: { ...router.query, preview: previewId } },
      undefined,
      { shallow: true }
    );
  }

  return (
    <DraftWorkspace
      draftId={draftId}
      previewId={previewId}
      onPreviewCreated={handlePreviewCreated}
    >
      {previewId && (
        <>
          <PreviewPanel previewId={previewId} />
          <PreviewSubmitSlot previewId={previewId} />
        </>
      )}
    </DraftWorkspace>
  );
};

DraftPage.getLayout = function getLayout(page: ReactElement) {
  const { bootstrap, errorCode, errorContext } = page.props as Props;
  return (
    <WorkspaceLayout
      bootstrap={bootstrap}
      errorCode={errorCode}
      errorContext={errorContext}
    >
      {page}
    </WorkspaceLayout>
  );
};

export const getServerSideProps = getWorkspacePageProps({
  namespaces: ['common', 'products'],
});
export default DraftPage;
