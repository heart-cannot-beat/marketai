import fs from 'node:fs';
import path from 'node:path';

const root = path.resolve(__dirname, '../..');

describe('support-mediated MFA recovery contract', () => {
  it('has no Workspace Owner reset API and exposes only an audited operator CLI', () => {
    expect(
      fs.existsSync(path.join(root, 'pages/api/teams/[slug]/mfa-reset.ts'))
    ).toBe(false);
    expect(fs.existsSync(path.join(root, 'scripts/mfa-reset.ts'))).toBe(true);
  });

  it('does not model customer members as MFA reset approvers', () => {
    const schema = fs.readFileSync(
      path.join(root, 'prisma/schema.prisma'),
      'utf8'
    );
    const resetCase = schema.split('model MfaResetCase')[1].split('model ')[0];
    expect(resetCase).toContain('requestedByOperatorId');
    expect(resetCase).not.toContain('firstApprovedBy  User');
    expect(schema).toContain('model MfaResetApproval');
    expect(schema).toContain('model MfaRecoveryEmailChallenge');
  });

  it('requires system evidence instead of operator-supplied proof booleans', () => {
    const script = fs.readFileSync(
      path.join(root, 'scripts/mfa-reset.ts'),
      'utf8'
    );
    expect(script).toContain('--case-id');
    expect(script).toContain('--reason');
    expect(script).toContain('requireOperatorIdentityFromEnv');
    expect(script).not.toContain('--operator-id');
    expect(script).not.toMatch(/password-verified|email-verified|proof-ok/i);
  });
});
