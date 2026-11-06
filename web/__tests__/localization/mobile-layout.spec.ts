import fs from 'node:fs';
import path from 'node:path';

/**
 * At 390px a responsive grid with no base column template (`grid
 * md:grid-cols-2`) gets one implicit `auto` track, which grows to its widest
 * unbreakable token — a JSON snapshot, a hash, an id — and scrolls the whole
 * page sideways (#286). `grid-cols-1` is `minmax(0, 1fr)`, so long values wrap
 * instead. The Advanced workbench declares it on every responsive grid.
 */
const WEB = path.resolve(__dirname, '../..');
const FOLDERS = ['components/localization', 'components/experiments'];

const CLASS_LITERAL = /className=["'`]([^"'`]*)["'`]/g;
const RESPONSIVE_COLUMNS = /(?:^|\s)(?:sm|md|lg|xl|2xl):grid-cols-/;
const BASE_COLUMNS = /(?:^|\s)grid-cols-/;

export const gridsWithoutMobileColumns = (source: string): string[] =>
  Array.from(source.matchAll(CLASS_LITERAL), (match) => match[1]).filter(
    (classes) =>
      /(?:^|\s)grid(?:\s|$)/.test(classes) &&
      RESPONSIVE_COLUMNS.test(classes) &&
      !BASE_COLUMNS.test(classes)
  );

describe('Advanced workbench at 390px', () => {
  const files = FOLDERS.flatMap((folder) =>
    fs
      .readdirSync(path.join(WEB, folder))
      .filter((name) => name.endsWith('.tsx'))
      .map((name) => path.join(folder, name))
  );

  it('has workbench components to check', () => {
    expect(files.length).toBeGreaterThanOrEqual(6);
  });

  it.each(files)('%s gives every responsive grid a mobile column', (file) => {
    const source = fs.readFileSync(path.join(WEB, file), 'utf8');
    expect(gridsWithoutMobileColumns(source)).toEqual([]);
  });

  it('reports a grid that only sets columns from a breakpoint up', () => {
    expect(
      gridsWithoutMobileColumns(
        '<div className="mt-3 grid gap-2 md:grid-cols-2" /><div className="grid grid-cols-1 md:grid-cols-2" />'
      )
    ).toEqual(['mt-3 grid gap-2 md:grid-cols-2']);
  });
});
