/**
 * @jest-environment jsdom
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import { SWRConfig } from 'swr';
import enLibrary from '../../../locales/en/library.json';
import zhCommon from '../../../locales/zh-CN/common.json';
import zhLibrary from '../../../locales/zh-CN/library.json';
import { TestFileDigest } from '@/lib/domain-client/file-digest';
import {
  BlobClientProvider,
  FileDigestProvider,
} from '@/lib/domain-client/upload-deps';
import type { BrowserBlobTransferClient } from '@/lib/domain-client/blob-transfer';
import { getEntryTypeDefinition } from '@/components/library/entry-types/registry';
import type { LibraryRouteState, LibraryTab } from '@/lib/library/route-codec';

/**
 * The detail page of a Manual Fact Set (#23a). Its Facts tab lists the
 * facts of the current version (or of the draft, marked as such) with a
 * link to the data source they came from; it no longer mounts the data
 * source's import panel, which sent the fact set's own id to the import
 * route. Only the gateway, the storage transfer, the digest, the workspace
 * and routing are replaced; the tabs, hooks and copy are the real ones.
 */
const FACT_SET = 'lib_fs_1';
const DATA_SOURCE = 'lib_ds_1';
const VERSION = 'ver_fs_1';
const META = { etag: null, retryAt: 0, location: null };

const mockSetEntry = jest.fn();

/**
 * Every gateway method the page touches. A method no test names answers
 * 404 and is recorded, so an unexpected call still shows up in `calls`.
 */
const known: Record<string, jest.Mock> = {
  getLibraryEntry: jest.fn(),
  getLibraryVersion: jest.fn(),
  createUploadIntent: jest.fn(),
  finalizeUpload: jest.fn(),
  pollOperation: jest.fn(),
  importLibraryManualFacts: jest.fn(),
};
const mockGateway = new Proxy(known, {
  get(target, name) {
    if (typeof name !== 'string' || name === 'then') return undefined;
    if (!(name in target)) {
      target[name] = jest.fn(async () => problem(404));
    }
    return target[name];
  },
});

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'w1', role: 'OWNER' },
  }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/hooks/library/useLibraryRoute', () => ({
  useLibraryRoute: () => ({
    state: {},
    setCategory: jest.fn(),
    setEntry: mockSetEntry,
    setTab: jest.fn(),
    setVersion: jest.fn(),
    setResource: jest.fn(),
    setResourceTab: jest.fn(),
    setResourceVersion: jest.fn(),
  }),
}));
const mockRouter = {
  query: { slug: 'acme' },
  asPath: '/w/acme/library',
  push: jest.fn(),
  replace: jest.fn(),
  beforePopState: jest.fn(),
  events: { on: jest.fn(), off: jest.fn(), emit: jest.fn() },
};
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

import { LibraryEntryDetail } from '@/components/library/detail/LibraryEntryDetail';
import { NavigationBlockerProvider } from '@/components/workspace/NavigationBlocker';

const blobClient = {
  upload: jest.fn(),
  validatedDownloadUrl: jest.fn(),
  downloadBlob: jest.fn(),
} as unknown as jest.Mocked<BrowserBlobTransferClient>;

function problem(status: number) {
  return {
    kind: 'problem',
    status,
    problem: {
      type: 'about:blank',
      title: 'Problem',
      status,
      detail: '',
      instance: '',
      code: status === 404 ? 'not_found' : 'internal_error',
      trace_id: 'trace_1',
      retryable: false,
      field_errors: [],
    },
    meta: META,
  };
}

const ok = (data: unknown) => ({ kind: 'ok', status: 200, data, meta: META });

const FACTS = [
  {
    fact_id: 'price-us-standard',
    fact_type: 'price',
    country_code: 'US',
    amount: '49.00',
    currency: 'USD',
    valid_from: '2026-11-01',
    valid_to: '2026-12-31',
  },
  {
    fact_id: 'tax-de',
    fact_type: 'tax',
    country_code: 'DE',
    rate: '0.19',
    valid_from: '2026-11-01',
    valid_to: '2027-06-30',
  },
];

