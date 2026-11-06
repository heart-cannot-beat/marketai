// Extends Jest's `expect` with DOM matchers (`toBeInTheDocument`, etc.) for
// jsdom-environment component tests. Runs after the test framework is set
// up, per `setupFilesAfterEnv` semantics.
import '@testing-library/jest-dom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';
import { onMissingErrorCode } from './lib/i18n/domain-errors';
import common from './locales/en/common.json';
import assets from './locales/en/assets.json';
import products from './locales/en/products.json';
import tests from './locales/en/tests.json';
import external from './locales/en/external.json';
import experiments from './locales/en/experiments.json';
import localization from './locales/en/localization.json';
import settings from './locales/en/settings.json';
import competitors from './locales/en/competitors.json';

// Component tests exercise the same translated accessibility and status text
// as production. A synchronous in-memory instance keeps those assertions
// deterministic without booting Next.js' server-side i18n loader.
i18next.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  ns: [
    'common',
    'assets',
    'products',
    'tests',
    'external',
    'experiments',
    'localization',
    'settings',
    'competitors',
  ],
  defaultNS: 'common',
  resources: {
    en: {
      common,
      assets,
      products,
      tests,
      external,
      experiments,
      localization,
      settings,
      competitors,
    },
  },
  interpolation: { escapeValue: false },
  initImmediate: false,
  showSupportNotice: false,
});

// An error code without `error-<code>` copy shows the bare code in the
// interface (#253); in tests it fails instead, so every code a component can
// render has en copy.
onMissingErrorCode((code) => {
  throw new Error(`No "error-${code}" copy in locales/en/common.json`);
});
