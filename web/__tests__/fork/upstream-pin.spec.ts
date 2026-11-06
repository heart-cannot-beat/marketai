import fs from 'fs';
import path from 'path';

const root = path.join(__dirname, '..', '..');
const pin = JSON.parse(
  fs.readFileSync(path.join(root, 'UPSTREAM.json'), 'utf8')
);

describe('Pinned upstream identity', () => {
  it('is machine-readable and names the exact upstream repository', () => {
    expect(pin.upstream.repository).toBe(
      'https://github.com/boxyhq/saas-starter-kit'
    );
    expect(pin.upstream.name).toBe('BoxyHQ SaaS Starter Kit');
  });

  it('pins a full commit SHA rather than a branch or tag', () => {
    expect(pin.upstream.pinnedCommit).toMatch(/^[0-9a-f]{40}$/);
  });

  it('records the Apache-2.0 licence and keeps the notice files', () => {
    expect(pin.upstream.license).toBe('Apache-2.0');
    expect(fs.existsSync(path.join(root, 'LICENSE'))).toBe(true);
    expect(fs.existsSync(path.join(root, 'NOTICE'))).toBe(true);

    const license = fs.readFileSync(path.join(root, 'LICENSE'), 'utf8');
    expect(license).toContain('Apache License');
    expect(license).toContain('Version 2.0');

    const notice = fs.readFileSync(path.join(root, 'NOTICE'), 'utf8');
    expect(notice).toContain(pin.upstream.pinnedCommit);
  });

  it('forbids automatic upstream rebases and dependency bumps', () => {
    expect(pin.updatePolicy.automaticRebase).toBe(false);
    expect(pin.updatePolicy.automaticDependencyUpdates).toBe(false);
    expect(pin.updatePolicy.procedure).toBeTruthy();
  });

  it('has no automated upstream sync configured in the repository', () => {
    const repoRoot = path.join(root, '..');
    const workflowDir = path.join(repoRoot, '.github', 'workflows');
    const workflows = fs.existsSync(workflowDir)
      ? fs.readdirSync(workflowDir)
      : [];

    for (const workflow of workflows) {
      const body = fs.readFileSync(path.join(workflowDir, workflow), 'utf8');
      expect(body).not.toContain('boxyhq/saas-starter-kit');
    }

    // Upstream shipped a Dependabot config; an inherited one would update the
    // fork's dependencies without review.
    expect(fs.existsSync(path.join(root, '.github'))).toBe(false);
  });

  it('points at a hardening manifest that exists', () => {
    const manifest = path.join(root, '..', pin.import.manifest);
    expect(fs.existsSync(manifest)).toBe(true);
  });
});
