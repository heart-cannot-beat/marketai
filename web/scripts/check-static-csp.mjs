#!/usr/bin/env node
/**
 * Assert the prerendered pages are compatible with the nonce-based CSP.
 *
 * Statically generated pages are rendered at build time, so they cannot carry a
 * per-request nonce. Two things must hold for them to load under
 * `script-src 'self' 'nonce-…'`:
 *
 *   1. no `nonce=""` attribute is written into the build output, and
 *   2. every inline `<script>` is a non-executable data block
 *      (`type="application/json"`), so nothing needs a nonce in the first place.
 *
 * Runs against the real output of `next build`.
 */
import { readdirSync, readFileSync, existsSync } from 'fs';
import { join, dirname, relative } from 'path';
import { fileURLToPath } from 'url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const pagesDir = join(root, '.next', 'server', 'pages');

if (!existsSync(pagesDir)) {
  process.stderr.write(
    'No build output at .next/server/pages. Run `npm run build` first.\n'
  );
  process.exit(1);
}

const walk = (dir, acc = []) => {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) walk(full, acc);
    else if (entry.name.endsWith('.html')) acc.push(full);
  }
  return acc;
};

const html = walk(pagesDir);
const problems = [];

for (const file of html) {
  const body = readFileSync(file, 'utf8');
  const name = relative(root, file);

  if (/nonce=""/.test(body)) {
    problems.push(`${name}: contains an empty nonce attribute`);
  }

  for (const match of body.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)) {
    const attrs = match[1];

    if (/\ssrc=/.test(attrs)) continue;
    if (/type="application\/json"/.test(attrs)) continue;

    problems.push(
      `${name}: prerendered page has an executable inline script (${attrs.trim() || 'no attributes'})`
    );
  }
}

if (problems.length) {
  process.stderr.write(
    `Static pages are not CSP-compatible:\n  ${problems.join('\n  ')}\n`
  );
  process.exit(1);
}

process.stdout.write(
  `Checked ${html.length} prerendered page(s): no empty nonce, no executable inline script\n`
);
