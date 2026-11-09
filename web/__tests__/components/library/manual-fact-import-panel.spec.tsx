/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import { SWRConfig } from 'swr';
import enLibrary from '../../../locales/en/library.json';
import common from '../../../locales/zh-CN/common.json';
import library from '../../../locales/zh-CN/library.json';
import { TestFileDigest } from '@/lib/domain-client/file-digest';
import {
  BlobClientProvider,
  FileDigestProvider,
} from '@/lib/domain-client/upload-deps';
import type { BrowserBlobTransferClient } from '@/lib/domain-client/blob-transfer';

const mockGateway = {
  createUploadIntent: jest.fn(),
  finalizeUpload: jest.fn(),
  pollOperation: jest.fn(),
  importLibraryManualFacts: jest.fn(),
  listLibraryEntries: jest.fn(),
};
const mockSetEntry = jest.fn();

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'w1', role: 'MEMBER' },
  }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/hooks/library/useLibraryRoute', () => ({
  useLibraryRoute: () => ({ setEntry: mockSetEntry }),
}));

import { ManualFactImportPanel } from '@/components/library/data-sources/ManualFactImportPanel';

/**
 * The data source's import panel (#14). Only the gateway, the storage
 * transfer, the digest and routing are replaced: `useManualFactImport`,
 * `useOperation`, `useUpload` and the copy are the real ones.
 */
const ENTRY = 'lib_ds_1';
const UPLOAD_OP = 'op_upload_1';
const IMPORT_OP = 'op_import_1';
const FACT_SET = 'lib_fs_1';
const META = { etag: null, retryAt: 0, location: null };

/** The Workspace's Offers: only a published one can be the default (#54). */
const offerSummary = (
  id: string,
  displayName: string,
  currentVersionId: string | null
) => ({
  id,
  entry_type: 'offer',
  display_name: displayName,
  lifecycle: 'active',
  current_version_id: currentVersionId,
  current_version_no: currentVersionId ? 1 : null,
  current_version_state: currentVersionId ? 'effective' : null,
  draft_revision: 1,
  draft_state: 'editing',
  updated_at: '2026-11-07T08:00:00+00:00',
});
const OFFERS = [
  offerSummary('lib_offer_65w', 'Acme Charge 65W single', 'libver_65w_1'),
  offerSummary('lib_offer_100w', 'Acme Charge 100W bundle', 'libver_100w_2'),
  offerSummary('lib_offer_draft', 'Acme Charge 30W draft', null),
];

const blobClient = {
  upload: jest.fn(),
  validatedDownloadUrl: jest.fn(),
  downloadBlob: jest.fn(),
} as unknown as jest.Mocked<BrowserBlobTransferClient>;

const operationIn = (
  operationId: string,
  state: string,
  result: { resource_type: string; resource_id: string } | null = null
) => ({
  kind: 'ok',
  status: 200,
  data: {
    operation_id: operationId,
    command_type:
      operationId === IMPORT_OP ? 'manual_fact_import' : 'verify_upload_intent',
    state,
    result,
    created_at: '2026-11-07T08:00:00+00:00',
    updated_at: '2026-11-07T08:00:01+00:00',
  },
  meta: { ...META, etag: `"${operationId}:${state}"` },
});

function importEndsIn(
  state: string,
  result: { resource_type: string; resource_id: string } | null = null
) {
  mockGateway.pollOperation.mockImplementation(async ({ operationId }) =>
    operationId === IMPORT_OP
      ? operationIn(IMPORT_OP, state, result)
      : operationIn(UPLOAD_OP, 'succeeded')
  );
}

function renderIn(language: 'en' | 'zh-CN') {
  const i18n = getI18n().cloneInstance({
    lng: language,
    fallbackLng: false,
    forkResourceStore: true,
  });
  // The Jest setup loads `common` but not `library`.
  i18n.addResourceBundle('en', 'library', enLibrary);
  if (language === 'zh-CN') {
    i18n.addResourceBundle('zh-CN', 'common', common);
    i18n.addResourceBundle('zh-CN', 'library', library);
  }
  void i18n.changeLanguage(language);
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <I18nextProvider i18n={i18n}>
        <FileDigestProvider digest={new TestFileDigest()}>
          <BlobClientProvider client={blobClient}>
            <ManualFactImportPanel dataSourceEntryId={ENTRY} />
          </BlobClientProvider>
        </FileDigestProvider>
      </I18nextProvider>
    </SWRConfig>
  );
}

function chooseFile(label: string) {
  const file = new File(['{"facts":[{"fact_id":"f1"}]}'], 'facts.json', {
    type: 'application/json',
  });
  fireEvent.change(screen.getByLabelText(label), { target: { files: [file] } });
}

