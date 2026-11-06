import fs from 'node:fs';
import path from 'node:path';
import { canonicalJson } from '@/lib/canonical-json';

const libraryDriverSource = fs.readFileSync(
  path.join(
    process.cwd(),
    'e2e',
    'golden-path',
    'fixtures',
    'area-drivers',
    'library.ts'
  ),
  'utf8'
);

describe('golden-path topology attestation verification', () => {
  it('uses the same recursive canonical JSON ordering as the Python signer', () => {
    expect(
      canonicalJson({
        version: 1,
        live_phase: {
          violations: [{ rule: 'none', detail: 'safe', service: 'api' }],
          ok: true,
        },
        services: ['web', 'api'],
      })
    ).toBe(
      '{"live_phase":{"ok":true,"violations":[{"detail":"safe","rule":"none","service":"api"}]},"services":["web","api"],"version":1}'
    );
  });

  it('drives the current responsive Library controls', () => {
    expect(libraryDriverSource).not.toContain("page.click('text=");
    expect(libraryDriverSource).not.toMatch(/Create (Product|Offer|Persona)/);
    expect(libraryDriverSource).toContain("page.locator('aside').getByRole");
    expect(libraryDriverSource).toContain("name: 'New entry'");
  });
});
