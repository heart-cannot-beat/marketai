/**
 * @jest-environment jsdom
 */
import { act, renderHook } from '@testing-library/react';

const mockStartUpload = jest.fn();
const mockPollOperation = jest.fn();
const mockCreateReference = jest.fn();
const mockPromoteReference = jest.fn();
const mockGetReferenceObject = jest.fn();
const mockInvalidate = jest.fn();
let mockUploadState = 'idle';

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: {
      slug: 'acme',
      workspaceId: 'w1',
      role: 'OWNER',
      membershipId: 'm1',
      displayName: 'Acme',
    },
  }),
}));

jest.mock('@/hooks/domain/useUpload', () => ({
  useUpload: () => ({
    startUpload: mockStartUpload,
    state: mockUploadState,
    hashProgress: 0,
  }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => ({
    pollOperation: mockPollOperation,
    createLibraryReferenceObject: mockCreateReference,
    promoteLibraryReferenceObject: mockPromoteReference,
    getLibraryReferenceObject: mockGetReferenceObject,
  }),
}));

jest.mock('@/hooks/library/useLibraryCache', () => ({
  useLibraryCache: () => ({ invalidate: mockInvalidate }),
}));

import {
  CONTENT_SAFETY_UNAVAILABLE,
  REFERENCE_GATE_REJECTED,
  useLibraryReferenceUpload,
} from '@/hooks/library/useLibraryReferenceUpload';

const META = { etag: null, retryAt: null, location: null };
const RIGHTS = { scope: 'owned_product_photo', valid_to: '2099-01-01' };
const OPTIONS = {
  contextEntryId: 'entry-1',
  bindToContext: true,
  rights: RIGHTS,
};

function makeFile(name = 'test.png', size = 1024, type = 'image/png'): File {
  return new File([new ArrayBuffer(size)], name, { type });
}

function operation(
  operationId: string,
  state: string,
  resourceId: string | null = null
) {
  return {
    kind: 'ok',
    status: 200,
    data: {
      operation_id: operationId,
      command_type:
        operationId === 'op-reference-1'
          ? 'reference_gate'
          : 'verify_upload_intent',
      state,
      result: resourceId
        ? { resource_type: 'reference_object', resource_id: resourceId }
        : null,
      created_at: '2026-09-05T07:00:00Z',
      updated_at: '2026-09-05T07:00:01Z',
    },
    meta: META,
  };
}

function setupHappyPath() {
  mockStartUpload.mockResolvedValue({
    kind: 'accepted',
    uploadIntentId: 'intent-1',
    operationRecordId: 'op-finalize-1',
  });
  mockCreateReference.mockResolvedValue({
    kind: 'accepted',
    status: 202,
    data: {
      operation_id: 'op-reference-1',
      state: 'queued',
      command_type: 'reference_gate',
    },
    meta: META,
  });
  mockPollOperation.mockImplementation(async ({ operationId }) =>
    operationId === 'op-reference-1'
      ? operation(operationId, 'succeeded', 'ref-obj-1')
      : operation(operationId, 'succeeded')
  );
  mockPromoteReference.mockResolvedValue({
    kind: 'created',
    status: 201,
    data: {
      object_id: 'ref-obj-1',
      entry_id: 'lib-ref-1',
      version_id: 'libver-ref-1',
      version_no: 1,
      display_name: 'test',
      created: true,
      binding: {
        entry_id: 'entry-1',
        draft_revision: 2,
        purpose: 'product_reference',
        viewpoint: 'unspecified',
        already_bound: false,
      },
    },
    meta: META,
  });
}

describe('useLibraryReferenceUpload', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockUploadState = 'idle';
    mockInvalidate.mockResolvedValue(undefined);
  });

  it('starts in idle state', () => {
    const { result } = renderHook(() => useLibraryReferenceUpload());
    expect(result.current.progress.phase).toBe('idle');
  });

  it('uploads, gates, promotes and binds without ever choosing the safety mode', async () => {
    setupHappyPath();
    const { result } = renderHook(() => useLibraryReferenceUpload());
    const file = makeFile();

    await act(async () => {
      await result.current.upload(file, OPTIONS);
    });

    expect(mockStartUpload).toHaveBeenCalledWith(
      expect.objectContaining({
        file,
        ownerKind: 'library_entry',
        ownerId: 'entry-1',
      })
    );
    expect(mockCreateReference).toHaveBeenCalledTimes(1);
    const createBody = mockCreateReference.mock.calls[0][0].body;
    expect(createBody).toEqual({
      upload_intent_id: 'intent-1',
      filename: 'test.png',
      declared_mime: 'image/png',
      rights: RIGHTS,
    });
    // The server decides the content-safety mode (issue #158).
    expect(createBody).not.toHaveProperty('execution_mode');
    expect(createBody).not.toHaveProperty('safety_scenario');

    expect(mockPromoteReference).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        objectId: 'ref-obj-1',
        body: { bind: { entry_id: 'entry-1' } },
      })
    );
    expect(result.current.progress).toMatchObject({
      phase: 'released',
      referenceObjectId: 'ref-obj-1',
      referenceVersionId: 'libver-ref-1',
      referenceEntryId: 'lib-ref-1',
      boundEntryId: 'entry-1',
      failureCode: null,
      bytesTransferred: file.size,
    });
    expect(mockInvalidate).toHaveBeenCalledWith({
      resources: ['entry', 'entryList', 'bindings', 'events'],
    });
  });

  it('promotes without binding for a non-bindable context', async () => {
    setupHappyPath();
    mockPromoteReference.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: {
        object_id: 'ref-obj-1',
        entry_id: 'lib-ref-1',
        version_id: 'libver-ref-1',
        version_no: 1,
        display_name: 'test',
        created: true,
        binding: null,
      },
      meta: META,
    });
    const { result } = renderHook(() => useLibraryReferenceUpload());

    await act(async () => {
      await result.current.upload(makeFile(), {
        ...OPTIONS,
        bindToContext: false,
      });
    });

    expect(mockPromoteReference.mock.calls[0][0].body).toEqual({});
    expect(result.current.progress).toMatchObject({
      phase: 'released',
      boundEntryId: null,
    });
  });

  it.each([
    [
      'problem',
      {
        kind: 'problem',
        problem: {
          code: 'content_type_not_allowed',
          detail: 'MIME type not allowed',
        },
      },
      'content_type_not_allowed',
    ],
    [
      'transfer failure',
      {
        kind: 'transfer_error',
        error: {
          status: 413,
          origin: 'https://blob.test',
          message: 'File transfer to storage failed',
        },
      },
      'unknown',
    ],
    [
      'ambiguous finalize',
      {
        kind: 'reconciliation_required',
        actionInstanceId: 'upload-1',
        staleSince: 1,
      },
      'unknown',
    ],
  ])(
    'fails safely for a shared-upload %s',
    async (_label, uploadResult, code) => {
      mockStartUpload.mockResolvedValue(uploadResult);
      const { result } = renderHook(() => useLibraryReferenceUpload());

      await act(async () => {
        await result.current.upload(makeFile(), OPTIONS);
      });

      expect(result.current.progress.phase).toBe('failed');
      expect(result.current.progress.problem?.code).toBe(code);
      expect(mockPromoteReference).not.toHaveBeenCalled();
    }
  );

  it('surfaces the content-safety-unavailable code when the gate cannot run', async () => {
    setupHappyPath();
    mockPollOperation.mockImplementation(async ({ operationId }) =>
      operationId === 'op-reference-1'
        ? operation(operationId, 'failed', 'ref-obj-1')
        : operation(operationId, 'succeeded')
    );
    mockGetReferenceObject.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: {
        object_id: 'ref-obj-1',
        state: 'quarantined',
        failure_code: CONTENT_SAFETY_UNAVAILABLE,
        failure_detail: '内容安全服务未配置',
        content_safety_unavailable: true,
        checks: [],
      },
      meta: META,
    });
    const { result } = renderHook(() => useLibraryReferenceUpload());

    await act(async () => {
      await result.current.upload(makeFile(), OPTIONS);
    });

    expect(mockGetReferenceObject).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      objectId: 'ref-obj-1',
    });
    expect(result.current.progress).toMatchObject({
      phase: 'failed',
      failureCode: CONTENT_SAFETY_UNAVAILABLE,
      problem: { code: CONTENT_SAFETY_UNAVAILABLE },
      referenceObjectId: 'ref-obj-1',
    });
    expect(mockPromoteReference).not.toHaveBeenCalled();
  });

  it('reports a gate rejection as rejected by its code', async () => {
    setupHappyPath();
    mockPollOperation.mockImplementation(async ({ operationId }) =>
      operationId === 'op-reference-1'
        ? operation(operationId, 'failed', 'ref-obj-1')
        : operation(operationId, 'succeeded')
    );
    mockGetReferenceObject.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: {
        object_id: 'ref-obj-1',
        state: 'rejected',
        failure_code: REFERENCE_GATE_REJECTED,
        failure_detail: '上传门禁未通过: malware_scan',
        content_safety_unavailable: false,
        checks: [],
      },
      meta: META,
    });
    const { result } = renderHook(() => useLibraryReferenceUpload());

    await act(async () => {
      await result.current.upload(makeFile(), OPTIONS);
    });

    expect(result.current.progress).toMatchObject({
      phase: 'rejected',
      failureCode: REFERENCE_GATE_REJECTED,
      problem: { code: REFERENCE_GATE_REJECTED },
    });
  });

  it('fails when the promotion is refused', async () => {
    setupHappyPath();
    mockPromoteReference.mockResolvedValue({
      kind: 'problem',
      status: 422,
      problem: {
        code: 'library_reference_rights_required',
        detail: '缺少权利声明，不能发布参考素材',
      },
      meta: META,
    });
    const { result } = renderHook(() => useLibraryReferenceUpload());

    await act(async () => {
      await result.current.upload(makeFile(), OPTIONS);
    });

    expect(result.current.progress.phase).toBe('failed');
    expect(result.current.progress.problem?.code).toBe(
      'library_reference_rights_required'
    );
  });

  it('resets to idle and never persists capabilities or object keys', async () => {
    setupHappyPath();
    const { result } = renderHook(() => useLibraryReferenceUpload());

    await act(async () => {
      await result.current.upload(makeFile(), OPTIONS);
    });

    expect(JSON.stringify(result.current.progress)).not.toMatch(
      /blob\.test|upload_url|signed|object_key/
    );
    act(() => result.current.reset());
    expect(result.current.progress).toEqual({
      phase: 'idle',
      fileName: '',
      bytesTransferred: 0,
      totalBytes: 0,
      referenceObjectId: null,
      referenceVersionId: null,
      referenceEntryId: null,
      boundEntryId: null,
      failureCode: null,
      problem: null,
    });
  });
});