const CONTENT = {
  data_source_entry_id: DATA_SOURCE,
  upload_intent_id: 'upi_1',
  facts: FACTS,
};

function entry(overrides: Record<string, unknown> = {}) {
  return {
    id: FACT_SET,
    entry_type: 'manual_fact_set',
    display_name: 'Acme Charge launch facts',
    lifecycle: 'active',
    current_version_id: VERSION,
    draft: null,
    revision: 3,
    allowed_actions: [],
    ...overrides,
  };
}

function version(content: unknown = CONTENT) {
  return {
    id: VERSION,
    entry_id: FACT_SET,
    entry_type: 'manual_fact_set',
    version_no: 2,
    state: 'effective',
    eligibility: 'eligible',
    content_schema_version: 1,
    lifecycle_revision: 1,
    content,
    content_hash: 'sha256:0000',
    published_at: '2026-11-05T09:00:00+00:00',
  };
}

const draft = (content: unknown) => ({
  id: 'drf_fs_1',
  entry_id: FACT_SET,
  revision: 1,
  state: 'editing',
  content_schema_version: 1,
  content,
});

function route(entryId: string, tab: LibraryTab | null): LibraryRouteState {
  return {
    cat: 'datasource',
    entry: entryId,
    tab,
    version: null,
    resource_type: null,
    resource: null,
    resource_tab: null,
    resource_version: null,
    parent_type: null,
    parent: null,
  };
}

function renderDetail(
  entryId: string,
  tab: LibraryTab | null,
  language: 'en' | 'zh-CN' = 'en'
) {
  const i18n = getI18n().cloneInstance({
    lng: language,
    fallbackLng: false,
    forkResourceStore: true,
  });
  // The Jest setup loads `common` but not `library`.
  i18n.addResourceBundle('en', 'library', enLibrary);
  if (language === 'zh-CN') {
    i18n.addResourceBundle('zh-CN', 'common', zhCommon);
    i18n.addResourceBundle('zh-CN', 'library', zhLibrary);
  }
  void i18n.changeLanguage(language);
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <I18nextProvider i18n={i18n}>
        <NavigationBlockerProvider>
          <FileDigestProvider digest={new TestFileDigest()}>
            <BlobClientProvider client={blobClient}>
              <LibraryEntryDetail
                route={route(entryId, tab)}
                onTabChange={jest.fn()}
                onVersionChange={jest.fn()}
                onDismissResource={jest.fn()}
              />
            </BlobClientProvider>
          </FileDigestProvider>
        </NavigationBlockerProvider>
      </I18nextProvider>
    </SWRConfig>
  );
}

/** Feeds a facts file to every file input on the page. */
async function chooseAFileEverywhere(container: HTMLElement) {
  const inputs = Array.from(
    container.querySelectorAll<HTMLInputElement>('input[type="file"]')
  );
  for (const input of inputs) {
    const file = new File(['{"facts":[]}'], 'facts.json', {
      type: 'application/json',
    });
    fireEvent.change(input, { target: { files: [file] } });
  }
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
  return inputs.length;
}

