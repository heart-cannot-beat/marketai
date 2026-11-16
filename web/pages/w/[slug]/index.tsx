import type { GetServerSidePropsContext } from 'next';
import { workspaceHome } from '@/lib/workspace-navigator';

/**
 * `/w/{slug}` lands on the marketing tests (front-flow contract §3.1, #162),
 * or on the overview while the ads-first switch is on (ADR 0018).
 */
export async function getServerSideProps({
  params,
}: GetServerSidePropsContext) {
  return {
    redirect: {
      destination: workspaceHome(params?.slug as string),
      permanent: false,
    },
  };
}

export default function WorkspaceIndex() {
  return null;
}
