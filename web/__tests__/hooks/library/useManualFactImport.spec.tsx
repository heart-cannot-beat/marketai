/**
 * @jest-environment jsdom
 */
import { StrictMode, type ReactNode } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import { SWRConfig } from 'swr';
import enLibrary from '../../../locales/en/library.json';
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
const mockInvalidate = jest.fn();

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'w1', role: 'MEMBER' },
  }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/hooks/library/useLibraryCache', () => ({
  useLibraryCache: () => ({ invalidate: mockInvalidate }),
}));

import { useManualFactImport } from '@/hooks/library/useManualFactImport';

/**
 * The import follows two Operations with the real `useOperation` (#14): the
 * upload's verification, then the import that creates a Manual Fact Set
 * draft. The upload runs through the real `useUpload`; only the gateway, the
 * storage transfer and the digest are replaced, and every response has the
 * shape the API sends.
 */
const ENTRY = 'lib_ds_1';
const INTENT = 'upi_1';
const UPLOAD_OP = 'op_upload_1';
const IMPORT_OP = 'op_import_1';
const FACT_SET = 'lib_fs_1';
// `Retry-After: 0` keeps the real poll loop fast.
const META = { etag: null, retryAt: 0, location: null };

const blobClient = {
  upload: jest.fn(),
  validatedDownloadUrl: jest.fn(),
  downloadBlob: jest.fn(),
} as unknown as jest.Mocked<BrowserBlobTransferClient>;

const intentCreated = {
  kind: 'created',
  status: 201,
  data: {
    intent_id: INTENT,
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
};
const finalizeAccepted = {
  kind: 'accepted',
  status: 202,
  data: { operation_id: UPLOAD_OP, state: 'queued' },
  meta: META,
};
const importAccepted = {
  kind: 'accepted',
  status: 202,
  data: {
    operation_id: IMPORT_OP,
    state: 'queued',
    command_type: 'manual_fact_import',
  },
  meta: META,
};

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
const imported = operationIn(IMPORT_OP, 'succeeded', {
  resource_type: 'manual_fact_set_draft',
  resource_id: FACT_SET,
});

const problem = (status: number, code: string) => ({
  kind: 'problem',
  status,
  problem: {
    type: 'about:blank',
    title: 'Refused',
    status,
    code,
    detail: 'English diagnostic that must not reach the page',
    trace_id: 'trace_1',
    retryable: false,
  },
});

/** Each Operation walks through its states, then stays at the last one. */
function pollsThrough(steps: Record<string, unknown[]>) {
  const seen: Record<string, number> = {};
  mockGateway.pollOperation.mockImplementation(async ({ operationId }) => {
    const list = steps[operationId];
    const index = Math.min(seen[operationId] ?? 0, list.length - 1);
    seen[operationId] = index + 1;
    return list[index];
  });
}

function wrapperFor(strict = false) {
  const i18n = getI18n().cloneInstance({
    lng: 'en',
    fallbackLng: false,
    forkResourceStore: true,
  });
  // The Jest setup loads `common` but not `library`.
  i18n.addResourceBundle('en', 'library', enLibrary);
  void i18n.changeLanguage('en');
  return function Wrapper({ children }: { children: ReactNode }) {
    const tree = (
      <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
        <I18nextProvider i18n={i18n}>
          <FileDigestProvider digest={new TestFileDigest()}>
            <BlobClientProvider client={blobClient}>
              {children}
            </BlobClientProvider>
          </FileDigestProvider>
        </I18nextProvider>
      </SWRConfig>
    );
    return strict ? <StrictMode>{tree}</StrictMode> : tree;
  };
}

const facts = () =>
  new File(['{"facts":[{"fact_id":"f1"}]}'], 'facts.json', {
    type: 'application/json',
  });

function renderImport(strict = false) {
  return renderHook(() => useManualFactImport(ENTRY), {
    wrapper: wrapperFor(strict),
  });
}

async function importFile(result: ReturnType<typeof renderImport>['result']) {
  await act(async () => {
    await result.current.importFile(facts());
  });
}

const settle = () =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  localStorage.clear();
  blobClient.upload.mockResolvedValue(undefined);
  mockGateway.createUploadIntent.mockResolvedValue(intentCreated);
  mockGateway.finalizeUpload.mockResolvedValue(finalizeAccepted);
  mockGateway.importLibraryManualFacts.mockResolvedValue(importAccepted);
  mockInvalidate.mockResolvedValue(undefined);
  pollsThrough({
    [UPLOAD_OP]: [
      operationIn(UPLOAD_OP, 'queued'),
      operationIn(UPLOAD_OP, 'succeeded'),
    ],
    [IMPORT_OP]: [operationIn(IMPORT_OP, 'running'), imported],
  });
});

