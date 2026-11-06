import { PollingRunProgressSource } from '@/components/generation/shared/RunProgressSource';
import type { RunProgressSnapshot } from '@/components/generation/shared/RunProgressSource';

const mockGateway = {
  getGenerationRun: jest.fn(),
  pollOperation: jest.fn(),
  listGenerationRunItems: jest.fn(),
};

function okResult<T>(data: T) {
  return {
    kind: 'ok' as const,
    status: 200,
    data,
    meta: { etag: null, retryAt: null, location: null },
  };
}

function makeRun(status: string, hasCommand = true) {
  return {
    id: 'run-1',
    product_id: 'prod-1',
    provider: 'openai',
    model: 'gpt-image-1',
    status,
    total: 10,
    done: 5,
    failed: 2,
    execution_mode: 'real',
    revision: 1,
    commands: hasCommand
      ? [
          {
            id: 'cmd-1',
            command_kind: 'submit_generation_run',
            operation_id: 'op-1',
            created_at: '2026-09-01T00:00:00Z',
          },
        ]
      : [],
  };
}

function makeOperation(state: string) {
  return {
    operation_id: 'op-1',
    command_type: 'submit_generation_run',
    state,
    result: null,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
  };
}

function makeItemsPage(count: number) {
  return {
    items: Array.from({ length: count }, (_, i) => ({
      id: `item-${i}`,
      state: 'succeeded',
      ordinal: i,
    })),
    next_cursor: null,
  };
}

function firstSnapshot(onSnapshot: jest.Mock): RunProgressSnapshot | undefined {
  return onSnapshot.mock.calls[0]?.[0];
}

beforeEach(() => {
  jest.useFakeTimers();
  mockGateway.getGenerationRun.mockReset();
  mockGateway.pollOperation.mockReset();
  mockGateway.listGenerationRunItems.mockReset();
});

afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
});

