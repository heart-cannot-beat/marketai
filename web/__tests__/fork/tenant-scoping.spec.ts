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

const stripComments = (source: string) =>
  source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

const sources = [
  ...walk(path.join(root, 'pages')),
  ...walk(path.join(root, 'models')),
  ...walk(path.join(root, 'lib')),
];

const prismaSchema = fs.readFileSync(
  path.join(root, 'prisma', 'schema.prisma'),
  'utf8'
);

/**
 * Prisma drops an `undefined` filter instead of matching nothing, so
 * `where: { userId: undefined }` is an unfiltered query that returns every
 * user's rows. `getSession()` returns `null` whenever the token is missing or
 * bound to a revoked session — including when it is revoked between the
 * middleware check and the handler — and `session?.user.id` on that `null` is
 * exactly `undefined`.
 *
 * See https://www.prisma.io/docs/orm/prisma-client/special-fields-and-types/null-and-undefined
 */
describe('User-scoped queries cannot degrade into unfiltered ones', () => {
  it('never reads an id off an optional session', () => {
    const offenders: string[] = [];

    for (const file of sources) {
      const relative = path.relative(root, file);

      // requireSession is where the null case is turned into a 401.
      if (relative === path.join('lib', 'requireSession.ts')) continue;

      const body = stripComments(fs.readFileSync(file, 'utf8'));

      for (const match of Array.from(
        body.matchAll(/session\?\.\s*user[\s\S]{0,40}/g)
      )) {
        offenders.push(`${relative}: ${match[0].split('\n')[0].trim()}`);
      }
    }

    expect(offenders).toEqual([]);
  });

  it('never casts a possibly-undefined id to string', () => {
    const offenders: string[] = [];

    for (const file of sources) {
      const body = stripComments(fs.readFileSync(file, 'utf8'));

      if (/user\.id as string/.test(body)) {
        offenders.push(path.relative(root, file));
      }
    }

    expect(offenders).toEqual([]);
  });

  it('routes every session consumer through requireSession', () => {
    // `getSession` itself stays available for requireSession; everything else
    // must go through the guard.
    const direct: string[] = [];

    for (const file of sources) {
      const relative = path.relative(root, file);

      if (
        relative === path.join('lib', 'requireSession.ts') ||
        relative === path.join('lib', 'session.ts')
      ) {
        continue;
      }

      const body = stripComments(fs.readFileSync(file, 'utf8'));

      if (/\bgetSession\(/.test(body)) {
        direct.push(relative);
      }
    }

    expect(direct).toEqual([]);
  });

  it('keeps Prisma strictUndefinedChecks on as a backstop', () => {
    expect(prismaSchema).toMatch(
      /previewFeatures\s*=\s*\[[^\]]*"strictUndefinedChecks"/
    );
  });
});
