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
    // Each row names its Offer entry (#54); `tax-de` stands for a row
    // published before #54, which names none.
    offer_entry_id: 'lib_offer_acme_65w',
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

const draft = (content: unknown, validation: unknown = null) => ({
  id: 'drf_fs_1',
  entry_id: FACT_SET,
  revision: 1,
  state: validation ? 'validation_failed' : 'editing',
  content_schema_version: 1,
  content,
  validation,
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
    ).toEqual([
      'Fact ID',
      'Type',
      'Market',
      'Offer',
      'Amount',
      'Valid',
      'Other values',
    ]);
    expect(factRows()).toEqual([
      [
        'price-us-standard',
        'price',
        'US',
        'lib_offer_acme_65w',
        '49.00 USD',
        '2026-11-01 to 2026-12-31',
        '—Not set',
      ],
      [
        'tax-de',
        'tax',
        'DE',
        '—Not set',
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
    ).toEqual([
      '事实 ID',
      '类型',
      '市场',
      '销售方案',
      '金额',
      '有效期',
      '其他值',
    ]);
    expect(factRows()[0]).toEqual([
      'price-us-standard',
      'price',
      'US',
      'lib_offer_acme_65w',
      '49.00 USD',
      '2026-11-01 至 2026-12-31',
      '—未填写',
    ]);
    expect(
      screen.getByRole('button', { name: '打开来源数据源' })
    ).toBeInTheDocument();
  });
});

/** An imported file whose rows have problems (#23): the import still created
 * the draft, and stored the publish validation's report on it. */
const IMPORTED_FACTS = [
  {
    fact_id: 'price-us',
    fact_type: 'price',
    country_code: 'US',
    amount: '49.00',
    valid_from: '2026-11-01',
    valid_to: '2026-12-31',
    evidence_source: 'Acme Charge price list',
  },
  {
    fact_id: 'price-de',
    fact_type: 'price',
    country_code: 'DE',
    amount: '49,00',
    currency: 'EUR',
    valid_from: '2026-12-31',
    valid_to: '2026-11-01',
  },
  {
    fact_id: 'ship-de',
    fact_type: 'shipping',
    country_code: 'DE',
    method: 'standard',
    cost: '4.90',
    currency: 'EUR',
    delivery_days_min: 2,
    delivery_days_max: 5,
    valid_from: '2026-11-01',
    valid_to: '2026-12-31',
    evidence_source: 'Kestrel Power carrier rates',
  },
  {
    fact_id: 'tax-de',
    fact_type: 'tax',
    country_code: 'DE',
    rate: '0.19',
    valid_from: '2026-11-01',
    valid_to: '2027-06-30',
    evidence_source: 'tax table',
  },
];

const blockingAt = (code: string, path: string, message = '') => ({
  code,
  message,
  path,
  severity: 'blocking',
});

const IMPORT_REPORT = {
  entry_type: 'manual_fact_set',
  content_hash: 'sha256:1111',
  ok: false,
  blocking: [
    blockingAt(
      'dependency_not_found',
      'data_source_entry_id',
      '引用的数据源尚无已发布版本'
    ),
    blockingAt('fact_value_missing', 'facts[0].currency', '缺少字段 currency'),
    blockingAt(
      'fact_valid_range',
      'facts[1].valid_to',
      'valid_to 必须晚于 valid_from'
    ),
    blockingAt(
      'fact_evidence_source_required',
      'facts[1].evidence_source',
      '必须声明证据来源 evidence_source'
    ),
    blockingAt(
      'fact_amount_parse',
      'facts[1].amount',
      "无法解析 amount: '49,00'"
    ),
    blockingAt(
      'fact_type_unknown',
      'facts[3].fact_type',
      'fact_type 必须是…之一'
    ),
  ],
  warnings: [],
};

function importedDraft(report: unknown = IMPORT_REPORT) {
  known.getLibraryEntry.mockResolvedValue(
    ok(
      entry({
        current_version_id: null,
        draft: draft({ ...CONTENT, facts: IMPORTED_FACTS }, report),
      })
    )
  );
}

/** Each row group of the Draft tab's report, as its text. */
async function reportRows(label: string) {
  const list = await screen.findByRole('list', { name: label });
  return within(list)
    .getAllByRole('listitem')
    .filter((item) => item.parentElement === list)
    .map((item) => item.textContent);
}

const CJK = /[㐀-鿿]/;