describe('PollingRunProgressSource', () => {
  it('polls at 1-2s when Operation is running', async () => {
    jest.spyOn(Math, 'random').mockReturnValue(0);
    mockGateway.getGenerationRun
      .mockResolvedValueOnce(okResult(makeRun('running')))
      .mockResolvedValueOnce(okResult(makeRun('running')));
    mockGateway.pollOperation
      .mockResolvedValueOnce(okResult(makeOperation('running')))
      .mockResolvedValueOnce(okResult(makeOperation('running')));
    mockGateway.listGenerationRunItems
      .mockResolvedValueOnce(okResult(makeItemsPage(5)))
      .mockResolvedValueOnce(okResult(makeItemsPage(5)));

    const source = new PollingRunProgressSource(mockGateway as any, 'acme');
    const onSnapshot = jest.fn();

    source.subscribe('run-1', {}, onSnapshot);
    expect(mockGateway.getGenerationRun).toHaveBeenCalled();

    await jest.advanceTimersByTimeAsync(0);
    expect(mockGateway.getGenerationRun).toHaveBeenCalledTimes(1);

    // Next poll is scheduled for the fast (1-2s) cadence — nothing yet at
    // 900ms, a second call once we reach 1000ms (with Math.random mocked
    // to 0, the interval resolves to its floor).
    await jest.advanceTimersByTimeAsync(900);
    expect(mockGateway.getGenerationRun).toHaveBeenCalledTimes(1);

    await jest.advanceTimersByTimeAsync(200);
    expect(mockGateway.getGenerationRun).toHaveBeenCalledTimes(2);

    source.unsubscribe();
  });

  it('stops polling when both Run and Operation are terminal', async () => {
    mockGateway.getGenerationRun.mockResolvedValue(
      okResult(makeRun('succeeded'))
    );
    mockGateway.pollOperation.mockResolvedValue(
      okResult(makeOperation('succeeded'))
    );
    mockGateway.listGenerationRunItems.mockResolvedValue(
      okResult(makeItemsPage(10))
    );

    const source = new PollingRunProgressSource(mockGateway as any, 'acme');
    const onSnapshot = jest.fn();

    source.subscribe('run-1', {}, onSnapshot);

    await jest.advanceTimersByTimeAsync(100);

    const snapshot = firstSnapshot(onSnapshot);
    expect(snapshot?.isRunTerminal).toBe(true);
    expect(snapshot?.isOperationTerminal).toBe(true);

    const callCount = mockGateway.getGenerationRun.mock.calls.length;
    await jest.advanceTimersByTimeAsync(60_000);
    expect(mockGateway.getGenerationRun).toHaveBeenCalledTimes(callCount);

    source.unsubscribe();
  });

  it('uses slow/jittered interval when Operation is admitted', async () => {
    jest.spyOn(Math, 'random').mockReturnValue(0);
    // Run status must not be 'running' (which overrides to the fast
    // cadence); 'queued' lets the Operation's 'admitted' state control
    // the interval.
    mockGateway.getGenerationRun
      .mockResolvedValueOnce(okResult(makeRun('queued')))
      .mockResolvedValueOnce(okResult(makeRun('queued')));
    mockGateway.pollOperation
      .mockResolvedValueOnce(okResult(makeOperation('admitted')))
      .mockResolvedValueOnce(okResult(makeOperation('admitted')));
    mockGateway.listGenerationRunItems
      .mockResolvedValueOnce(okResult(makeItemsPage(5)))
      .mockResolvedValueOnce(okResult(makeItemsPage(5)));

    const source = new PollingRunProgressSource(mockGateway as any, 'acme');
    const onSnapshot = jest.fn();

    source.subscribe('run-1', {}, onSnapshot);
    await jest.advanceTimersByTimeAsync(0);
    expect(mockGateway.getGenerationRun).toHaveBeenCalledTimes(1);

    // With Math.random mocked to 0, the slow interval resolves to its
    // floor of 15 000ms — no second call at 14 999ms.
    await jest.advanceTimersByTimeAsync(14_999);
    expect(mockGateway.getGenerationRun).toHaveBeenCalledTimes(1);

    await jest.advanceTimersByTimeAsync(2);
    expect(mockGateway.getGenerationRun).toHaveBeenCalledTimes(2);

    source.unsubscribe();
  });

  it('continues polling when Operation is uncertain', async () => {
    mockGateway.getGenerationRun.mockResolvedValue(
      okResult(makeRun('running'))
    );
    mockGateway.pollOperation.mockResolvedValue(
      okResult(makeOperation('uncertain'))
    );
    mockGateway.listGenerationRunItems.mockResolvedValue(
      okResult(makeItemsPage(5))
    );

    const source = new PollingRunProgressSource(mockGateway as any, 'acme');
    const onSnapshot = jest.fn();

    source.subscribe('run-1', {}, onSnapshot);
    await jest.advanceTimersByTimeAsync(100);

    const snapshot = firstSnapshot(onSnapshot);
    expect(snapshot?.isOperationTerminal).toBe(false);
    expect(snapshot?.isRunTerminal).toBe(false);

    source.unsubscribe();
  });

  it('tracks transport errors without creating terminal state', async () => {
    mockGateway.getGenerationRun.mockRejectedValueOnce(
      new Error('Network error')
    );

    const source = new PollingRunProgressSource(mockGateway as any, 'acme');
    const onSnapshot = jest.fn();

    source.subscribe('run-1', {}, onSnapshot);
    await jest.advanceTimersByTimeAsync(100);

    const snapshot = firstSnapshot(onSnapshot);
    expect(snapshot?.transportError).toBeInstanceOf(Error);
    expect(snapshot?.isRunTerminal).toBe(false);
    expect(mockGateway.pollOperation).not.toHaveBeenCalled();

    source.unsubscribe();
  });

  it('unsubscribe stops further polling', async () => {
    mockGateway.getGenerationRun.mockResolvedValue(
      okResult(makeRun('running', false))
    );
    mockGateway.listGenerationRunItems.mockResolvedValue(
      okResult(makeItemsPage(5))
    );

    const source = new PollingRunProgressSource(mockGateway as any, 'acme');
    const onSnapshot = jest.fn();

    source.subscribe('run-1', {}, onSnapshot);
    await jest.advanceTimersByTimeAsync(100);
    const callCount = mockGateway.getGenerationRun.mock.calls.length;

    source.unsubscribe();
    await jest.advanceTimersByTimeAsync(60_000);
    expect(mockGateway.getGenerationRun).toHaveBeenCalledTimes(callCount);
  });

  it('forceRefresh polls immediately, bypassing the scheduled delay', async () => {
    mockGateway.getGenerationRun.mockResolvedValue(
      okResult(makeRun('running', false))
    );
    mockGateway.listGenerationRunItems.mockResolvedValue(
      okResult(makeItemsPage(5))
    );

    const source = new PollingRunProgressSource(mockGateway as any, 'acme');
    const onSnapshot = jest.fn();

    source.subscribe('run-1', {}, onSnapshot);
    await jest.advanceTimersByTimeAsync(0);
    expect(mockGateway.getGenerationRun).toHaveBeenCalledTimes(1);

    source.forceRefresh();
    await jest.advanceTimersByTimeAsync(0);
    expect(mockGateway.getGenerationRun).toHaveBeenCalledTimes(2);

    source.unsubscribe();
  });

  it('setItemPage re-fetches items with the new cursor', async () => {
    mockGateway.getGenerationRun.mockResolvedValue(
      okResult(makeRun('running', false))
    );
    mockGateway.listGenerationRunItems.mockResolvedValue(
      okResult(makeItemsPage(5))
    );

    const source = new PollingRunProgressSource(mockGateway as any, 'acme');
    const onSnapshot = jest.fn();

    source.subscribe('run-1', {}, onSnapshot);
    await jest.advanceTimersByTimeAsync(0);

    source.setItemPage('cursor-2');
    await jest.advanceTimersByTimeAsync(0);

    const lastCall =
      mockGateway.listGenerationRunItems.mock.calls[
        mockGateway.listGenerationRunItems.mock.calls.length - 1
      ][0];
    expect(lastCall.cursor).toBe('cursor-2');

    source.unsubscribe();
  });
});