/** Each row of the facts table, as its cells' text. */
function factRows() {
  const table = screen.getByRole('table');
  return within(table)
    .getAllByRole('row')
    .slice(1)
    .map((row) =>
      within(row)
        .getAllByRole('cell')
        .map((cell) => cell.textContent)
    );
}

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  localStorage.clear();
  blobClient.upload.mockResolvedValue(undefined);
  known.getLibraryEntry.mockImplementation(async ({ entryId }) =>
    entryId === FACT_SET
      ? ok(entry())
      : ok({
          id: DATA_SOURCE,
          entry_type: 'data_source',
          display_name: 'Acme Charge pricing sheet',
          lifecycle: 'active',
          current_version_id: null,
          draft: null,
          revision: 1,
          allowed_actions: [],
        })
  );
  known.getLibraryVersion.mockResolvedValue(ok(version()));
  known.createUploadIntent.mockResolvedValue({
    kind: 'created',
    status: 201,
    data: {
      intent_id: 'upi_1',
      state: 'pending',
      purpose: 'library_manual_fact_import',
      expires_at: '2026-11-08T08:05:00+00:00',
      upload_grant: {
        kind: 'signed_put',
        url: 'https://storage.example/upload/upi_1',
        expires_at: '2026-11-08T08:05:00+00:00',
      },
    },
    meta: META,
  });
  known.finalizeUpload.mockResolvedValue({
    kind: 'accepted',
    status: 202,
    data: { operation_id: 'op_upload_1', state: 'queued' },
    meta: META,
  });
  known.pollOperation.mockResolvedValue(
    ok({
      operation_id: 'op_upload_1',
      command_type: 'verify_upload_intent',
      state: 'succeeded',
      result: null,
      created_at: '2026-11-08T08:00:00+00:00',
      updated_at: '2026-11-08T08:00:01+00:00',
    })
  );
  known.importLibraryManualFacts.mockResolvedValue({
    kind: 'accepted',
    status: 202,
    data: {
      operation_id: 'op_import_1',
      state: 'queued',
      command_type: 'manual_fact_import',
    },
    meta: META,
  });
});

describe('Manual Fact Set detail: no import with the fact set id', () => {
  const tabs = getEntryTypeDefinition('manual_fact_set').supportedTabs;

  it.each(tabs.map((tab) => [tab]))(
    'the %s tab never starts an import or an upload',
    async (tab) => {
      const { container } = renderDetail(FACT_SET, tab);
      await screen.findByText('Acme Charge launch facts');
      await chooseAFileEverywhere(container);

      expect(known.importLibraryManualFacts).not.toHaveBeenCalled();
      expect(known.createUploadIntent).not.toHaveBeenCalled();
      const calls = Object.values(known).flatMap((fn) => fn.mock.calls);
      expect(JSON.stringify(calls)).not.toMatch(/manual-fact-imports/);
    }
  );

  it('offers no file to import on the Facts tab', async () => {
    const { container } = renderDetail(FACT_SET, 'facts');
    await screen.findByText('price-us-standard');
    expect(container.querySelector('input[type="file"]')).toBeNull();
    expect(
      screen.queryByRole('button', { name: 'Select file' })
    ).not.toBeInTheDocument();
  });
});

