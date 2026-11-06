import type { ReactElement } from 'react';
import { useRouter } from 'next/router';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { RunProgressView } from '@/components/generation/progress/RunProgressView';
import { RecoveryPanel } from '@/components/generation/recovery/RecoveryPanel';
import { useRunProgress } from '@/components/generation/shared/useRunProgress';
import { useRunDirectionGroups } from '@/hooks/test-plans/useRunDirectionGroups';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

/**
 * `RunProgressView`'s polling loop is private to it (spec: Task 9) --
 * there's no way for a parent to reach into it to learn
 * `run`/`allowed_actions` or to force a refresh after a recovery command
 * lands. So this page runs its own
 * `useRunProgress(runId)` alongside `RunProgressView`'s to build the slot:
 * a second, independent poller rather than a shared one, but the only
 * option `RunProgressView`'s `ReactNode` slot (not a render-prop) leaves
 * open. Both polling loops stop once the Run and its latest command
 * Operation are terminal, so the steady-state cost of the duplicate is one
 * extra `get_generation_run` call, not indefinite double-polling.
 */
const RunPage: NextPageWithLayout<Props> = () => {
  const router = useRouter();
  const runId = router.query.runId as string;
  const progress = useRunProgress(runId || null);
  // "按方向" (Issue #170): a Run made from a test plan groups its items by
  // the plan's directions; the grouped read follows the Run's progress.
  const grouped = useRunDirectionGroups(
    runId || null,
    [progress.run?.status, progress.run?.done, progress.run?.failed].join(':')
  );

  return (
    <RunProgressView
      runId={runId}
      directionGroups={grouped}
      recoverySlot={
        progress.run ? (
          <RecoveryPanel
            run={progress.run}
            onRecoveryComplete={progress.forceRefresh}
          />
        ) : undefined
      }
    />
  );
};

RunPage.getLayout = function getLayout(page: ReactElement) {
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
  namespaces: ['common', 'products', 'directions'],
});
export default RunPage;
