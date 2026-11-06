import fs from 'fs';
import path from 'path';

import { inviteViaEmailSchema } from '@/lib/zod';

const root = path.join(__dirname, '..', '..');

const schemaDeclarations = fs
  .readFileSync(path.join(root, 'prisma', 'schema.prisma'), 'utf8')
  .split('\n')
  .filter(
    (line) => !line.trim().startsWith('//') && !line.trim().startsWith('///')
  )
  .join('\n');

const baselineDir = path.join(
  root,
  'prisma',
  'migrations',
  '20260826000000_control_plane_baseline'
);
const baselineSql = fs.readFileSync(
  path.join(baselineDir, 'migration.sql'),
  'utf8'
);
const activeUniquenessSql = fs.readFileSync(
  path.join(
    root,
    'prisma',
    'migrations',
    '20260910150000_active_invitation_uniqueness',
    'migration.sql'
  ),
  'utf8'
);

const invitationModel = schemaDeclarations
  .split('model Invitation {')[1]
  .split('}')[0];

/**
 * CONTEXT.md defines an invitation as one-time and addressed to a named email.
 * Upstream also supported shareable link invitations, which admitted anyone
 * holding the URL whose email domain matched an allowlist.
 */
describe('Invitations are single-use and email-addressed', () => {
  it('cannot represent a link invitation in the schema', () => {
    expect(invitationModel).not.toContain('sentViaEmail');
    expect(invitationModel).not.toContain('allowedDomains');
  });

  it('requires an email address at the column level', () => {
    expect(invitationModel).toMatch(/^\s*email\s+String\s*$/m);
    // `String?` would make the address optional again.
    expect(invitationModel).not.toMatch(/email\s+String\?/);
  });

  it('allows historical rows but enforces one active invite per address', () => {
    expect(invitationModel).not.toContain('@@unique([teamId, email])');
    expect(activeUniquenessSql).toContain(
      'CREATE UNIQUE INDEX "Invitation_active_team_email_key"'
    );
    expect(activeUniquenessSql).toContain(
      'ON "Invitation"("teamId", lower("email"))'
    );
    expect(activeUniquenessSql).toContain(
      'WHERE "consumedAt" IS NULL AND "revokedAt" IS NULL'
    );
    expect(activeUniquenessSql).toContain(
      'active invitation uniqueness violation'
    );
  });

  it('creates the invitation table with a NOT NULL email and no link columns', () => {
    const createTable = baselineSql
      .split('CREATE TABLE "Invitation" (')[1]
      .split(');')[0];

    expect(createTable).toContain('"email" TEXT NOT NULL');
    expect(createTable).not.toContain('sentViaEmail');
    expect(createTable).not.toContain('allowedDomains');
  });

  it('rejects an invitation request that names no recipient', () => {
    expect(
      inviteViaEmailSchema.safeParse({ role: 'MEMBER', domains: 'example.com' })
        .success
    ).toBe(false);

    expect(
      inviteViaEmailSchema.safeParse({ role: 'MEMBER', sentViaEmail: false })
        .success
    ).toBe(false);

    expect(
      inviteViaEmailSchema.safeParse({
        email: 'teammate@example.com',
        role: 'MEMBER',
      }).success
    ).toBe(true);
  });

  it('has no link-invitation component or hook left', () => {
    for (const file of [
      'components/invitation/InviteViaLink.tsx',
      'components/invitation/EmailDomainMismatch.tsx',
    ]) {
      expect(fs.existsSync(path.join(root, file))).toBe(false);
    }
  });

  it('consumes the invitation in the same transaction as the membership', () => {
    const model = fs.readFileSync(
      path.join(root, 'models', 'invitation.ts'),
      'utf8'
    );

    expect(model).toContain('prisma.$transaction');

    // Durable consumption supports an idempotent replay while updateMany's
    // consumedAt=null compare-and-set preserves single-use semantics.
    expect(model).toContain('consumedAt: null');
    expect(model).toContain('consumedByUserId');
    expect(model).toContain('tx.invitation.updateMany');
  });

  it('never creates a membership outside consumeInvitation', () => {
    const route = fs.readFileSync(
      path.join(root, 'pages', 'api', 'teams', '[slug]', 'invitations.ts'),
      'utf8'
    );

    expect(route).toContain('consumeInvitation');
    expect(route).not.toContain('addTeamMember');
  });
});
