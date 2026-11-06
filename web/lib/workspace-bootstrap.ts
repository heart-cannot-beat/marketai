import type { GetServerSideProps, GetServerSidePropsContext } from 'next';
import { serverSideTranslations } from 'next-i18next/serverSideTranslations';
import { getAuthenticatedSession } from '@/lib/requireSession';
import { prisma } from '@/lib/prisma';

export type WorkspaceBootstrap = {
  workspaceId: string;
  slug: string;
  displayName: string;
  membershipId: string;
  role: 'OWNER' | 'MEMBER';
};

export type WorkspaceSummary = {
  workspaceId: string;
  slug: string;
  displayName: string;
  role: 'OWNER' | 'MEMBER';
};

const SLUG_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$/;

export function getWorkspacePageProps(
  options: { ownerOnly?: boolean; namespaces?: string[] } = {}
): GetServerSideProps<{ bootstrap: WorkspaceBootstrap }> {
  const { ownerOnly = false, namespaces = ['common'] } = options;

  return async (context: GetServerSidePropsContext) => {
    const { req, res, params, locale } = context;
    const slug = params?.slug as string | undefined;

    // 1. Session
    const session = await getAuthenticatedSession(req, res);
    if (!session) {
      return {
        redirect: {
          destination: `/auth/login?callbackUrl=${encodeURIComponent(context.resolvedUrl)}`,
          permanent: false,
        },
      };
    }

    // 2. Slug shape
    if (!slug || !SLUG_PATTERN.test(slug)) {
      return { notFound: true };
    }

    // 3. Workspace lookup. `findUnique` resolves to `null` (not a throw) when
    // the slug doesn't exist, so a real DB/network error propagates instead
    // of being swallowed into a false 404 — Next.js renders its default 500
    // page for an unhandled rejection from `getServerSideProps`.
    //
    // Deliberately two-step (team, then membership) rather than a single
    // `models/team#getTeamMember` call: that helper's `findFirstOrThrow`
    // filters on `team.slug` and `userId` together and throws (never
    // resolves `null`) when either the workspace doesn't exist or the user
    // just isn't a member of it — the two cases are indistinguishable from
    // the caller's side, which would collapse the "no membership" 403 below
    // into this 404 for every real request. See task-1-review.md finding #1.
    const team = await prisma.team.findUnique({ where: { slug } });
    if (!team) {
      return { notFound: true };
    }

    // 4. Membership lookup — `findFirst` also resolves `null` rather than
    // throwing, so this 403 branch is actually reachable in production.
    const member = await prisma.teamMember.findFirst({
      where: { userId: session.user.id, teamId: team.id },
    });

    if (!member) {
      const i18n = locale
        ? await serverSideTranslations(locale, namespaces)
        : {};
      return {
        props: { ...i18n, errorCode: 403, errorContext: 'no-membership' },
      } as any;
    }

    // 5. Owner gate
    if (ownerOnly && member.role !== 'OWNER') {
      const i18n = locale
        ? await serverSideTranslations(locale, namespaces)
        : {};
      return {
        props: {
          ...i18n,
          errorCode: 403,
          errorContext: 'owner-only',
          bootstrap: null,
        },
      } as any;
    }

    // 6. Success
    const bootstrap: WorkspaceBootstrap = {
      workspaceId: team.id,
      slug: team.slug,
      displayName: team.name,
      membershipId: member.id,
      role: member.role as 'OWNER' | 'MEMBER',
    };

    const i18n = locale ? await serverSideTranslations(locale, namespaces) : {};
    return { props: { ...i18n, bootstrap } };
  };
}
