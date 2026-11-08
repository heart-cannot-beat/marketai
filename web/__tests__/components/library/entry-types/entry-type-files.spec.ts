/**
 * @jest-environment jsdom
 */
import fs from 'node:fs';
import path from 'node:path';

jest.mock('next-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import {
  getDraftFieldsEditor,
  hasDraftFieldsEditor,
} from '@/components/library/entry-types/draft-editors';
import { getCodec } from '@/lib/library/draft-codecs';
import {
  LIBRARY_ENTRY_TYPES,
  type LibraryEntryType,
} from '@/lib/library/navigation';

const WEB = path.resolve(__dirname, '../../../..');
const ENTRY_TYPES_DIR = path.join(WEB, 'components/library/entry-types');
/** Application source: where a component counts as mounted. */
const APP_DIRS = ['components', 'pages', 'hooks', 'lib'];

/**
 * Every file under `components/library/entry-types/` and why it stays (#21).
 * The editors and viewers there used to include sketches for nine entry
 * types, written with field names the server's validator never reads
 * (`external_object_id` for `external_object.{kind,id}`, `market_code` for
 * `market`, …) and mounted nowhere. They were removed; those types are edited
 * as JSON only. A new file here is either mounted by the application, or the
 * version viewer of an entry type with a mounted form, and is listed below.
 *
 * - `mounted`: imported by application source.
 * - `form-viewer`: a version viewer kept with its entry type's form, which
 *   #21 left unchanged. It is not mounted yet.
 */
const KEPT: Record<string, 'mounted' | 'form-viewer'> = {
  'registry.tsx': 'mounted',
  'draft-editors.ts': 'mounted',
  'product/ProductDraftEditor.tsx': 'mounted',
  'dimension-template/DimensionTemplateDraftEditor.tsx': 'mounted',
  'data-source/DataSourceDraftEditor.tsx': 'mounted',
  'manual-fact-set/ManualFactSetFactList.tsx': 'mounted',
  'product/ProductVersionViewer.tsx': 'form-viewer',
  'dimension-template/DimensionTemplateVersionViewer.tsx': 'form-viewer',
  'data-source/DataSourceVersionViewer.tsx': 'form-viewer',
};

function filesUnder(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((item) => {
    const full = path.join(dir, item.name);
    return item.isDirectory() ? filesUnder(full) : [full];
  });
}

const relative = (full: string) =>
  path.relative(ENTRY_TYPES_DIR, full).split(path.sep).join('/');

/** `market-source-mapping/...` -> `market_source_mapping`. */
const entryTypeOf = (file: string) =>
  file.split('/')[0].replace(/-/g, '_') as LibraryEntryType;

/** Every module application source imports, as an absolute path without
 * extension (`@/…` and relative specifiers; packages are skipped). */
const importedModules = new Set(
  APP_DIRS.flatMap((dir) => filesUnder(path.join(WEB, dir)))
    .filter((file) => /\.tsx?$/.test(file) && !file.endsWith('.d.ts'))
    .flatMap((file) =>
      Array.from(
        fs.readFileSync(file, 'utf8').matchAll(/from '([^']+)'/g),
        (m) => m[1]
      ).flatMap((spec) => {
        if (spec.startsWith('@/')) return [path.join(WEB, spec.slice(2))];
        if (spec.startsWith('.'))
          return [path.resolve(path.dirname(file), spec)];
        return [];
      })
    )
);

describe('components/library/entry-types', () => {
  it('holds only the files listed as kept', () => {
    const files = filesUnder(ENTRY_TYPES_DIR).map(relative).sort();
    expect(files).toEqual(Object.keys(KEPT).sort());
  });

  it.each(
    Object.entries(KEPT)
      .filter(([, kind]) => kind === 'mounted')
      .map(([file]) => [file])
  )('%s is imported by application source', (file: string) => {
    const target = path.join(ENTRY_TYPES_DIR, file.replace(/\.tsx?$/, ''));
    expect(importedModules.has(target)).toBe(true);
  });

  it.each(
    Object.entries(KEPT)
      .filter(([, kind]) => kind === 'form-viewer')
      .map(([file]) => [file])
  )('%s belongs to an entry type with a mounted form', (file: string) => {
    expect(LIBRARY_ENTRY_TYPES).toContain(entryTypeOf(file));
    expect(hasDraftFieldsEditor(entryTypeOf(file))).toBe(true);
  });

  it.each(
    Object.keys(KEPT)
      .filter((file) => file.endsWith('DraftEditor.tsx'))
      .map((file) => [file])
  )('%s is the form mounted for its entry type', (file: string) => {
    const name = path.basename(file, '.tsx');
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const exported = require(path.join(ENTRY_TYPES_DIR, file))[name];
    expect(exported).toBeDefined();
    expect(getDraftFieldsEditor(entryTypeOf(file))).toBe(exported);
  });

  it.each(LIBRARY_ENTRY_TYPES.map((t) => [t]))(
    '%s has a form exactly when its codec knows fields',
    (entryType: string) => {
      const type = entryType as LibraryEntryType;
      const knowsFields =
        Object.keys(getCodec(type).parse({}).fields).length > 0;
      expect(hasDraftFieldsEditor(type)).toBe(knowsFields);
    }
  );
});
