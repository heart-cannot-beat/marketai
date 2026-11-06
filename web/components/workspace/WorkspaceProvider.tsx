import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
} from 'react';
import type { ReactNode } from 'react';
import { useRouter } from 'next/router';
import useTeams from 'hooks/useTeams';
import { computeTargetRoute } from '@/lib/workspace-navigator';
import type {
  WorkspaceBootstrap,
  WorkspaceSummary,
} from '@/lib/workspace-bootstrap';

// `useTeams` resolves `TeamWithMemberCount[]` from the generated Prisma
// payload type, which has no `role` field. `GET /api/teams` (see
// pages/api/teams/index.ts) always attaches the caller's membership role to
// each row before responding, so the field is present on the wire even
// though it is absent from the Prisma-derived type.
type TeamWithRole = {
  id: string;
  slug: string;
  name: string;
  role?: 'OWNER' | 'MEMBER';
};

type WorkspaceContextValue = {
  current: WorkspaceBootstrap;
  available: WorkspaceSummary[];
  pendingSlug: string | null;
  switchWorkspace: (slug: string) => Promise<boolean>;
};

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function useWorkspace(): WorkspaceContextValue {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) {
    throw new Error('useWorkspace must be used within WorkspaceProvider');
  }
  return ctx;
}

export function WorkspaceProvider({
  bootstrap,
  children,
}: {
  bootstrap: WorkspaceBootstrap;
  children: ReactNode;
}) {
  const router = useRouter();
  const { teams } = useTeams();
  const [pendingSlug, setPendingSlug] = useState<string | null>(null);

  const available: WorkspaceSummary[] = useMemo(
    () =>
      ((teams as TeamWithRole[] | undefined) ?? []).map((team) => ({
        workspaceId: team.id,
        slug: team.slug,
        displayName: team.name,
        role: team.role ?? 'MEMBER',
      })),
    [teams]
  );

  const switchWorkspace = useCallback(
    async (slug: string): Promise<boolean> => {
      // Already on the target Workspace, or a switch is already in flight.
      if (slug === bootstrap.slug || pendingSlug) {
        return false;
      }

      // `available` is sourced from the control plane's `/api/teams`
      // membership listing, not from an entitlement or authorization check —
      // it only gates which Workspaces this switcher can offer to navigate
      // to. Authorization for the destination page is re-derived server-side
      // from BFF claims (see workspace-bootstrap.ts), never trusted from
      // this client-side context.
      const target = available.find((workspace) => workspace.slug === slug);
      if (!target) {
        return false;
      }

      setPendingSlug(slug);
      const targetUrl = computeTargetRoute({
        targetSlug: slug,
        targetRole: target.role,
        currentPathname: router.asPath,
      });

      try {
        await router.push(targetUrl);
        return true;
      } catch {
        return false;
      } finally {
        setPendingSlug(null);
      }
    },
    [bootstrap.slug, pendingSlug, available, router]
  );

  const value = useMemo<WorkspaceContextValue>(
    () => ({ current: bootstrap, available, pendingSlug, switchWorkspace }),
    [bootstrap, available, pendingSlug, switchWorkspace]
  );

  return (
    <WorkspaceContext.Provider value={value}>
      {children}
    </WorkspaceContext.Provider>
  );
}
