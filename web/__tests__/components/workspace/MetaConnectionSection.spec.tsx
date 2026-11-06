/** @jest-environment jsdom */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockExecute = jest.fn();
const mockRefresh = jest.fn();

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'test-ws' } }),
}));

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: jest.fn(),
}));

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: jest.fn(() => ({
    execute: mockExecute,
    isPending: false,
    result: undefined,
    problem: undefined,
    reconciliation: undefined,
  })),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => ({}),
}));

import { MetaConnectionSection } from '@/components/workspace/MetaConnectionSection';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';

const mockUseDomainQuery = jest.mocked(useDomainQuery);

const emptyList = () =>
  ({
    data: { items: [], next_cursor: null },
    problem: undefined,
    isLoading: false,
    isStale: false,
    lastSuccessfulAt: null,
    refresh: mockRefresh,
  }) as any;

const problemDetails = (code: string, traceId: string) => ({
  type: `urn:marketai:error:${code}`,
  title: 'Request failed',
  status: 503,
  detail: '',
  instance: '',
  code,
  trace_id: traceId,
  retryable: true,
  field_errors: [],
});

/**
 * Browsers expose `crypto.randomUUID` only in secure contexts (HTTPS or
 * localhost). The golden-path browser reaches web at http://web:4002, which is
 * not one. Swaps in such a Crypto and returns a function that restores the
 * original.
 */
function simulateInsecureContextCrypto(): () => void {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
  const secureCrypto = globalThis.crypto;
  Object.defineProperty(globalThis, 'crypto', {
    configurable: true,
    value: {
      getRandomValues: secureCrypto.getRandomValues.bind(secureCrypto),
    },
  });
  return () => {
    if (original) {
      Object.defineProperty(globalThis, 'crypto', original);
    } else {
      delete (globalThis as { crypto?: Crypto }).crypto;
    }
  };
}

describe('MetaConnectionSection', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('shows create button when no connection exists', () => {
    mockUseDomainQuery.mockReturnValue({
      data: { items: [], next_cursor: null },
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: mockRefresh,
    } as any);

    render(<MetaConnectionSection />);
    expect(screen.getByTestId('meta-connection-section')).toBeInTheDocument();
    expect(screen.getByTestId('create-meta-connection')).toBeInTheDocument();
  });

  test('shows active status when connection exists', () => {
    mockUseDomainQuery.mockReturnValue({
      data: {
        items: [
          {
            id: 'mcon_123',
            workspace_id: 'ws_1',
            meta_mode: 'customer_sandbox',
            ad_account_id: 'act_sandbox_abc123',
            state: 'active',
            created_at: '2026-01-01T00:00:00',
            updated_at: '2026-01-01T00:00:00',
            retired_at: null,
          },
        ],
        next_cursor: null,
      },
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: Date.now(),
      refresh: mockRefresh,
    } as any);

    render(<MetaConnectionSection />);
    expect(
      screen.getByTestId('meta-connection-status-active')
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId('create-meta-connection')
    ).not.toBeInTheDocument();
  });

  test('create flow shows mode select and confirm', () => {
    mockUseDomainQuery.mockReturnValue({
      data: { items: [], next_cursor: null },
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: mockRefresh,
    } as any);

    render(<MetaConnectionSection />);
    fireEvent.click(screen.getByTestId('create-meta-connection'));
    expect(
      screen.getByTestId('meta-connection-mode-select')
    ).toBeInTheDocument();
    expect(screen.getByTestId('confirm-meta-connection')).toBeInTheDocument();
  });

  test('confirm calls create command and refreshes', async () => {
    mockUseDomainQuery.mockReturnValue({
      data: { items: [], next_cursor: null },
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: mockRefresh,
    } as any);

    mockExecute.mockResolvedValue({
      kind: 'success',
      data: {
        id: 'mcon_new',
        state: 'active',
        meta_mode: 'customer_sandbox',
        ad_account_id: 'act_sandbox_abc',
      },
      meta: { etag: null, retryAt: null, location: null },
    });

    render(<MetaConnectionSection />);
    fireEvent.click(screen.getByTestId('create-meta-connection'));
    fireEvent.click(screen.getByTestId('confirm-meta-connection'));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          body: { meta_mode: 'customer_sandbox' },
        })
      );
    });
    await waitFor(() => {
      expect(mockRefresh).toHaveBeenCalled();
    });
  });

  test('creates the connection outside a secure context', async () => {
    const restoreCrypto = simulateInsecureContextCrypto();
    try {
      mockUseDomainQuery.mockReturnValue(emptyList());
      mockExecute.mockResolvedValue({
        kind: 'success',
        data: { id: 'mcon_new', state: 'active' },
        meta: { etag: null, retryAt: null, location: null },
      });

      render(<MetaConnectionSection />);
      expect('randomUUID' in globalThis.crypto).toBe(false);
      fireEvent.click(screen.getByTestId('create-meta-connection'));
      fireEvent.click(screen.getByTestId('confirm-meta-connection'));

      await waitFor(() => {
        expect(mockExecute).toHaveBeenCalledWith({
          actionInstanceId: expect.any(String),
          body: { meta_mode: 'customer_sandbox' },
        });
      });
      await waitFor(() => {
        expect(mockRefresh).toHaveBeenCalled();
      });
    } finally {
      restoreCrypto();
    }
  });

  test('keeps the form open and shows the problem when creation is refused', async () => {
    mockUseDomainQuery.mockReturnValue(emptyList());
    mockExecute.mockResolvedValue({
      kind: 'problem',
      problem: problemDetails('workspace_provisioning', 'trace-create'),
      meta: { etag: null, retryAt: null, location: null },
    });

    render(<MetaConnectionSection />);
    fireEvent.click(screen.getByTestId('create-meta-connection'));
    fireEvent.click(screen.getByTestId('confirm-meta-connection'));

    const problem = await screen.findByTestId('meta-connection-create-problem');
    expect(problem).toHaveTextContent('workspace_provisioning');
    expect(problem).toHaveTextContent('trace-create');
    expect(screen.getByTestId('confirm-meta-connection')).toBeInTheDocument();
    expect(mockRefresh).not.toHaveBeenCalled();
  });

  test('reports an unavailable connection list instead of offering to create one', () => {
    mockUseDomainQuery.mockReturnValue({
      data: undefined,
      problem: problemDetails('projection_sync_unavailable', 'trace-list'),
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: mockRefresh,
    } as any);

    render(<MetaConnectionSection />);
    const problem = screen.getByTestId('meta-connection-list-problem');
    expect(problem).toHaveTextContent('projection_sync_unavailable');
    expect(problem).toHaveTextContent('trace-list');
    expect(
      screen.queryByTestId('create-meta-connection')
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(mockRefresh).toHaveBeenCalled();
  });
});
