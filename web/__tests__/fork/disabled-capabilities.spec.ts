import fs from 'fs';
import path from 'path';

import { unAuthenticatedRoutes } from '../../middleware';

const root = path.join(__dirname, '..', '..');

/** Comments explain what was removed; assertions run against code only. */
const stripComments = (source: string) =>
  source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

const walk = (dir: string, acc: string[] = []): string[] => {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walk(full, acc);
    } else {
      acc.push(path.relative(root, full));
    }
  }
  return acc;
};

const sourceFiles = [
  ...walk(path.join(root, 'pages')),
  ...walk(path.join(root, 'components')),
  ...walk(path.join(root, 'lib')),
  ...walk(path.join(root, 'models')),
  ...walk(path.join(root, 'hooks')),
];

const packageJson = JSON.parse(
  fs.readFileSync(path.join(root, 'package.json'), 'utf8')
);

const prismaSchemaSource = fs.readFileSync(
  path.join(root, 'prisma', 'schema.prisma'),
  'utf8'
);

// Comments explain what was removed, so assertions run against declarations only.
const prismaSchema = prismaSchemaSource
  .split('\n')
  .filter((line) => !line.trim().startsWith('//'))
  .join('\n');

/**
 * Each disabled upstream capability is asserted three ways: no file, no
 * dependency and no allowlist entry. A capability that is merely feature
 * flagged would pass none of these.
 */
const disabledCapabilities = [
  {
    name: 'SAML SSO',
    pathFragments: [
      'auth/sso',
      'api/oauth',
      'teams/[slug]/sso',
      'jackson',
      'idp',
    ],
    packages: ['@boxyhq/saml-jackson', '@boxyhq/react-ui'],
  },
  {
    name: 'SCIM / directory sync',
    pathFragments: ['api/scim', 'dsync', 'directory-sync'],
    packages: [],
  },
  {
    name: 'Customer webhooks',
    pathFragments: [
      'api/webhooks',
      'teams/[slug]/webhooks',
      'components/webhook',
    ],
    packages: ['svix'],
  },
  {
    name: 'Live Stripe billing',
    // The upstream Stripe catalogue page was `pages/teams/[slug]/products.tsx`
    // (HARDENING.md). MarketAI's own product home (`/w/[slug]/products`,
    // #162) is domain product data, not billing, so the fragment names the
    // upstream route rather than every path containing "products".
    pathFragments: [
      'payments',
      'billing',
      'teams/[slug]/products',
      'components/billing',
    ],
    packages: ['stripe', 'currency-symbol-map'],
  },
  {
    name: 'Retraced audit-log service',
    pathFragments: ['audit-logs', 'retraced'],
    packages: ['@retracedhq/retraced', '@retracedhq/logs-viewer'],
  },
  {
    name: 'Team API keys',
    pathFragments: ['api-keys', 'apiKey'],
    packages: [],
  },
  {
    name: 'Third-party browser analytics and error reporting',
    pathFragments: ['sentry', 'mixpanel'],
    packages: ['@sentry/nextjs', 'mixpanel-browser', 'slack-notify'],
  },
  {
    name: 'Public marketing / signup funnel',
    pathFragments: ['defaultLanding', 'components/auth/Join.tsx', 'CreateTeam'],
    packages: ['react-google-recaptcha'],
  },
  {
    name: 'Shareable link invitations',
    pathFragments: ['InviteViaLink', 'EmailDomainMismatch'],
    packages: [],
  },
];

describe('Disabled upstream capabilities', () => {
  describe.each(disabledCapabilities)('$name', (capability) => {
    it('has no source file, route, component or handler', () => {
      for (const fragment of capability.pathFragments) {
        const matches = sourceFiles.filter((file) =>
          file.toLowerCase().includes(fragment.toLowerCase())
        );

        expect(matches).toEqual([]);
      }
    });

    it('has no dependency left in package.json', () => {
      const declared = {
        ...(packageJson.dependencies ?? {}),
        ...(packageJson.devDependencies ?? {}),
      };

      for (const pkg of capability.packages) {
        expect(declared[pkg]).toBeUndefined();
      }
    });
  });

  it('keeps removed public routes out of the unauthenticated allowlist', () => {
    const forbidden = [
      '/api/oauth/**',
      '/api/scim/v2.0/**',
      '/api/webhooks/stripe',
      '/api/webhooks/dsync',
      '/login/saml',
      '/.well-known/*',
      '/api/hello',
    ];

    for (const route of forbidden) {
      expect(unAuthenticatedRoutes).not.toContain(route);
    }
  });

  it('drops the removed models from the control-plane schema', () => {
    for (const model of [
      'model ApiKey',
      'model Service',
      'model Price',
      'jackson_store',
      'jackson_index',
      'jackson_ttl',
    ]) {
      expect(prismaSchema).not.toContain(model);
    }
  });

  it('keeps the Role enum reduced to the customer contract', () => {
    expect(prismaSchema).toContain('enum Role {\n  OWNER\n  MEMBER\n}');
  });

  it('exports no physical Workspace delete', () => {
    // The Workspace lifecycle ends in deletion_pending with a 30-day recovery
    // window; a hard delete cannot express that, so no helper exists at all.
    const teamModel = stripComments(
      fs.readFileSync(path.join(root, 'models', 'team.ts'), 'utf8')
    );

    expect(teamModel).not.toContain('prisma.team.delete');
    expect(teamModel).not.toMatch(/export const deleteTeam/);
  });

  it('routes every server log through the redacting logger', () => {
    const offenders: string[] = [];

    for (const file of sourceFiles) {
      if (file === path.join('lib', 'logger.ts')) continue;

      const body = stripComments(
        fs.readFileSync(path.join(root, file), 'utf8')
      );
      if (
        /(?<![A-Za-z0-9_$.])console\.(log|error|warn|info|debug)\(/.test(body)
      ) {
        offenders.push(file);
      }
    }

    expect(offenders).toEqual([]);
  });

  it('does not own any MarketAI domain table', () => {
    // The domain database is owned by SQLAlchemy (FastAPI/Worker). If a domain
    // concept ever appears in this schema, the boundary has been crossed.
    for (const domainModel of [
      'AssetVersion',
      'GenerationRun',
      'Experiment',
      'Localization',
      'ScaleAdvice',
      'CredentialVersion',
      'ProviderCost',
    ]) {
      expect(prismaSchema).not.toContain(domainModel);
    }
  });
});

describe('Release path', () => {
  it('never runs prisma db push in build or release scripts', () => {
    const scripts: Record<string, string> = packageJson.scripts;

    for (const [name, command] of Object.entries(scripts)) {
      expect(`${name}: ${command}`).not.toContain('db push');
      expect(`${name}: ${command}`).not.toContain('db-push');
    }
  });

  it('applies schema changes with prisma migrate deploy', () => {
    // Migration identity is asserted in migration-identity.spec.ts.
    expect(packageJson.scripts['db:migrate:deploy']).toContain(
      'scripts/prisma-migrate.mjs deploy'
    );
    expect(packageJson.scripts.build).toContain(
      'prisma generate && next build'
    );
  });

  it('ships a reviewed migration directory', () => {
    const migrations = fs
      .readdirSync(path.join(root, 'prisma', 'migrations'), {
        withFileTypes: true,
      })
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name);

    expect(migrations.length).toBeGreaterThan(0);

    for (const migration of migrations) {
      const sql = fs.readFileSync(
        path.join(root, 'prisma', 'migrations', migration, 'migration.sql'),
        'utf8'
      );
      expect(sql.length).toBeGreaterThan(0);
    }
  });
});
