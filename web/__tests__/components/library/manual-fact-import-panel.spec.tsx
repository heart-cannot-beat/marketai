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
});

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
        'Facts imported into a new fact set draft. Review it there before publishing.'
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
        '事实已导入一份新的事实集草稿，请在那里检查后再发布。'
      )
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '打开事实集' })).toBeEnabled();
    expect(container.textContent).not.toMatch(/[A-Za-z]/);
  });

  it('says a failed import did not finish, in Chinese', async () => {
    importEndsIn('failed');
    const { container } = renderIn('zh-CN');
    chooseFile('选择要导入的事实文件');

    expect(await screen.findByText('事实导入没有完成。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重试' })).toBeEnabled();
    expect(container.textContent).not.toMatch(/[A-Za-z]/);
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

  it('offers no link when the result names no fact set', async () => {
    importEndsIn('succeeded');
    renderIn('en');
    chooseFile('Select fact file to import');

    expect(
      await screen.findByText(
        'Facts imported into a new fact set draft. Review it there before publishing.'
      )
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Open fact set' })).toBeNull();
  });
});
