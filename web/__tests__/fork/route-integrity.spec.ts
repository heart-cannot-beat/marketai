import fs from 'fs';
import path from 'path';

const root = path.join(__dirname, '..', '..');

const walk = (dir: string, acc: string[] = []): string[] => {
  if (!fs.existsSync(dir)) return acc;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, acc);
    else if (/\.tsx?$/.test(entry.name)) acc.push(full);
  }
  return acc;
};

/** `/teams/[slug]/settings.tsx` -> `/teams/:p/settings` */
const fileToRoute = (file: string) => {
  const relative = path
    .relative(path.join(root, 'pages'), file)
    .replace(/\.tsx?$/, '')
    .replace(/\\/g, '/');

  const withoutIndex = relative.endsWith('/index')
    ? relative.slice(0, -'/index'.length)
    : relative === 'index'
      ? ''
      : relative;

  return `/${withoutIndex}`.replace(/\[\.\.\.[^\]]+\]|\[[^\]]+\]/g, ':p');
};

const routes = new Set(walk(path.join(root, 'pages')).map(fileToRoute));

// `${team.slug}` and `[slug]` are the same hole in a route. An interpolation
// that is not a whole path segment is a query-string suffix, so the route ends
// there.
const normalise = (link: string) =>
  link
    .replace(/([^/])\$\{[^}]*\}.*$/, '$1')
    .replace(/\$\{[^}]*\}/g, ':p')
    .split('?')[0]
    .split('#')[0]
    .replace(/\/$/, '') || '/';

const LINK_PATTERNS = [
  /href=["'](\/[^"'\s]*)["']/g,
  /href=\{`(\/[^`]*)`\}/g,
  /(?:router\.push|router\.replace)\(\s*["'](\/[^"']*)["']/g,
  /(?:router\.push|router\.replace)\(\s*`(\/[^`]*)`/g,
  /fetch\(\s*["'](\/api\/[^"']*)["']/g,
  /fetch\(\s*`(\/api\/[^`]*)`/g,
];

const sources = [
  ...walk(path.join(root, 'pages')),
  ...walk(path.join(root, 'components')),
  ...walk(path.join(root, 'hooks')),
];

const collectLinks = () => {
  const found: { file: string; link: string }[] = [];

  for (const file of sources) {
    const body = fs.readFileSync(file, 'utf8');

    for (const pattern of LINK_PATTERNS) {
      for (const match of Array.from(body.matchAll(pattern))) {
        const link = normalise(match[1]);

        // Static assets and the NextAuth catch-all are not page routes.
        if (/\.(png|jpe?g|svg|ico|css|webp)$/.test(link)) continue;
        if (link.startsWith('/api/auth/')) continue;

        found.push({ file: path.relative(root, file), link });
      }
    }
  }

  return found;
};

describe('Route integrity', () => {
  const links = collectLinks();

  it('finds links to check', () => {
    expect(links.length).toBeGreaterThan(10);
  });

  /**
   * Every internal link resolves to a page or API route that still exists.
   * A removed capability that survives as a navigation item, a redirect or a
   * client fetch fails here.
   */
  it('has no link to a route this fork removed', () => {
    const dangling = links.filter(({ link }) => !routes.has(link));

    expect(dangling).toEqual([]);
  });

  it('exposes no page for a disabled capability', () => {
    const forbidden = [
      '/teams/:p/sso',
      '/teams/:p/directory-sync',
      '/teams/:p/webhooks',
      '/teams/:p/api-keys',
      '/teams/:p/audit-logs',
      '/teams/:p/billing',
      '/teams/:p/products',
      '/auth/sso',
      '/auth/magic-link',
      '/auth/idp-login',
      '/api/scim/v2.0/:p',
      '/api/webhooks/stripe',
      '/api/webhooks/dsync',
      '/api/idp',
      '/api/hello',
    ];

    for (const route of forbidden) {
      expect(Array.from(routes)).not.toContain(route);
    }
  });

  it('keeps the routes the Beta needs', () => {
    for (const route of [
      '/auth/login',
      '/auth/join',
      '/invitations/:p',
      '/teams',
      '/teams/:p/settings',
      '/teams/:p/members',
      '/api/health',
      '/api/readiness',
      '/api/teams',
      '/api/teams/:p/members',
      '/api/teams/:p/invitations',
    ]) {
      expect(Array.from(routes)).toContain(route);
    }
  });
});