/** No raw key, code or English diagnostic reaches the page. */
function expectOnlySentences(text: string) {
  expect(text).not.toMatch(
    /data-source-ops|error-|manual_fact|upload_intent|Import operation|timed out|requires reconciliation|File transfer|Upload failed|signature/
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  localStorage.clear();
  blobClient.upload.mockResolvedValue(undefined);
  mockGateway.createUploadIntent.mockResolvedValue({
    kind: 'created',
    status: 201,
    data: {
      intent_id: 'upi_1',
      state: 'pending',
      purpose: 'library_manual_fact_import',
      expires_at: '2026-11-07T08:05:00+00:00',
      upload_grant: {
        kind: 'signed_put',
        url: 'https://storage.example/upload/upi_1?signature=secret',
        expires_at: '2026-11-07T08:05:00+00:00',
      },
    },
    meta: META,
  });
  mockGateway.finalizeUpload.mockResolvedValue({
    kind: 'accepted',
    status: 202,
    data: { operation_id: UPLOAD_OP, state: 'queued' },
    meta: META,
  });
  mockGateway.importLibraryManualFacts.mockResolvedValue({
    kind: 'accepted',
    status: 202,
    data: {
      operation_id: IMPORT_OP,
      state: 'queued',
      command_type: 'manual_fact_import',
    },
    meta: META,
  });
  importEndsIn('succeeded', {
    resource_type: 'manual_fact_set_draft',
    resource_id: FACT_SET,
  });
  mockGateway.listLibraryEntries.mockResolvedValue({
    kind: 'ok',
    status: 200,
    data: { items: OFFERS, next_cursor: null },
    meta: META,
  });
});

/** The body the panel sent with the import. */
const importBody = () =>
  mockGateway.importLibraryManualFacts.mock.calls[0][0].body as Record<
    string,
    unknown
  >;

