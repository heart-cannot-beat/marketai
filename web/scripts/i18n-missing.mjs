#!/usr/bin/env node
/**
 * Lists the keys each locale is missing or has in excess, namespace by
 * namespace, against the source language `en`.
 *
 *   node scripts/i18n-missing.mjs              # report, exit 1 on any gap
 *   node scripts/i18n-missing.mjs --json       # same, machine-readable
 *   node scripts/i18n-missing.mjs --locales-dir <dir>
 *
 * Nested namespaces (library, products, scale) are compared by their dotted
 * key paths. A key whose `{{placeholders}}` differ from the English value is
 * reported too: i18next would print the placeholder literally.
 * `__tests__/locales/key-parity.spec.ts` enforces the same rule in CI; run
 * this to see the whole list at once.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SOURCE_LOCALE = 'en';

const args = process.argv.slice(2);
const json = args.includes('--json');
const dirFlag = args.indexOf('--locales-dir');
const localesDir =
  dirFlag >= 0
    ? path.resolve(args[dirFlag + 1])
    : path.resolve(
        path.dirname(fileURLToPath(import.meta.url)),
        '..',
        'locales'
      );

const flatten = (value, prefix = '', out = new Map()) => {
  for (const [key, child] of Object.entries(value)) {
    const keyPath = prefix ? `${prefix}.${key}` : key;
    if (child && typeof child === 'object' && !Array.isArray(child)) {
      flatten(child, keyPath, out);
    } else {
      out.set(keyPath, child);
    }
  }
  return out;
};

const placeholders = (value) =>
  typeof value === 'string'
    ? Array.from(value.matchAll(/{{\s*([^}\s]+)\s*}}/g), (match) => match[1])
        .sort()
        .join(',')
    : '';

const readNamespace = (locale, namespace) =>
  flatten(
    JSON.parse(
      fs.readFileSync(
        path.join(localesDir, locale, `${namespace}.json`),
        'utf8'
      )
    )
  );

const namespacesOf = (locale) => {
  const dir = path.join(localesDir, locale);
  return fs.existsSync(dir)
    ? fs
        .readdirSync(dir)
        .filter((file) => file.endsWith('.json'))
        .map((file) => file.slice(0, -'.json'.length))
        .sort()
    : [];
};

const locales = fs
  .readdirSync(localesDir)
  .filter((entry) => fs.statSync(path.join(localesDir, entry)).isDirectory())
  .filter((entry) => entry !== SOURCE_LOCALE)
  .sort();

const sourceNamespaces = namespacesOf(SOURCE_LOCALE);
const report = [];

for (const locale of locales) {
  const targetNamespaces = namespacesOf(locale);
  for (const namespace of targetNamespaces) {
    if (!sourceNamespaces.includes(namespace)) {
      report.push({ locale, namespace, problem: 'extra-namespace' });
    }
  }
  for (const namespace of sourceNamespaces) {
    if (!targetNamespaces.includes(namespace)) {
      report.push({ locale, namespace, problem: 'missing-namespace' });
      continue;
    }
    const source = readNamespace(SOURCE_LOCALE, namespace);
    const target = readNamespace(locale, namespace);
    const missing = [...source.keys()].filter((key) => !target.has(key));
    const extra = [...target.keys()].filter((key) => !source.has(key));
    const placeholderMismatch = [...source.keys()].filter(
      (key) =>
        target.has(key) &&
        placeholders(source.get(key)) !== placeholders(target.get(key))
    );
    if (missing.length || extra.length || placeholderMismatch.length) {
      report.push({ locale, namespace, missing, extra, placeholderMismatch });
    }
  }
}

if (json) {
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
} else if (report.length === 0) {
  process.stdout.write(
    `Every locale (${locales.join(', ') || 'none'}) has exactly the keys of ${SOURCE_LOCALE} in ${sourceNamespaces.length} namespaces.\n`
  );
} else {
  for (const entry of report) {
    const where = `${entry.locale}/${entry.namespace}.json`;
    if (entry.problem === 'missing-namespace') {
      process.stdout.write(`${where}: missing (en has this namespace)\n`);
      continue;
    }
    if (entry.problem === 'extra-namespace') {
      process.stdout.write(`${where}: no such namespace in en\n`);
      continue;
    }
    for (const key of entry.missing) {
      process.stdout.write(`${where}: missing  ${key}\n`);
    }
    for (const key of entry.extra) {
      process.stdout.write(`${where}: extra    ${key}\n`);
    }
    for (const key of entry.placeholderMismatch) {
      process.stdout.write(`${where}: placeholders differ from en  ${key}\n`);
    }
  }
}

process.exitCode = report.length === 0 ? 0 : 1;
