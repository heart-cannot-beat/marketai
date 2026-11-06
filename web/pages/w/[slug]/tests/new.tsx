import type { GetServerSidePropsContext } from 'next';

/**
 * `/w/{slug}/tests/new` -- the legacy guided setup (Issue #165) is frozen
 * (#337): new tests start in the marketing test. A `?product=` deep link
 * lands there too, without its query.
 */
export async function getServerSideProps({
  params,
}: GetServerSidePropsContext) {
  return {
    redirect: {
      destination: `/w/${encodeURIComponent(String(params?.slug ?? ''))}/experiments`,
      permanent: false,
    },
  };
}

export default function NewTestPage() {
  return null;
}