describe('ManualFactImportPanel', () => {
  it('says each import makes a new draft, and takes CSV or JSON', () => {
    renderIn('en');
    expect(
      screen.getByText(
        'Import facts from a CSV or JSON file. Each import creates a new fact set draft.'
      )
    ).toBeInTheDocument();
    const input = screen.getByLabelText(
      'Select fact file to import'
    ) as HTMLInputElement;
    expect(input.type).toBe('file');
    expect(input.accept).toBe('.csv,.json');
  });

  it('imports a file and opens the new draft, with no count', async () => {
    const { container } = renderIn('en');
    chooseFile('Select fact file to import');

    expect(
      await screen.findByText(
        'Facts imported into a new fact set draft. Its Draft tab lists any rows to fix before publishing.'
      )
    ).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/\d/);
    expectOnlySentences(container.textContent ?? '');

    fireEvent.click(screen.getByRole('button', { name: 'Open fact set' }));
    expect(mockSetEntry).toHaveBeenCalledWith(FACT_SET, 'draft');
  });

  it('reads in Chinese', async () => {
    const { container } = renderIn('zh-CN');
    expect(
      screen.getByText(
        '从 CSV 或 JSON 文件导入事实。每次导入都会新建一份事实集草稿。'
      )
    ).toBeInTheDocument();
    chooseFile('选择要导入的事实文件');

    expect(
      await screen.findByText(
        '事实已导入一份新的事实集草稿，草稿页签会列出发布前需要修改的行。'
      )
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '打开事实集' })).toBeEnabled();
    expect(container.textContent).not.toMatch(/[A-Za-z]/);
  });

  it('says a failed import could not read the file, in Chinese', async () => {
    importEndsIn('failed');
    const { container } = renderIn('zh-CN');
    chooseFile('选择要导入的事实文件');

    expect(
      await screen.findByText(
        '这个文件无法导入：请使用 UTF-8 编码、带 facts 列表的 JSON 文件，或带表头行的 CSV 文件，包含 1 到 10,000 条事实，大小不超过 10 MB。'
      )
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重试' })).toBeEnabled();
    // Only the names of the file's formats and its `facts` key stay Latin.
    expect(
      (container.textContent ?? '').replace(/UTF-8|JSON|CSV|MB|facts/g, '')
    ).not.toMatch(/[A-Za-z]/);
  });

  it('keeps an uncertain import apart from a failure', async () => {
    importEndsIn('uncertain');
    const { container } = renderIn('en');
    chooseFile('Select fact file to import');

    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent(
      'We could not confirm whether the import finished. Check the library for a new fact set draft before importing again.'
    );
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    expectOnlySentences(container.textContent ?? '');

    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(
      screen.getByRole('button', { name: 'Select file' })
    ).toBeInTheDocument();
  });

  it.each([
    [
      'storage transfer',
      () =>
        blobClient.upload.mockRejectedValue(new Error('Upload failed: 500')),
      'The file could not be uploaded. Try again.',
    ],
    [
      'unconfirmed import',
      () =>
        mockGateway.importLibraryManualFacts.mockResolvedValue({
          kind: 'reconciliation_required',
          actionInstanceId: 'x:import',
          staleSince: 0,
        }),
      'We could not confirm that the import started. Check the library for a new fact set draft, then try again.',
    ],
    [
      'rejected upload',
      () =>
        mockGateway.pollOperation.mockResolvedValue(
          operationIn(UPLOAD_OP, 'failed')
        ),
      'The file did not pass the upload checks. Use a CSV or JSON file of up to 10 MB.',
    ],
    [
      'refused import',
      () =>
        mockGateway.importLibraryManualFacts.mockResolvedValue({
          kind: 'problem',
          status: 409,
          problem: {
            type: 'about:blank',
            title: 'Conflict',
            status: 409,
            code: 'upload_intent_consumed',
            detail: 'upload intent already imported',
            trace_id: 'trace_1',
            retryable: false,
          },
        }),
      'This upload was already used. Upload the file again.',
    ],
  ])('words a %s and starts over', async (_case, arrange, sentence) => {
    arrange();
    const { container } = renderIn('en');
    chooseFile('Select fact file to import');

    expect(await screen.findByRole('alert')).toHaveTextContent(sentence);
    expectOnlySentences(container.textContent ?? '');

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(
      screen.getByText(
        'Import facts from a CSV or JSON file. Each import creates a new fact set draft.'
      )
    ).toBeInTheDocument();
  });

  it('offers the published Offers as the default and sends the chosen one', async () => {
    renderIn('en');
    const picker = screen.getByLabelText('Default offer') as HTMLSelectElement;
    expect(
      await screen.findByRole('option', { name: 'Acme Charge 65W single' })
    ).toBeInTheDocument();
    expect(
      Array.from(picker.options).map((option) => option.textContent)
    ).toEqual([
      'None: every row names its offer',
      'Acme Charge 65W single',
      'Acme Charge 100W bundle',
    ]);
    expect(mockGateway.listLibraryEntries).toHaveBeenCalledWith(
      expect.objectContaining({ entryType: 'offer' })
    );
    expect(picker).toHaveAccessibleDescription(
      'Rows that name no offer (offer_entry_id) get this one. A row that names its own offer keeps it.'
    );

    fireEvent.change(picker, { target: { value: 'lib_offer_100w' } });
    chooseFile('Select fact file to import');

    expect(
      await screen.findByText(
        'Facts imported into a new fact set draft. Its Draft tab lists any rows to fix before publishing.'
      )
    ).toBeInTheDocument();
    expect(importBody()).toEqual({
      upload_intent_id: 'upi_1',
      purpose: 'library_manual_fact_import',
      default_offer_entry_id: 'lib_offer_100w',
    });
  });

  it('sends no default when none is chosen', async () => {
    renderIn('en');
    await screen.findByRole('option', { name: 'Acme Charge 65W single' });
    chooseFile('Select fact file to import');

    await screen.findByRole('button', { name: 'Open fact set' });
    expect(importBody()).toEqual({
      upload_intent_id: 'upi_1',
      purpose: 'library_manual_fact_import',
    });
  });

  it('still imports when the Offers cannot be listed, and says each row names its own', async () => {
    mockGateway.listLibraryEntries.mockResolvedValue({
      kind: 'problem',
      status: 503,
      problem: {
        type: 'about:blank',
        title: 'Unavailable',
        status: 503,
        code: 'service_unavailable',
        detail: 'down',
        trace_id: 'trace_1',
        retryable: true,
      },
    });
    renderIn('en');

    expect(
      await screen.findByText(
        'Offers could not be loaded. Each row must name its offer.'
      )
    ).toBeInTheDocument();
    chooseFile('Select fact file to import');
    await screen.findByRole('button', { name: 'Open fact set' });
    expect(importBody()).not.toHaveProperty('default_offer_entry_id');
  });

  it('words the default Offer in Chinese', async () => {
    renderIn('zh-CN');
    const picker = screen.getByLabelText('默认销售方案') as HTMLSelectElement;
    await screen.findByRole('option', { name: 'Acme Charge 65W single' });
    expect(picker.options[0].textContent).toBe('不选：每行自己写明销售方案');
    expect(picker).toHaveAccessibleDescription(
      '没有写明销售方案（offer_entry_id）的行使用这个销售方案；已经写明的行保持不变。'
    );
  });

  it('offers no link when the result names no fact set', async () => {
    importEndsIn('succeeded');
    renderIn('en');
    chooseFile('Select fact file to import');

    expect(
      await screen.findByText(
        'Facts imported into a new fact set draft. Its Draft tab lists any rows to fix before publishing.'
      )
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Open fact set' })).toBeNull();
  });
});
