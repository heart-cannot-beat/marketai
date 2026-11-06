import fs from 'fs';
import path from 'path';

const root = path.join(__dirname, '..', '..');

const locale = JSON.parse(
  fs.readFileSync(path.join(root, 'locales', 'en', 'common.json'), 'utf8')
) as Record<string, string>;

const walk = (dir: string, acc: string[] = []): string[] => {
  if (!fs.existsSync(dir)) return acc;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, acc);
    else if (/\.tsx?$/.test(entry.name)) acc.push(full);
  }
  return acc;
};

const stripComments = (source: string) =>
  source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

/**
 * Prose the product can show a person: quoted strings containing a space, plus
 * JSX text nodes. Identifiers, import paths and permission keys never contain a
 * space, so this catches user-facing copy without flagging `team_member` or
 * `models/team`.
 */
const proseIn = (file: string): string[] => {
  const source = stripComments(fs.readFileSync(file, 'utf8'));
  const found: string[] = [];

  // Single-line quoted strings only: prose never spans statements.
  for (const pattern of [
    // `[^\S\n]` is "whitespace that is not a newline": a plain `\s` would let
    // the match hop the line break between two statements.
    /'([^'\n\\]*[^\S\n][^'\n\\]*)'/g,
    /"([^"\n\\]*[^\S\n][^"\n\\]*)"/g,
    /`([^`\n\\]*[^\S\n][^`\n\\]*)`/g,
  ]) {
    for (const match of Array.from(source.matchAll(pattern))) {
      found.push(match[1]);
    }
  }

  // JSX text nodes, e.g.
  //   <Button ...>
  //     Join the Workspace
  //   </Button>
  // Newlines are allowed because the copy usually sits on its own line;
  // excluding `;`, `=`, brackets and quotes keeps the match from wandering
  // across code that happens to sit between a `>` and a `<`.
  if (file.endsWith('.tsx')) {
    for (const match of Array.from(source.matchAll(/>([^<>{};=()'"`]+)</g))) {
      found.push(match[1]);
    }
  }

  return (
    found
      // `${team.name}` is an identifier being interpolated, not the word
      // "team" shown to a reader.
      .map((value) => value.replace(/\$\{[^}]*\}/g, '\u2026'))
      .filter((value) => /\w/.test(value))
  );
};

const surfaces = [
  ...walk(path.join(root, 'components', 'emailTemplates')),
  ...walk(path.join(root, 'lib', 'email')),
  ...walk(path.join(root, 'pages', 'api')),
  ...walk(path.join(root, 'models')),
  ...walk(path.join(root, 'lib', 'zod')),
];

/**
 * `Team` is the upstream Prisma model name and stays that way internally, but
 * the customer-facing vocabulary in CONTEXT.md is Workspace, and the product is
 * MarketAI. Any string a customer can read must say so — including the ones the
 * locale file never sees, such as email templates and API error messages.
 */
describe('Customer-facing vocabulary', () => {
  it('never shows the word "team" in a locale string', () => {
    const offenders = Object.entries(locale).filter(([, value]) =>
      /\bteams?\b/i.test(value)
    );

    expect(offenders).toEqual([]);
  });

  it('shows no upstream branding in a locale string', () => {
    const offenders = Object.entries(locale).filter(([, value]) =>
      /boxyhq/i.test(value)
    );

    expect(offenders).toEqual([]);
    expect(locale['login-title']).toBe('Login | MarketAI');
  });

  it('never shows "team" or upstream branding in email or API prose', () => {
    const offenders: string[] = [];

    for (const file of surfaces) {
      for (const value of proseIn(file)) {
        if (/\bteams?\b/i.test(value) || /boxyhq/i.test(value)) {
          offenders.push(`${path.relative(root, file)}: ${value.trim()}`);
        }
      }
    }

    expect(offenders).toEqual([]);
  });

  it('covers the surfaces that carry copy outside the locale file', () => {
    // Guards the guard: if these directories move, the scan above silently
    // stops checking anything.
    expect(surfaces.length).toBeGreaterThan(20);
    expect(
      surfaces.some((f) =>
        f.endsWith(path.join('emailTemplates', 'TeamInvite.tsx'))
      )
    ).toBe(true);
    expect(
      surfaces.some((f) =>
        f.endsWith(path.join('email', 'sendWelcomeEmail.ts'))
      )
    ).toBe(true);
  });

  it('uses Workspace in the strings that name the tenant', () => {
    expect(locale['all-teams']).toBe('All Workspaces');
    expect(locale['team-settings']).toBe('Workspace Settings');
    expect(locale['team-not-found']).toBe('Workspace not found');
  });

  it('has no string left for a removed capability', () => {
    for (const key of [
      'create-team',
      'new-team',
      'remove-team',
      'share-invitation-link',
      'invitation-link-created',
      'continue-with-saml-sso',
      'api-key',
      'webhooks',
      'audit-logs',
      'pricing',
    ]) {
      expect(locale[key]).toBeUndefined();
    }
  });

  it('keeps every key the application actually renders', () => {
    const files = [
      ...walk(path.join(root, 'pages')),
      ...walk(path.join(root, 'components')),
    ];

    const localeDir = path.join(root, 'locales', 'en');
    const flatten = (
      obj: Record<string, unknown>,
      prefix = '',
      acc: Record<string, string> = {}
    ): Record<string, string> => {
      for (const [k, v] of Object.entries(obj)) {
        const key = prefix ? `${prefix}.${k}` : k;
        if (typeof v === 'object' && v !== null && !Array.isArray(v)) {
          flatten(v as Record<string, unknown>, key, acc);
        } else {
          acc[key] = String(v);
        }
      }
      return acc;
    };
    const namespaceLocales: Record<string, Record<string, string>> = {
      common: locale,
    };
    const loadNs = (ns: string): Record<string, string> => {
      if (!namespaceLocales[ns]) {
        const p = path.join(localeDir, `${ns}.json`);
        namespaceLocales[ns] = fs.existsSync(p)
          ? flatten(
              JSON.parse(fs.readFileSync(p, 'utf8')) as Record<string, unknown>
            )
          : {};
      }
      return namespaceLocales[ns];
    };

    const missing: string[] = [];
    for (const file of files) {
      const source = fs.readFileSync(file, 'utf8');
      const nsMatch = source.match(
        /useTranslation\(\s*['"]([a-z_-]+)['"]\s*\)/
      );
      const ns = nsMatch ? nsMatch[1] : 'common';
      const nsLocale = loadNs(ns);

      const keys = new Set<string>([
        ...Array.from(
          source.matchAll(/(?<![A-Za-z0-9_$])t\(\s*['"]([a-z0-9._-]+)['"]/g)
        ).map((m) => m[1]),
        ...Array.from(source.matchAll(/i18nKey=['"]([a-z0-9._-]+)['"]/g)).map(
          (m) => m[1]
        ),
      ]);

      for (const key of Array.from(keys)) {
        if (!(key in nsLocale)) {
          missing.push(`[${ns}] ${key}`);
        }
      }
    }

    expect(missing).toEqual([]);
  });
});
