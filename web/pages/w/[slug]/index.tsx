import type { GetServerSidePropsContext } from 'next';

/** `/w/{slug}` lands on the product home (front-flow contract §3.1, #162). */
export async function getServerSideProps({
  params,
}: GetServerSidePropsContext) {
  return {
    redirect: {
      destination: `/w/${params?.slug}/experiments`,
      permanent: false,
    },
  };
}

export default function WorkspaceIndex() {
  return null;
}
