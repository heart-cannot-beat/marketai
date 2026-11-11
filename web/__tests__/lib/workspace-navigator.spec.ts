import { computeTargetRoute } from '@/lib/workspace-navigator';

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