describe('An imported draft whose rows have problems (#23)', () => {
  it('marks the rows on the Facts tab and links to the Draft tab', async () => {
    importedDraft();
    renderDetail(FACT_SET, 'facts');

    expect(await screen.findByRole('status')).toHaveTextContent(
      '3 row(s) have problems that block publishing. The Draft tab lists each reason.'
    );
    expect(
      screen.getAllByRole('columnheader').map((cell) => cell.textContent)
    ).toEqual([
      'Fact ID',
      'Problems',
      'Type',
      'Market',
      'Offer',
      'Amount',
      'Valid',
      'Other values',
    ]);
    expect(factRows().map((cells) => cells[1])).toEqual([
      'Row 1: currency',
      'Row 2: valid_to, evidence_source, amount',
      '—No problems',
      'Row 4: fact_type',
    ]);

    fireEvent.click(screen.getByRole('button', { name: 'Open the Draft tab' }));
    expect(mockSetEntry).toHaveBeenCalledWith(FACT_SET, 'draft');
  });

  it('says the draft is blocked when only the fact set as a whole has a problem', async () => {
    importedDraft({
      ...IMPORT_REPORT,
      blocking: IMPORT_REPORT.blocking.slice(0, 1),
    });
    renderDetail(FACT_SET, 'facts');

    expect(await screen.findByRole('status')).toHaveTextContent(
      'This draft has problems that block publishing.'
    );
    expect(factRows().map((cells) => cells[1])).toEqual([
      '—No problems',
      '—No problems',
      '—No problems',
      '—No problems',
    ]);
  });

  it('marks nothing once the draft passes', async () => {
    importedDraft({ ...IMPORT_REPORT, ok: true, blocking: [] });
    renderDetail(FACT_SET, 'facts');

    expect(await screen.findByText('Facts in the draft')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: 'Problems' })).toBeNull();
  });

  it('lists each problem by row, field and reason on the Draft tab', async () => {
    importedDraft();
    renderDetail(FACT_SET, 'draft');

    const results = await screen.findByRole('region', {
      name: 'Validation results',
    });
    expect(within(results).getByRole('status')).toHaveTextContent(
      'Validation failed'
    );
    expect(
      screen.getByText(
        '3 row(s) must be fixed before this fact set can be published. Row 1 is the first fact in the file (in a CSV file, the first line after the header).'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'The referenced version does not exist or has not been published yet.',
        { exact: false }
      )
    ).toHaveTextContent('data_source_entry_id:');
    expect(await reportRows('Problems by row')).toEqual([
      'Row 1price-uscurrency: This value is missing.',
      'Row 2price-devalid_to: The fact must stop being valid later than it starts.' +
        'evidence_source: Say where this fact comes from (evidence_source), such as the price list or contract it was taken from.' +
        'amount: The amount is not a valid decimal number.',
      'Row 4tax-defact_type: The fact type must be price, availability, discount, shipping, delivery or product_claim.',
    ]);
    expect(results.textContent).not.toMatch(CJK);
  });

  it('reads in Chinese', async () => {
    importedDraft();
    renderDetail(FACT_SET, 'draft', 'zh-CN');

    const results = await screen.findByRole('region', { name: '校验结果' });
    expect(within(results).getByRole('status')).toHaveTextContent('校验未通过');
    expect(await reportRows('按行列出的问题')).toEqual([
      '第 1 行price-uscurrency: 缺少这个值。',
      '第 2 行price-devalid_to: 失效时间必须晚于生效时间。' +
        'evidence_source: 请填写这条事实的证据来源（evidence_source），例如它取自哪份价目表或合同。' +
        'amount: 金额不是有效的十进制数。',
      '第 4 行tax-defact_type: 事实类型必须是 price、availability、discount、shipping、delivery 或 product_claim。',
    ]);
  });

  it('marks the rows on the Facts tab in Chinese', async () => {
    importedDraft();
    renderDetail(FACT_SET, 'facts', 'zh-CN');

    expect(await screen.findByRole('status')).toHaveTextContent(
      '有 3 行的问题会阻止发布，草稿页签逐条列出了原因。'
    );
    expect(factRows().map((cells) => cells[1])).toEqual([
      '第 1 行：currency',
      '第 2 行：valid_to, evidence_source, amount',
      '—没有问题',
      '第 4 行：fact_type',
    ]);
    expect(
      screen.getByRole('button', { name: '打开草稿页签' })
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
