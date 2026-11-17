import env from '@/lib/env';
import {
  computeTargetRoute,
  firstBatchHref,
  newProductHref,
  workspaceHome,
} from '@/lib/workspace-navigator';
import { getServerSideProps as workspaceIndex } from '../../pages/w/[slug]/index';

describe('the Workspace home (#194)', () => {
  const initial = env.adsFirstEnabled;
  afterEach(() => {
    env.adsFirstEnabled = initial;
  });

  it('is off unless NEXT_PUBLIC_MARKETAI_ADS_FIRST is "true"', () => {
    expect(process.env.NEXT_PUBLIC_MARKETAI_ADS_FIRST).toBeUndefined();
    expect(env.adsFirstEnabled).toBe(false);
  });

  it('/w/{slug} goes to /experiments while the switch is off', async () => {
    env.adsFirstEnabled = false;
    await expect(
      workspaceIndex({ params: { slug: 'acme' } } as never)
    ).resolves.toEqual({
      redirect: { destination: '/w/acme/experiments', permanent: false },
    });
  });

  it('/w/{slug} goes to the overview while the switch is on', async () => {
    env.adsFirstEnabled = true;
    await expect(
      workspaceIndex({ params: { slug: 'acme' } } as never)
    ).resolves.toEqual({
      redirect: { destination: '/w/acme/overview', permanent: false },
    });
  });

  it('switching Workspaces follows the switch when no flag is passed', () => {
    env.adsFirstEnabled = true;
    expect(
      computeTargetRoute({
        targetSlug: 'beta',
        targetRole: 'OWNER',
        currentPathname: '/w/alpha',
      })
    ).toBe('/w/beta/overview');
    expect(workspaceHome('beta', false)).toBe('/w/beta/experiments');
  });

  it('"New product" goes to the product intake while the switch is off', () => {
    env.adsFirstEnabled = false;
    expect(newProductHref('acme')).toBe('/w/acme/products/new');
    expect(newProductHref('acme', false)).toBe('/w/acme/products/new');
  });

  it('"New product" goes to the start page while the switch is on (#195)', () => {
    env.adsFirstEnabled = true;
    expect(newProductHref('acme')).toBe('/w/acme/start');
    expect(newProductHref('acme', true)).toBe('/w/acme/start');
  });

  it('a first batch reads at its own page under the start page (#195)', () => {
    expect(firstBatchHref('acme', 'mfb_1')).toBe('/w/acme/start/mfb_1');
  });
});

describe('computeTargetRoute', () => {
  it('preserves feature area when switching workspaces', () => {
    expect(
      computeTargetRoute({
        targetSlug: 'beta',
        targetRole: 'OWNER',
        currentPathname: '/w/alpha/assets',
      })
    ).toBe('/w/beta/assets');
  });

  it('preserves the product home when switching workspaces (#162)', () => {
    expect(
      computeTargetRoute({
        targetSlug: 'beta',
        targetRole: 'MEMBER',
        currentPathname: '/w/alpha/products/new',
      })
    ).toBe('/w/beta/products');
  });

  it('lands on the tests list, never a foreign plan id, when switching (#165)', () => {
    expect(
      computeTargetRoute({
        targetSlug: 'beta',
        targetRole: 'MEMBER',
        currentPathname: '/w/alpha/tests/atp_123',
      })
    ).toBe('/w/beta/tests');
  });

  it('preserves settings for Owner target', () => {
    expect(
      computeTargetRoute({
        targetSlug: 'beta',
        targetRole: 'OWNER',
        currentPathname: '/w/alpha/settings',
      })
    ).toBe('/w/beta/settings');
  });

  it('preserves settings for Member target: a Member reads its spending limits there', () => {
    expect(
      computeTargetRoute({
        targetSlug: 'beta',
        targetRole: 'MEMBER',
        currentPathname: '/w/alpha/settings',
      })
    ).toBe('/w/beta/settings');
  });

  it('falls back to /experiments when Member targets members page', () => {
    expect(
      computeTargetRoute({
        targetSlug: 'beta',
        targetRole: 'MEMBER',
        currentPathname: '/w/alpha/members',
      })
    ).toBe('/w/beta/experiments');
  });

  it('falls back to /experiments for unknown path', () => {
    expect(
      computeTargetRoute({
        targetSlug: 'beta',
        targetRole: 'OWNER',
        currentPathname: '/teams/alpha/settings',
      })
    ).toBe('/w/beta/experiments');
  });

  it('lands on /experiments with the ads-first switch off (#194)', () => {
    expect(
      computeTargetRoute({
        targetSlug: 'beta',
        targetRole: 'OWNER',
        currentPathname: '/w/alpha',
        adsFirst: false,
      })
    ).toBe('/w/beta/experiments');
    // The ads-first areas exist only while the switch is on.
    expect(
      computeTargetRoute({
        targetSlug: 'beta',
        targetRole: 'OWNER',
        currentPathname: '/w/alpha/overview',
        adsFirst: false,
      })
    ).toBe('/w/beta/experiments');
  });

  it('lands on the overview with the ads-first switch on (#194)', () => {
    for (const currentPathname of [
      '/w/alpha',
      '/w/alpha/overview',
      '/teams/alpha/settings',
      // A product belongs to one Workspace: never carry its id across.
      '/w/alpha/p/prod_1/ideas',
    ]) {
      expect(
        computeTargetRoute({
          targetSlug: 'beta',
          targetRole: 'MEMBER',
          currentPathname,
          adsFirst: true,
        })
      ).toBe('/w/beta/overview');
    }
    expect(
      computeTargetRoute({
        targetSlug: 'beta',
        targetRole: 'MEMBER',
        currentPathname: '/w/alpha/competitors',
        adsFirst: true,
      })
    ).toBe('/w/beta/competitors');
    expect(
      computeTargetRoute({
        targetSlug: 'beta',
        targetRole: 'MEMBER',
        currentPathname: '/w/alpha/members',
        adsFirst: true,
      })
    ).toBe('/w/beta/overview');
    // The start page carries across; a first batch's own page does not.
    for (const currentPathname of ['/w/alpha/start', '/w/alpha/start/mfb_1'])
      expect(
        computeTargetRoute({
          targetSlug: 'beta',
          targetRole: 'MEMBER',
          currentPathname,
          adsFirst: true,
        })
      ).toBe('/w/beta/start');
    expect(
      computeTargetRoute({
        targetSlug: 'beta',
        targetRole: 'MEMBER',
        currentPathname: '/w/alpha/start',
        adsFirst: false,
      })
    ).toBe('/w/beta/experiments');
  });

  it('handles all six feature areas', () => {
    const areas = [
      'generate',
      'assets',
      'experiments',
      'localization',
      'scale',
      'library',
    ];
    for (const area of areas) {
      expect(
        computeTargetRoute({
          targetSlug: 'x',
          targetRole: 'MEMBER',
          currentPathname: `/w/old/${area}`,
        })
      ).toBe(`/w/x/${area}`);
    }
  });
});