describe('useManualFactImport', () => {
  it('uploads, imports and names the new fact set draft', async () => {
    const { result } = renderImport();
    expect(result.current.view).toEqual({ step: 'idle' });

    await importFile(result);
    await waitFor(() =>
      expect(result.current.view).toEqual({
        step: 'done',
        factSetId: FACT_SET,
      })
    );

    // The file is uploaded for this data source, in this Workspace.
    expect(mockGateway.createUploadIntent).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        body: expect.objectContaining({
          purpose: 'library_manual_fact_import',
          owner_kind: 'data_source',
          owner_id: ENTRY,
        }),
      })
    );
    // Then imported by the upload intent it produced.
    expect(mockGateway.importLibraryManualFacts).toHaveBeenCalledTimes(1);
    const [[sent]] = mockGateway.importLibraryManualFacts.mock.calls;
    expect(sent).toEqual({
      workspaceSlug: 'acme',
      actionInstanceId: expect.stringMatching(/^manual-fact-import:.+:import$/),
      dataSourceEntryId: ENTRY,
      body: {
        upload_intent_id: INTENT,
        purpose: 'library_manual_fact_import',
      },
    });
    // The upload's keys and the import's come from one action per file.
    const [[upload]] = mockGateway.createUploadIntent.mock.calls;
    expect(upload.actionInstanceId).toBe(
      `upload:${sent.actionInstanceId.replace(/:import$/, '')}:create`
    );
    // Both Operations were followed in this Workspace.
    const polled = mockGateway.pollOperation.mock.calls.map(([call]) => call);
    expect(new Set(polled.map((call) => call.operationId))).toEqual(
      new Set([UPLOAD_OP, IMPORT_OP])
    );
    expect(polled.every((call) => call.workspaceSlug === 'acme')).toBe(true);
    // Only the entry list gained an item.
    expect(mockInvalidate).toHaveBeenCalledTimes(1);
    expect(mockInvalidate).toHaveBeenCalledWith({ resources: ['entryList'] });
  });

  it('does not import before the upload passed its checks', async () => {
    pollsThrough({ [UPLOAD_OP]: [operationIn(UPLOAD_OP, 'running')] });
    const { result } = renderImport();
    await importFile(result);
    await settle();

    expect(result.current.view).toEqual({ step: 'verifying' });
    expect(mockGateway.importLibraryManualFacts).not.toHaveBeenCalled();
  });

  it('sends one import, under one action, however often it re-renders', async () => {
    const { result, rerender } = renderImport(true);
    await importFile(result);
    for (let i = 0; i < 5; i += 1) {
      rerender();
      await settle();
    }
    await waitFor(() => expect(result.current.view.step).toBe('done'));
    rerender();
    await settle();

    expect(mockGateway.importLibraryManualFacts).toHaveBeenCalledTimes(1);
    expect(mockInvalidate).toHaveBeenCalledTimes(1);
  });

  it('gives each file its own action', async () => {
    const { result } = renderImport();
    await importFile(result);
    await waitFor(() => expect(result.current.view.step).toBe('done'));
    act(() => result.current.reset());
    expect(result.current.view).toEqual({ step: 'idle' });

    await importFile(result);
    await waitFor(() => expect(result.current.view.step).toBe('done'));

    const [first, second] = mockGateway.importLibraryManualFacts.mock.calls;
    expect(first[0].actionInstanceId).not.toEqual(second[0].actionInstanceId);
  });

  it('stops at an uncertain import and says so', async () => {
    pollsThrough({
      [UPLOAD_OP]: [operationIn(UPLOAD_OP, 'succeeded')],
      [IMPORT_OP]: [operationIn(IMPORT_OP, 'uncertain')],
    });
    const { result } = renderImport();
    await importFile(result);
    await waitFor(() =>
      expect(result.current.view).toEqual({ step: 'uncertain' })
    );

    const polls = mockGateway.pollOperation.mock.calls.length;
    await settle();
    expect(mockGateway.pollOperation).toHaveBeenCalledTimes(polls);
    expect(mockInvalidate).not.toHaveBeenCalled();
  });

  it('stops at an uncertain upload without importing', async () => {
    pollsThrough({ [UPLOAD_OP]: [operationIn(UPLOAD_OP, 'uncertain')] });
    const { result } = renderImport();
    await importFile(result);
    await waitFor(() =>
      expect(result.current.view).toEqual({ step: 'uncertain' })
    );

    const polls = mockGateway.pollOperation.mock.calls.length;
    await settle();
    expect(mockGateway.pollOperation).toHaveBeenCalledTimes(polls);
    expect(mockGateway.importLibraryManualFacts).not.toHaveBeenCalled();
  });

  it.each([
    [
      'an import that failed',
      () =>
        pollsThrough({
          [UPLOAD_OP]: [operationIn(UPLOAD_OP, 'succeeded')],
          [IMPORT_OP]: [operationIn(IMPORT_OP, 'failed')],
        }),
      'The fact import did not finish.',
    ],
    [
      'an import that was cancelled',
      () =>
        pollsThrough({
          [UPLOAD_OP]: [operationIn(UPLOAD_OP, 'succeeded')],
          [IMPORT_OP]: [operationIn(IMPORT_OP, 'cancelled')],
        }),
      'The fact import did not finish.',
    ],
    [
      'a file that failed its upload checks',
      () => pollsThrough({ [UPLOAD_OP]: [operationIn(UPLOAD_OP, 'failed')] }),
      'The file did not pass the upload checks. Use a CSV or JSON file of up to 10 MB.',
    ],
    [
      'a storage transfer that failed',
      () =>
        blobClient.upload.mockRejectedValue(new Error('Upload failed: 500')),
      'The file could not be uploaded. Try again.',
    ],
    [
      'an upload whose outcome is unconfirmed',
      () =>
        mockGateway.finalizeUpload.mockResolvedValue({
          kind: 'reconciliation_required',
          actionInstanceId: 'upload:x:finalize',
          staleSince: 0,
        }),
      'We could not confirm that the import started. Check the library for a new fact set draft, then try again.',
    ],
    [
      'an import whose start is unconfirmed',
      () =>
        mockGateway.importLibraryManualFacts.mockResolvedValue({
          kind: 'reconciliation_required',
          actionInstanceId: 'x:import',
          staleSince: 0,
        }),
      'We could not confirm that the import started. Check the library for a new fact set draft, then try again.',
    ],
    [
      'an upload that was already used',
      () =>
        mockGateway.importLibraryManualFacts.mockResolvedValue(
          problem(409, 'upload_intent_consumed')
        ),
      'This upload was already used. Upload the file again.',
    ],
    [
      'a key reused for another import',
      () =>
        mockGateway.importLibraryManualFacts.mockResolvedValue(
          problem(409, 'idempotency_key_reused')
        ),
      'This request was already used for a different action. Start again.',
    ],
    [
      "another Workspace's data source",
      () =>
        mockGateway.importLibraryManualFacts.mockResolvedValue(
          problem(404, 'library_not_found')
        ),
      'This library item no longer exists.',
    ],
    [
      'an import the role may not start',
      () =>
        mockGateway.importLibraryManualFacts.mockResolvedValue(
          problem(403, 'forbidden')
        ),
      'You do not have permission to do this.',
    ],
    [
      'an Operation this Workspace cannot read',
      () =>
        mockGateway.pollOperation.mockResolvedValue(
          problem(404, 'operation_not_found')
        ),
      'This task no longer exists.',
    ],
    [
      'a request that never got an answer',
      () =>
        mockGateway.importLibraryManualFacts.mockRejectedValue(
          new TypeError('Failed to fetch')
        ),
      'The action could not be completed. Refresh and try again.',
    ],
  ])('words %s', async (_case, arrange, sentence) => {
    arrange();
    const { result } = renderImport();
    await importFile(result);
    await waitFor(() =>
      expect(result.current.view).toEqual({ step: 'error', message: sentence })
    );
    expect(mockInvalidate).not.toHaveBeenCalled();
  });

  it('does not fail an import whose result names no fact set', async () => {
    pollsThrough({
      [UPLOAD_OP]: [operationIn(UPLOAD_OP, 'succeeded')],
      [IMPORT_OP]: [operationIn(IMPORT_OP, 'succeeded')],
    });
    const { result } = renderImport();
    await importFile(result);
    await waitFor(() =>
      expect(result.current.view).toEqual({ step: 'done', factSetId: null })
    );
  });

  it('drops the answer about a file the user moved on from', async () => {
    let answer: (value: unknown) => void = () => undefined;
    mockGateway.importLibraryManualFacts.mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      })
    );
    const { result } = renderImport();
    await importFile(result);
    await waitFor(() =>
      expect(mockGateway.importLibraryManualFacts).toHaveBeenCalled()
    );
    act(() => result.current.reset());

    await act(async () => {
      answer(importAccepted);
    });
    await settle();

    expect(result.current.view).toEqual({ step: 'idle' });
    expect(
      mockGateway.pollOperation.mock.calls.some(
        ([call]) => call.operationId === IMPORT_OP
      )
    ).toBe(false);
  });
});
