/**
 * @jest-environment jsdom
 */
import fs from 'node:fs';
import path from 'node:path';
import { createElement, type ReactNode } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import serverContent from '../../fixtures/library-draft-content.json';

jest.mock('next-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (key: string) => `${ns}:${key}`,
  }),
}));

const mockSaveRun = jest.fn(async () => ({
  kind: 'success',
  data: { revision: 2 },
  meta: { etag: 'etag-2' },
}));

jest.mock('@/hooks/library/useLibraryCommand', () => ({
  useLibraryCommand: () => ({
    run: mockSaveRun,
    reset: jest.fn(),
    retry: jest.fn(),
    dismiss: jest.fn(),
    isPending: false,
    problem: undefined,
    reconciliation: undefined,
  }),
}));

jest.mock('@/hooks/library/useLibraryCache', () => ({
  useLibraryCache: () => ({ invalidate: jest.fn(async () => {}) }),
}));

jest.mock('@/components/workspace/NavigationBlocker', () => ({
  useNavigationBlocker: () => ({
    register: () => () => {},
    navigate: jest.fn(),
    isBlocked: () => false,
  }),
  NavigationBlockerProvider: ({ children }: { children: ReactNode }) =>
    createElement('div', null, children),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => ({}),
}));

import { RawJsonDraftEditor } from '@/components/library/draft/RawJsonDraftEditor';
import { hasDraftFieldsEditor } from '@/components/library/entry-types/draft-editors';
import { useDraftSession } from '@/hooks/library/useDraftSession';
import {
  LIBRARY_ENTRY_TYPES,
  type LibraryEntryType,
} from '@/lib/library/navigation';

/** One content per entry type with the server's field names only; each
 * passes the server's content validator
 * (`tests/test_library_web_draft_content.py`). */
const SERVER_CONTENT = serverContent as Record<
  LibraryEntryType,
  Record<string, unknown>
>;
const MAPPING = serverContent.market_source_mapping;
const JSON_ONLY_TYPES = LIBRARY_ENTRY_TYPES.filter(
  (entryType) => !hasDraftFieldsEditor(entryType)
);

const CONTRACTS = fs.readFileSync(
  path.resolve(__dirname, '../../../../app/library/contracts.py'),
  'utf8'
);

/** The body of a top-level function of `app/library/contracts.py`. */
function pythonFunction(name: string): string {
  const start = CONTRACTS.search(new RegExp(`^def ${name}\\(`, 'm'));
  expect(start).toBeGreaterThanOrEqual(0);
  const end = CONTRACTS.indexOf('\ndef ', start + 1);
  return CONTRACTS.slice(start, end === -1 ? undefined : end);
}

const quoted = (text: string) =>
  Array.from(text.matchAll(/"(\w+)"/g), (m) => m[1]);

const unique = (items: string[]) => Array.from(new Set(items)).sort();

/** The draft page's JSON editing path: the real session and codec under the
 * JSON editor, with a Save button. */
function JsonDraft({
  entryType,
  content,
}: {
  entryType: LibraryEntryType;
  content: Record<string, unknown>;
}) {
  const session = useDraftSession('e1', entryType, {
    id: 'd1',
    entry_id: 'e1',
    revision: 1,
    state: 'editing',
    content_schema_version: 1,
    content,
    content_hash: 'h1',
    etag: 'etag-1',
  } as Parameters<typeof useDraftSession>[2]);
  return createElement(
    'div',
    null,
    createElement(RawJsonDraftEditor, { entryType, session }),
    createElement('button', { onClick: () => void session.save() }, 'save')
  );
}

const jsonText = () =>
  (screen.getByLabelText('library:draft.json-label') as HTMLTextAreaElement)
    .value;

/** Types `content` into the JSON editor, saves, and returns what was sent. */
async function typeAndSave(content: Record<string, unknown>) {
  fireEvent.change(screen.getByLabelText('library:draft.json-label'), {
    target: { value: JSON.stringify(content) },
  });
  fireEvent.click(screen.getByText('save'));
  await waitFor(() => expect(mockSaveRun).toHaveBeenCalledTimes(1));
  const input = (mockSaveRun.mock.calls[0] as unknown[])[0] as {
    body: { content: Record<string, unknown> };
  };
  return input.body.content;
}

beforeEach(() => {
  mockSaveRun.mockClear();
});

describe('market source mapping drafts (#21)', () => {
  const validator = pythonFunction('_validate_market_mapping');

  it('the fixture writes exactly the fields `_validate_market_mapping` reads', () => {
    const contentKeys = unique(
      Array.from(validator.matchAll(/content\.get\("(\w+)"\)/g), (m) => m[1])
    );
    const externalKeys = unique(
      Array.from(validator.matchAll(/external\.get\("(\w+)"\)/g), (m) => m[1])
    );
    expect(contentKeys).toEqual([
      'data_source_version_id',
      'execution_mode',
      'external_object',
      'market',
      'offer_version_id',
    ]);
    expect(unique(Object.keys(MAPPING))).toEqual(contentKeys);
    expect(unique(Object.keys(MAPPING.external_object))).toEqual(externalKeys);
  });

  it('the fixture takes its values from the ones the validator accepts', () => {
    const kinds = validator.match(/kind not in \{([^}]*)\}/);
    expect(kinds).not.toBeNull();
    expect(quoted(kinds![1])).toContain(MAPPING.external_object.kind);

    expect(validator).toMatch(
      /content\.get\("execution_mode"\) not in EXECUTION_MODES/
    );
    const members = CONTRACTS.match(
      /^EXECUTION_MODES = frozenset\(\{(.*)\}\)$/m
    );
    expect(members).not.toBeNull();
    const modes = members![1].split(',').map((name) => {
      const constant = CONTRACTS.match(
        new RegExp(`^${name.trim()} = "(\\w+)"$`, 'm')
      );
      expect(constant).not.toBeNull();
      return constant![1];
    });
    expect(modes).toContain(MAPPING.execution_mode);

    expect(validator).toMatch(/len\(market\.strip\(\)\) != 2/);
    expect(MAPPING.market.trim()).toHaveLength(2);
  });

  it('a new mapping draft opens in the JSON editor with no field filled in', () => {
    render(
      createElement(JsonDraft, {
        entryType: 'market_source_mapping',
        content: {},
      })
    );
    expect(JSON.parse(jsonText())).toEqual({});
  });

  it('saves a mapping written with the server fields exactly as typed', async () => {
    render(
      createElement(JsonDraft, {
        entryType: 'market_source_mapping',
        content: {},
      })
    );
    const saved = await typeAndSave(MAPPING);
    expect(JSON.stringify(saved)).toBe(JSON.stringify(MAPPING));
  });
});

describe('JSON editor round trip (#21)', () => {
  it.each(JSON_ONLY_TYPES.map((t) => [t]))(
    '%s opens as the stored content and nothing else',
    (entryType: string) => {
      const content = SERVER_CONTENT[entryType as LibraryEntryType];
      render(
        createElement(JsonDraft, {
          entryType: entryType as LibraryEntryType,
          content,
        })
      );
      expect(jsonText()).toBe(JSON.stringify(content, null, 2));
    }
  );

  it.each(LIBRARY_ENTRY_TYPES.map((t) => [t]))(
    '%s saves server-shaped content unchanged',
    async (entryType: string) => {
      const content = SERVER_CONTENT[entryType as LibraryEntryType];
      render(
        createElement(JsonDraft, {
          entryType: entryType as LibraryEntryType,
          content: {},
        })
      );
      expect(await typeAndSave(content)).toEqual(content);
    }
  );
});
