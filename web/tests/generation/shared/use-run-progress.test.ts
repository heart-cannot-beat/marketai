/**
 * @jest-environment jsdom
 */
import { renderHook, waitFor } from '@testing-library/react';
import { useRunProgress } from '@/components/generation/shared/useRunProgress';

const mockGateway = {
  getGenerationRun: jest.fn(),
  pollOperation: jest.fn(),
  listGenerationRunItems: jest.fn(),
};

const mockCurrentWorkspace = {
  slug: 'acme',
  workspaceId: 'w1',
  role: 'OWNER',
  membershipId: 'm1',
  displayName: 'Acme',
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: mockCurrentWorkspace }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));

function okResult<T>(data: T) {
  return {
    kind: 'ok' as const,
    status: 200,
    data,
    meta: { etag: null, retryAt: null, location: null },
  };
}

function makeRun(status: string) {
  return {
    id: 'run-1',
    product_id: 'prod-1',
    provider: 'openai',
    model: 'gpt-image-1',
    status,
    total: 10,
    done: 10,
    failed: 0,
    execution_mode: 'real',
    revision: 1,
    commands: [],
  };
}

function makeItemsPage() {
  return { items: [], next_cursor: null };
}

beforeEach(() => {
  jest.useFakeTimers();
  jest.clearAllMocks();
});

afterEach(() => {
  jest.useRealTimers();
});

describe('useRunProgress', () => {
  it('does not poll when runId is null', () => {
    renderHook(() => useRunProgress(null));
    expect(mockGateway.getGenerationRun).not.toHaveBeenCalled();
  });

  it('polls the run, its command operation, and its items page', async () => {
    mockGateway.getGenerationRun.mockResolvedValue(
      okResult(makeRun('succeeded'))
    );
    mockGateway.listGenerationRunItems.mockResolvedValue(
      okResult(makeItemsPage())
    );

    const { result } = renderHook(() => useRunProgress('run-1'));

    await waitFor(() => expect(result.current.run).not.toBeNull());
    expect(result.current.run?.id).toBe('run-1');
    expect(result.current.isRunTerminal).toBe(true);
    expect(result.current.isOperationTerminal).toBe(true);
    expect(mockGateway.getGenerationRun).toHaveBeenCalledWith(
      expect.objectContaining({ workspaceSlug: 'acme', runId: 'run-1' })
    );
  });

  it('exposes forceRefresh and setItemPage', async () => {
    mockGateway.getGenerationRun.mockResolvedValue(
      okResult(makeRun('succeeded'))
    );
    mockGateway.listGenerationRunItems.mockResolvedValue(
      okResult(makeItemsPage())
    );

    const { result } = renderHook(() => useRunProgress('run-1'));
    await waitFor(() => expect(result.current.run).not.toBeNull());

    expect(typeof result.current.forceRefresh).toBe('function');
    expect(typeof result.current.setItemPage).toBe('function');
  });
});
