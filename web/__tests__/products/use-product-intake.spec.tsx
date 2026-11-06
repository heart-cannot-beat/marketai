/**
 * @jest-environment jsdom
 */
import { act, renderHook } from '@testing-library/react';
import { useProductIntake } from '@/hooks/products/useProductIntake';

const mockGateway = {
  createProductIntake: jest.fn(),
  getProductIntake: jest.fn(),
  confirmProductIntake: jest.fn(),
  pollOperation: jest.fn(),
};
const mockStartUpload = jest.fn();
const telemetry = jest.fn();

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'MEMBER' } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/hooks/domain/useUpload', () => ({
  useUpload: (purpose: string) => {
    expect(purpose).toBe('product_intake_image');
    return { startUpload: mockStartUpload, state: 'idle', hashProgress: 0 };
  },
}));

const meta = { etag: null, retryAt: null, location: null };

function detail(status: string, extra: Record<string, unknown> = {}) {
  return {
    kind: 'ok',
    status: 200,
    data: {
      intake_id: 'pi_1',
      status,
      execution_mode: 'mock',
      input: { url: null, text_chars: 10, upload_count: 0 },
      replayed: false,
      ...extra,
    },
    meta,
  };
}

async function flush() {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

async function advance(ms: number) {
  await act(async () => {
    jest.advanceTimersByTime(ms);
    await flush();
  });
}

beforeEach(() => {
  jest.useFakeTimers();
  jest.clearAllMocks();
  window.sessionStorage.clear();
  (window as any).__PRODUCT_TELEMETRY__ = telemetry;
  mockGateway.createProductIntake.mockResolvedValue({
    kind: 'accepted',
    status: 202,
    data: {
      intake_id: 'pi_1',
      operation_id: 'op_1',
      status: 'queued',
      replayed: false,
    },
    meta,
  });
});

afterEach(() => {
  jest.useRealTimers();
  delete (window as any).__PRODUCT_TELEMETRY__;
});

describe('useProductIntake', () => {
  it('starts from pasted text and polls with backoff until the card is ready', async () => {
    mockGateway.getProductIntake
      .mockResolvedValueOnce(detail('queued'))
      .mockResolvedValueOnce(detail('extracting', { current_step: 'extract' }))
      .mockResolvedValueOnce(detail('preparing_images'))
      .mockResolvedValue(detail('ready', { card: { name: '充电桩' } }));

    const { result } = renderHook(() => useProductIntake());
    await act(async () => {
      await result.current.submit({ raw: '一款家用充电桩', files: [] });
      await flush();
    });

    expect(mockGateway.createProductIntake).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        body: { text: '一款家用充电桩' },
      })
    );
    expect(result.current.phase).toBe('working');
    expect(result.current.detail?.status).toBe('queued');
    expect(window.sessionStorage.getItem('products:acme:intake')).toBe('pi_1');

    await advance(999);
    expect(mockGateway.getProductIntake).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(result.current.detail?.status).toBe('extracting');
    await advance(1500);
    expect(result.current.detail?.status).toBe('preparing_images');
    await advance(2250);
    expect(result.current.phase).toBe('ready');
    expect(mockGateway.getProductIntake).toHaveBeenCalledTimes(4);

    // Polling stops once ready.
    await advance(30_000);
    expect(mockGateway.getProductIntake).toHaveBeenCalledTimes(4);
    expect(telemetry).toHaveBeenCalledWith({
      event: 'product_intake_started',
      inputKinds: ['text'],
      replayed: false,
    });
  });

  it('uploads images first and only sends finalized uploads', async () => {
    mockStartUpload
      .mockResolvedValueOnce({
        kind: 'accepted',
        uploadIntentId: 'ui_1',
        operationRecordId: 'op_u1',
      })
      .mockResolvedValueOnce({
        kind: 'accepted',
        uploadIntentId: 'ui_2',
        operationRecordId: 'op_u2',
      });
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: { state: 'succeeded' },
      meta,
    });
    mockGateway.getProductIntake.mockResolvedValue(detail('extracting'));

    const files = [
      new File(['a'], 'a.png', { type: 'image/png' }),
      new File(['b'], 'b.png', { type: 'image/png' }),
    ];
    const { result } = renderHook(() => useProductIntake());
    await act(async () => {
      await result.current.submit({
        raw: 'https://shop.example/p/wallbox',
        files,
      });
    });

    expect(mockStartUpload).toHaveBeenCalledTimes(2);
    expect(mockStartUpload.mock.calls[0][0]).toMatchObject({
      file: files[0],
      ownerKind: 'product_intake',
    });
    expect(mockGateway.createProductIntake).toHaveBeenCalledWith(
      expect.objectContaining({
        body: {
          url: 'https://shop.example/p/wallbox',
          upload_intent_ids: ['ui_1', 'ui_2'],
        },
      })
    );
    expect(telemetry).toHaveBeenCalledWith(
      expect.objectContaining({
        event: 'product_intake_started',
        inputKinds: ['images', 'url'],
      })
    );
  });

  it('stops before creating the intake when an upload is rejected', async () => {
    mockStartUpload.mockResolvedValueOnce({
      kind: 'accepted',
      uploadIntentId: 'ui_1',
      operationRecordId: 'op_u1',
    });
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: { state: 'failed' },
      meta,
    });

    const { result } = renderHook(() => useProductIntake());
    await act(async () => {
      await result.current.submit({
        raw: '',
        files: [new File(['a'], 'a.png', { type: 'image/png' })],
      });
    });

    expect(mockGateway.createProductIntake).not.toHaveBeenCalled();
    expect(result.current.phase).toBe('input');
    expect(result.current.problem).toEqual({
      stage: 'upload',
      code: 'upload_rejected',
      problem: null,
    });
  });

  it('reports a failed intake once, with its code and suggested action', async () => {
    mockGateway.getProductIntake.mockResolvedValue(
      detail('fetch_failed', {
        failure: {
          code: 'access_denied',
          step: 'fetch',
          message: '该网站拒绝了自动读取',
          suggested_action: 'paste_text_or_screenshot',
          retryable: false,
        },
      })
    );
    const { result } = renderHook(() => useProductIntake());
    await act(async () => {
      await result.current.submit({ raw: 'https://shop.example/p', files: [] });
      await flush();
    });

    expect(result.current.phase).toBe('failed');
    expect(result.current.lastInput?.raw).toBe('https://shop.example/p');
    expect(window.sessionStorage.getItem('products:acme:intake')).toBeNull();
    const failures = telemetry.mock.calls.filter(
      ([event]) => event.event === 'product_intake_failed'
    );
    expect(failures).toEqual([
      [
        {
          event: 'product_intake_failed',
          stage: 'intake',
          failureCode: 'access_denied',
          suggestedAction: 'paste_text_or_screenshot',
          executionMode: 'mock',
        },
      ],
    ]);
  });

  it('resumes the remembered intake after a refresh', async () => {
    window.sessionStorage.setItem('products:acme:intake', 'pi_1');
    mockGateway.getProductIntake.mockResolvedValue(
      detail('ready', { card: { name: '充电桩' } })
    );
    const { result } = renderHook(() => useProductIntake());
    await act(flush);
    expect(mockGateway.getProductIntake).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      intakeId: 'pi_1',
    });
    expect(result.current.phase).toBe('ready');
  });

  it('confirms the card and reports it', async () => {
    mockGateway.getProductIntake.mockResolvedValue(
      detail('ready', { card: { name: '充电桩' } })
    );
    mockGateway.confirmProductIntake.mockResolvedValue(
      detail('confirmed', { product_id: 'prod_9' })
    );
    const { result } = renderHook(() => useProductIntake());
    await act(async () => {
      await result.current.submit({ raw: '充电桩', files: [] });
      await flush();
    });

    let confirmed: unknown;
    await act(async () => {
      confirmed = await result.current.confirm({ card: { name: '充电桩' } });
    });
    expect(mockGateway.confirmProductIntake).toHaveBeenCalledWith(
      expect.objectContaining({
        intakeId: 'pi_1',
        body: { card: { name: '充电桩' } },
      })
    );
    expect(confirmed).toMatchObject({ product_id: 'prod_9' });
    expect(result.current.phase).toBe('confirmed');
    expect(telemetry).toHaveBeenCalledWith(
      expect.objectContaining({ event: 'product_intake_confirmed' })
    );
  });

  it('keeps the card editable when the confirm is refused', async () => {
    mockGateway.getProductIntake.mockResolvedValue(
      detail('ready', { card: { name: '充电桩' } })
    );
    mockGateway.confirmProductIntake.mockResolvedValue({
      kind: 'problem',
      status: 422,
      problem: {
        code: 'variant_selection_required',
        detail: '请选择型号',
      },
      meta,
    });
    const { result } = renderHook(() => useProductIntake());
    await act(async () => {
      await result.current.submit({ raw: '充电桩', files: [] });
      await flush();
    });
    await act(async () => {
      await result.current.confirm({});
    });
    expect(result.current.phase).toBe('ready');
    expect(result.current.problem).toEqual({
      stage: 'confirm',
      code: 'variant_selection_required',
      problem: { code: 'variant_selection_required', detail: '请选择型号' },
    });
  });
});
