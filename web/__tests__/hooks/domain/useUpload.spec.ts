/**
 * @jest-environment jsdom
 */
import { renderHook, act } from '@testing-library/react';
import { useUpload } from '@/hooks/domain/useUpload';

const mockGateway = {
  createUploadIntent: jest.fn(),
  finalizeUpload: jest.fn(),
};
const mockBlobClient = { upload: jest.fn(), validatedDownloadUrl: jest.fn() };
const mockDigest = { computeSha256: jest.fn() };

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme' } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => mockBlobClient,
  useFileDigest: () => mockDigest,
}));

describe('useUpload', () => {
  beforeEach(() => jest.clearAllMocks());

  it('completes three-phase upload and returns operationRecordId', async () => {
    mockDigest.computeSha256.mockResolvedValue('abc123hash=');
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: {
        intent_id: 'ui1',
        state: 'pending',
        purpose: 'reference',
        expires_at: new Date().toISOString(),
        upload_grant: {
          kind: 'signed_put',
          url: 'https://store/obj',
          expires_at: new Date().toISOString(),
        },
      },
      meta: {},
    });
    mockBlobClient.upload.mockResolvedValue(undefined);
    mockGateway.finalizeUpload.mockResolvedValue({
      kind: 'accepted',
      status: 202,
      data: { operation_id: 'op42', state: 'queued' },
      meta: {},
    });

    const file = new File(['data'], 'test.png', { type: 'image/png' });
    const { result } = renderHook(() => useUpload('reference'));

    expect(result.current.state).toBe('idle');

    let uploadResult: any;
    await act(async () => {
      uploadResult = await result.current.startUpload({
        file,
        ownerKind: 'product',
        ownerId: 'p1',
        actionInstanceId: 'upload-1',
      });
    });

    expect(uploadResult.kind).toBe('accepted');
    expect(uploadResult.uploadIntentId).toBe('ui1');
    expect(uploadResult.operationRecordId).toBe('op42');
    expect(result.current.state).toBe('awaiting_verification');
    expect(mockBlobClient.upload).toHaveBeenCalledWith(
      {
        kind: 'signed_put',
        url: 'https://store/obj',
        expires_at: expect.any(String),
      },
      file
    );
  });

  it('passes purpose, size bounds and checksum through to createUploadIntent', async () => {
    mockDigest.computeSha256.mockResolvedValue('the-checksum=');
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: {
        intent_id: 'ui-body',
        state: 'pending',
        purpose: 'reference',
        expires_at: new Date().toISOString(),
        upload_grant: {
          kind: 'signed_put',
          url: 'https://s/o',
          expires_at: new Date().toISOString(),
        },
      },
      meta: {},
    });
    mockBlobClient.upload.mockResolvedValue(undefined);
    mockGateway.finalizeUpload.mockResolvedValue({
      kind: 'accepted',
      status: 202,
      data: { operation_id: 'op-body', state: 'queued' },
      meta: {},
    });

    const file = new File(['0123456789'], 'body.txt', { type: 'text/plain' });
    const { result } = renderHook(() => useUpload('reference'));

    await act(async () => {
      await result.current.startUpload({
        file,
        ownerKind: 'product',
        ownerId: 'p1',
        actionInstanceId: 'body-test',
      });
    });

    expect(mockGateway.createUploadIntent.mock.calls[0][0].body).toEqual({
      purpose: 'reference',
      owner_kind: 'product',
      owner_id: 'p1',
      filename: 'body.txt',
      content_type: 'text/plain',
      content_length_min: 10,
      content_length_max: 10,
      checksum_sha256_b64: 'the-checksum=',
    });
  });

  it('uses derived action IDs for create and finalize', async () => {
    mockDigest.computeSha256.mockResolvedValue('hash=');
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: {
        intent_id: 'ui2',
        state: 'pending',
        purpose: 'ref',
        expires_at: new Date().toISOString(),
        upload_grant: {
          kind: 'signed_put',
          url: 'https://s/o',
          expires_at: new Date().toISOString(),
        },
      },
      meta: {},
    });
    mockBlobClient.upload.mockResolvedValue(undefined);
    mockGateway.finalizeUpload.mockResolvedValue({
      kind: 'accepted',
      status: 202,
      data: { operation_id: 'op2', state: 'queued' },
      meta: {},
    });

    const file = new File(['x'], 'f.txt');
    const { result } = renderHook(() => useUpload('ref'));

    await act(async () => {
      await result.current.startUpload({
        file,
        ownerKind: 'product',
        ownerId: 'p1',
        actionInstanceId: 'my-action',
      });
    });

    expect(
      mockGateway.createUploadIntent.mock.calls[0][0].actionInstanceId
    ).toBe('upload:my-action:create');
    expect(mockGateway.finalizeUpload.mock.calls[0][0].actionInstanceId).toBe(
      'upload:my-action:finalize'
    );
    expect(mockGateway.finalizeUpload.mock.calls[0][0].uploadIntentId).toBe(
      'ui2'
    );
  });

  it('returns problem when createUploadIntent fails, without calling the blob client', async () => {
    mockDigest.computeSha256.mockResolvedValue('hash=');
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'problem',
      status: 413,
      problem: { type: 'too_large', title: 'File too large', status: 413 },
      meta: {},
    });

    const file = new File(['x'], 'big.bin');
    const { result } = renderHook(() => useUpload('ref'));

    let uploadResult: any;
    await act(async () => {
      uploadResult = await result.current.startUpload({
        file,
        ownerKind: 'product',
        ownerId: 'p1',
        actionInstanceId: 'fail-test',
      });
    });

    expect(uploadResult.kind).toBe('problem');
    expect(uploadResult.problem.status).toBe(413);
    expect(result.current.state).toBe('idle');
    expect(mockBlobClient.upload).not.toHaveBeenCalled();
    expect(mockGateway.finalizeUpload).not.toHaveBeenCalled();
  });

  it('returns problem when finalizeUpload fails', async () => {
    mockDigest.computeSha256.mockResolvedValue('hash=');
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: {
        intent_id: 'ui-finalize-fail',
        state: 'pending',
        purpose: 'ref',
        expires_at: new Date().toISOString(),
        upload_grant: {
          kind: 'signed_put',
          url: 'https://s/o',
          expires_at: new Date().toISOString(),
        },
      },
      meta: {},
    });
    mockBlobClient.upload.mockResolvedValue(undefined);
    mockGateway.finalizeUpload.mockResolvedValue({
      kind: 'problem',
      status: 410,
      problem: { type: 'expired', title: 'Intent expired', status: 410 },
      meta: {},
    });

    const file = new File(['x'], 'f.txt');
    const { result } = renderHook(() => useUpload('ref'));

    let uploadResult: any;
    await act(async () => {
      uploadResult = await result.current.startUpload({
        file,
        ownerKind: 'product',
        ownerId: 'p1',
        actionInstanceId: 'finalize-fail',
      });
    });

    expect(uploadResult.kind).toBe('problem');
    expect(uploadResult.problem.status).toBe(410);
    expect(result.current.state).toBe('idle');
  });

  it('returns reconciliation_required from createUploadIntent', async () => {
    mockDigest.computeSha256.mockResolvedValue('hash=');
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'reconciliation_required',
      actionInstanceId: 'upload:recon-test:create',
      staleSince: 1234,
    });

    const file = new File(['x'], 'f.txt');
    const { result } = renderHook(() => useUpload('ref'));

    let uploadResult: any;
    await act(async () => {
      uploadResult = await result.current.startUpload({
        file,
        ownerKind: 'product',
        ownerId: 'p1',
        actionInstanceId: 'recon-test',
      });
    });

    expect(uploadResult.kind).toBe('reconciliation_required');
    expect(uploadResult.staleSince).toBe(1234);
    expect(mockBlobClient.upload).not.toHaveBeenCalled();
  });

  it('returns transfer_error with a safe origin (never the full grant URL) when the blob upload throws', async () => {
    mockDigest.computeSha256.mockResolvedValue('hash=');
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: {
        intent_id: 'ui-transfer-fail',
        state: 'pending',
        purpose: 'ref',
        expires_at: new Date().toISOString(),
        upload_grant: {
          kind: 'signed_put',
          url: 'https://store.example.com/bucket/key?signature=super-secret',
          expires_at: new Date().toISOString(),
        },
      },
      meta: {},
    });
    mockBlobClient.upload.mockRejectedValue(new Error('Upload failed: 500'));

    const file = new File(['x'], 'f.txt');
    const { result } = renderHook(() => useUpload('ref'));

    let uploadResult: any;
    await act(async () => {
      uploadResult = await result.current.startUpload({
        file,
        ownerKind: 'product',
        ownerId: 'p1',
        actionInstanceId: 'transfer-fail',
      });
    });

    expect(uploadResult.kind).toBe('transfer_error');
    expect(uploadResult.error.status).toBe(500);
    expect(uploadResult.error.origin).toBe('https://store.example.com');
    expect(uploadResult.error.message).not.toContain('signature');
    expect(result.current.state).toBe('idle');
    expect(mockGateway.finalizeUpload).not.toHaveBeenCalled();
  });

  it('tracks state through each phase and resets hashProgress at the start of a new upload', async () => {
    const states: string[] = [];
    mockDigest.computeSha256.mockImplementation(
      async (_file: File, onProgress?: (f: number) => void) => {
        states.push('during-hash');
        onProgress?.(0.5);
        onProgress?.(1);
        return 'hash=';
      }
    );
    mockGateway.createUploadIntent.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: {
        intent_id: 'ui-phase',
        state: 'pending',
        purpose: 'ref',
        expires_at: new Date().toISOString(),
        upload_grant: {
          kind: 'signed_put',
          url: 'https://s/o',
          expires_at: new Date().toISOString(),
        },
      },
      meta: {},
    });
    mockBlobClient.upload.mockResolvedValue(undefined);
    mockGateway.finalizeUpload.mockResolvedValue({
      kind: 'accepted',
      status: 202,
      data: { operation_id: 'op-phase', state: 'queued' },
      meta: {},
    });

    const file = new File(['x'], 'f.txt');
    const { result } = renderHook(() => useUpload('ref'));

    await act(async () => {
      await result.current.startUpload({
        file,
        ownerKind: 'product',
        ownerId: 'p1',
        actionInstanceId: 'phase-test',
      });
    });

    expect(result.current.hashProgress).toBe(1);
    expect(result.current.state).toBe('awaiting_verification');
  });

  it('resets state to idle and rethrows when the gateway throws (ambiguous network failure)', async () => {
    mockDigest.computeSha256.mockResolvedValue('hash=');
    mockGateway.createUploadIntent.mockRejectedValue(new Error('network down'));

    const file = new File(['x'], 'f.txt');
    const { result } = renderHook(() => useUpload('ref'));

    await act(async () => {
      await expect(
        result.current.startUpload({
          file,
          ownerKind: 'product',
          ownerId: 'p1',
          actionInstanceId: 'throw-test',
        })
      ).rejects.toThrow('network down');
    });

    expect(result.current.state).toBe('idle');
  });
});