describe('Manual Fact Set Facts tab', () => {
  it('lists the facts of the current version with a link to their data source', async () => {
    renderDetail(FACT_SET, 'facts');

    expect(await screen.findByText('Facts in version 2')).toBeInTheDocument();
    expect(known.getLibraryVersion).toHaveBeenCalledWith(
      expect.objectContaining({ versionId: VERSION })
    );
    expect(
      screen.getAllByRole('columnheader').map((cell) => cell.textContent)
    ).toEqual(['Fact ID', 'Type', 'Market', 'Amount', 'Valid', 'Other values']);
    expect(factRows()).toEqual([
      [
        'price-us-standard',
        'price',
        'US',
        '49.00 USD',
        '2026-11-01 to 2026-12-31',
        '—Not set',
      ],
      [
        'tax-de',
        'tax',
        'DE',
        '—Not set',
        '2026-11-01 to 2027-06-30',
        'rate: 0.19',
      ],
    ]);
    expect(
      screen.queryByText('This draft has not been published yet.')
    ).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole('button', { name: 'Open the data source' })
    );
    expect(mockSetEntry).toHaveBeenCalledWith(DATA_SOURCE, 'overview');
  });

  it('reads the draft when nothing is published yet, and says it is a draft', async () => {
    known.getLibraryEntry.mockResolvedValue(
      ok(
        entry({
          current_version_id: null,
          draft: draft({ ...CONTENT, facts: [FACTS[1]] }),
        })
      )
    );
    renderDetail(FACT_SET, 'facts');

    expect(await screen.findByText('Facts in the draft')).toBeInTheDocument();
    expect(
      screen.getByText('This draft has not been published yet.')
    ).toBeInTheDocument();
    expect(factRows()).toEqual([
      [
        'tax-de',
        'tax',
        'DE',
        '—Not set',
        '2026-11-01 to 2027-06-30',
        'rate: 0.19',
      ],
    ]);
    expect(known.getLibraryVersion).not.toHaveBeenCalled();
    expect(
      screen.getByRole('button', { name: 'Open the data source' })
    ).toBeInTheDocument();
  });

  it('says so when the fact set has no facts', async () => {
    known.getLibraryVersion.mockResolvedValue(
      ok(version({ ...CONTENT, facts: [] }))
    );
    renderDetail(FACT_SET, 'facts');

    expect(
      await screen.findByText('This fact set has no facts yet.')
    ).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('says so when there is neither a version nor a draft', async () => {
    known.getLibraryEntry.mockResolvedValue(
      ok(entry({ current_version_id: null, draft: null }))
    );
    renderDetail(FACT_SET, 'facts');

    expect(
      await screen.findByText('This fact set has no facts yet.')
    ).toBeInTheDocument();
    expect(known.getLibraryVersion).not.toHaveBeenCalled();
  });

  it('says the facts could not be read, and reads them again on retry', async () => {
    known.getLibraryVersion.mockResolvedValueOnce(problem(500));
    renderDetail(FACT_SET, 'facts');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Something went wrong'
    );
    expect(screen.queryByRole('table')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('price-us-standard')).toBeInTheDocument();
    expect(known.getLibraryVersion).toHaveBeenCalledTimes(2);
  });

  it('says the facts cannot be shown when the content holds no fact list', async () => {
    known.getLibraryVersion.mockResolvedValue(
      ok(version({ _unsupported: true, _schema_version: 9, _known_version: 1 }))
    );
    renderDetail(FACT_SET, 'facts');

    expect(
      await screen.findByText('These facts cannot be shown here.')
    ).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('reads in Chinese', async () => {
    known.getLibraryEntry.mockResolvedValue(
      ok(entry({ current_version_id: null, draft: draft(CONTENT) }))
    );
    renderDetail(FACT_SET, 'facts', 'zh-CN');

    expect(await screen.findByText('草稿中的事实')).toBeInTheDocument();
    expect(screen.getByText('这是草稿，还没有发布。')).toBeInTheDocument();
    expect(
      screen.getAllByRole('columnheader').map((cell) => cell.textContent)
    ).toEqual(['事实 ID', '类型', '市场', '金额', '有效期', '其他值']);
    expect(factRows()[0]).toEqual([
      'price-us-standard',
      'price',
      'US',
      '49.00 USD',
      '2026-11-01 至 2026-12-31',
      '—未填写',
    ]);
    expect(
      screen.getByRole('button', { name: '打开来源数据源' })
    ).toBeInTheDocument();
  });
});

describe('Data source Facts tab', () => {
  it('still imports into the data source itself', async () => {
    const { container } = renderDetail(DATA_SOURCE, 'facts');
    await screen.findByText('Acme Charge pricing sheet');

    expect(await chooseAFileEverywhere(container)).toBe(1);
    await waitFor(() =>
      expect(known.createUploadIntent).toHaveBeenCalledTimes(1)
    );
    expect(JSON.stringify(known.createUploadIntent.mock.calls[0])).toContain(
      DATA_SOURCE
    );
    expect(JSON.stringify(known.createUploadIntent.mock.calls)).not.toContain(
      FACT_SET
    );
  });
});
