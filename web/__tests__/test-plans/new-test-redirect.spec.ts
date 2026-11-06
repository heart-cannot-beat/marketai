import type { GetServerSidePropsContext } from 'next';
import { getServerSideProps } from '../../pages/w/[slug]/tests/new';

/** `/w/{slug}/tests/new` starts no legacy plan since the freeze (#337). */
describe('/w/{slug}/tests/new', () => {
  const visit = (query: Record<string, string> = {}) =>
    getServerSideProps({
      params: { slug: 'acme' },
      query,
    } as unknown as GetServerSidePropsContext);

  it('redirects to the marketing test', async () => {
    await expect(visit()).resolves.toEqual({
      redirect: { destination: '/w/acme/experiments', permanent: false },
    });
  });

  it('drops a ?product= deep link instead of carrying an id along', async () => {
    await expect(visit({ product: 'prod_1' })).resolves.toEqual({
      redirect: { destination: '/w/acme/experiments', permanent: false },
    });
  });
});
