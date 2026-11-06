import type { GetServerSidePropsContext } from 'next';
import { getAuthenticatedSession } from '@/lib/requireSession';
import { getTeams } from 'models/team';

// Rendered per request, not prerendered: the page is behind authentication and
// carries the per-request CSP nonce from middleware. Redirecting server-side
// (rather than client-side, per the previous `useTeams` + `useEffect`
// implementation) avoids a loading flash before the destination is known.
export async function getServerSideProps({
  req,
  res,
}: GetServerSidePropsContext) {
  const session = await getAuthenticatedSession(req, res);
  if (!session) {
    return { redirect: { destination: '/auth/login', permanent: false } };
  }

  const teams = await getTeams(session.user.id);
  if (teams.length > 0) {
    return {
      redirect: {
        destination: `/w/${teams[0].slug}/experiments`,
        permanent: false,
      },
    };
  }

  return { redirect: { destination: '/teams', permanent: false } };
}

export default function Dashboard() {
  return null;
}
