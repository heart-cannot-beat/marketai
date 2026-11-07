import assets from '../../locales/en/assets.json';
import common from '../../locales/en/common.json';
import competitors from '../../locales/en/competitors.json';
import directions from '../../locales/en/directions.json';
import experiments from '../../locales/en/experiments.json';
import external from '../../locales/en/external.json';
import learnings from '../../locales/en/learnings.json';
import library from '../../locales/en/library.json';
import localization from '../../locales/en/localization.json';
import products from '../../locales/en/products.json';
import scale from '../../locales/en/scale.json';
import settings from '../../locales/en/settings.json';
import tests from '../../locales/en/tests.json';
import vocabulary from '../../locales/en/vocabulary.json';

/**
 * Interface copy for E2E locators and assertions, read from the `en` locale
 * the pages render (#19). `en('localization', 'new-batch.source-label')`
 * follows a wording change instead of timing out on the old text, and a
 * renamed or removed key fails `npm run check-types:e2e`.
 *
 * `__tests__/locales/e2e-copy.spec.ts` checks the copy the suites still
 * write out by hand.
 */
const EN = {
  assets,
  common,
  competitors,
  directions,
  experiments,
  external,
  learnings,
  library,
  localization,
  products,
  scale,
  settings,
  tests,
  vocabulary,
};

export type Namespace = keyof typeof EN;

type PluralForm = 'zero' | 'one' | 'two' | 'few' | 'many' | 'other';

/**
 * The dotted paths to a namespace's strings, as `t('a.b')` takes them. A
 * plural key (`x_one`, `x_other`) also answers to its base `x`, chosen by
 * `count` the way i18next does.
 */
type KeysOf<T, Prefix extends string = ''> = {
  [K in keyof T & string]: T[K] extends string
    ? K extends `${infer Base}_${PluralForm}`
      ? `${Prefix}${K}` | `${Prefix}${Base}`
      : `${Prefix}${K}`
    : KeysOf<T[K], `${Prefix}${K}.`>;
}[keyof T & string];

export type CopyKey<N extends Namespace> = KeysOf<(typeof EN)[N]>;

export type CopyValues = Record<string, string | number>;

const pluralRules = new Intl.PluralRules('en');

function lookup(namespace: Namespace, key: string): unknown {
  let node: unknown = EN[namespace];
  for (const part of key.split('.')) {
    if (!node || typeof node !== 'object') return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return node;
}

/**
 * The `en` text of `namespace:key`, with each `{{name}}` filled from
 * `values`. A plural base key picks its form from `values.count`. A missing
 * key or value throws, so a locator never matches a half-filled sentence.
 */
export function en<N extends Namespace>(
  namespace: N,
  key: CopyKey<N>,
  values: CopyValues = {}
): string {
  let text = lookup(namespace, key);
  if (typeof text !== 'string' && values.count !== undefined) {
    const form = pluralRules.select(Number(values.count));
    text =
      lookup(namespace, `${key}_${form}`) ?? lookup(namespace, `${key}_other`);
  }
  if (typeof text !== 'string') {
    throw new Error(`No en copy for ${namespace}:${key}`);
  }
  return text.replace(/{{\s*([^}\s]+)\s*}}/g, (_, name: string) => {
    if (values[name] === undefined) {
      throw new Error(`en copy ${namespace}:${key} needs a value for ${name}`);
    }
    return String(values[name]);
  });
}
