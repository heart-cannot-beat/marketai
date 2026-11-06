/**
 * @jest-environment jsdom
 */
import { createElement, type ReactNode } from 'react';
import { renderHook, act } from '@testing-library/react';
import { SWRConfig } from 'swr';
import { useLibraryCommand } from '@/hooks/library/useLibraryCommand';

let mockSlug = 'acme';

const mockGateway = {
  createLibraryEntry: jest.fn(),
  publishLibraryEntry: jest.fn(),
  archiveLibraryEntry: jest.fn(),
  withdrawLibraryVersion: jest.fn(),
  forkLibraryVersion: jest.fn(),
  cloneLibraryProduct: jest.fn(),
  saveLibraryDraft: jest.fn(),
  validateLibraryDraft: jest.fn(),
  bindLibraryReference: jest.fn(),
  applyLibraryTemplate: jest.fn(),
  createLibraryReferenceObject: jest.fn(),
  createLibraryReferenceDownloadIntent: jest.fn(),
  getLibraryCommandOutcome: jest.fn(),
  reconcileLibraryCommandOutcome: jest.fn(),
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: {
      slug: mockSlug,
      workspaceId: 'w1',
      role: 'OWNER',
      membershipId: 'm1',
      displayName: 'Acme',
    },
  }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));

function wrapper({ children }: { children: ReactNode }) {
  return createElement(
    SWRConfig,
    { value: { provider: () => new Map(), dedupingInterval: 0 } },
    children
  );
}

function makeSuccess(data: unknown, status = 201) {
  return {
    kind: status === 200 ? 'ok' : 'created',
    status,
    data,
    meta: { etag: null, retryAt: null, location: null },
  };
}

function makeProblem(status: number, code: string) {
  return {
    kind: 'problem' as const,
    status,
    problem: {
      type: code,
      title: code,
      status,
      detail: '',
      instance: '',
      code,
      trace_id: '',
      retryable: false,
      field_errors: [],
    },
    meta: { etag: null, retryAt: null, location: null },
  };
}

function makeReconciliation(actionInstanceId: string, staleSince: number) {
  return {
    kind: 'reconciliation_required' as const,
    actionInstanceId,
    staleSince,
  };
}

describe('useLibraryCommand', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockSlug = 'acme';
  });

  it('runs a create_entry command and returns success outcome', async () => {
    mockGateway.createLibraryEntry.mockResolvedValue(
      makeSuccess({ id: 'entry_1', display_name: 'Product A' })
    );

    const { result } = renderHook(() => useLibraryCommand('create_entry'), {
      wrapper,
    });

    let outcome: any;
    await act(async () => {
      outcome = await result.current.run({
        body: { entry_type: 'product', display_name: 'Product A' },
      });
    });

    expect(outcome.kind).toBe('success');
    expect(outcome.data).toEqual({ id: 'entry_1', display_name: 'Product A' });
    expect(mockGateway.createLibraryEntry).toHaveBeenCalledTimes(1);
    expect(mockGateway.createLibraryEntry).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        body: { entry_type: 'product', display_name: 'Product A' },
      })
    );
  });

  it('runs a publish command with entry_id pathParam', async () => {
    mockGateway.publishLibraryEntry.mockResolvedValue(
      makeSuccess({ id: 'v1', lifecycle: 'published' })
    );

    const { result } = renderHook(() => useLibraryCommand('publish'), {
      wrapper,
    });

    let outcome: any;
    await act(async () => {
      outcome = await result.current.run({
        entry_id: 'entry_42',
        body: { revision: 'rev_1' },
      });
    });

    expect(outcome.kind).toBe('success');
    expect(mockGateway.publishLibraryEntry).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        entryId: 'entry_42',
        body: { revision: 'rev_1' },
      })
    );
  });

  it('surfaces a problem from the domain', async () => {
    mockGateway.createLibraryEntry.mockResolvedValue(
      makeProblem(409, 'entry_exists')
    );

    const { result } = renderHook(() => useLibraryCommand('create_entry'), {
      wrapper,
    });

    let outcome: any;
    await act(async () => {
      outcome = await result.current.run({
        body: { entry_type: 'product', display_name: 'Dup' },
      });
    });

    expect(outcome.kind).toBe('problem');
    expect(outcome.problem.code).toBe('entry_exists');
    expect(result.current.problem?.code).toBe('entry_exists');
  });

  it('returns command_already_pending for different params while pending', async () => {
    let resolver: (v: any) => void;
    mockGateway.forkLibraryVersion.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolver = resolve;
        })
    );

    const { result } = renderHook(() => useLibraryCommand('fork'), {
      wrapper,
    });

    let firstPromise: Promise<any>;
    act(() => {
      firstPromise = result.current.run({
        version_id: 'v1',
        body: { target_entry_id: 'e1' },
      });
    });

    let secondOutcome: any;
    await act(async () => {
      secondOutcome = await result.current.run({
        version_id: 'v2',
        body: { target_entry_id: 'e2' },
      });
    });

    expect(secondOutcome.kind).toBe('problem');
    expect(secondOutcome.problem.code).toBe('command_already_pending');

    await act(async () => {
      resolver!(makeSuccess({ id: 'forked' }));
      await firstPromise!;
    });
  });

  it('coalesces duplicate run with same params while pending', async () => {
    let resolver: (v: any) => void;
    mockGateway.cloneLibraryProduct.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolver = resolve;
        })
    );

    const { result } = renderHook(() => useLibraryCommand('clone'), {
      wrapper,
    });

    let promise1: Promise<any>;
    let promise2: Promise<any>;
    act(() => {
      promise1 = result.current.run({
        version_id: 'v1',
        body: { display_name: 'Clone' },
      });
      promise2 = result.current.run({
        version_id: 'v1',
        body: { display_name: 'Clone' },
      });
    });

    await act(async () => {
      resolver!(makeSuccess({ id: 'cloned' }));
      const [out1, out2] = await Promise.all([promise1!, promise2!]);
      expect(out1.kind).toBe('success');
      expect(out2.kind).toBe('success');
    });

    expect(mockGateway.cloneLibraryProduct).toHaveBeenCalledTimes(1);
  });

  it('isPending is true during execution', async () => {
    let resolver: (v: any) => void;
    mockGateway.archiveLibraryEntry.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolver = resolve;
        })
    );

    const { result } = renderHook(() => useLibraryCommand('archive'), {
      wrapper,
    });

    expect(result.current.isPending).toBe(false);

    let promise: Promise<any>;
    act(() => {
      promise = result.current.run({
        entry_id: 'e1',
        body: { reason: 'test' },
      });
    });

    expect(result.current.isPending).toBe(true);

    await act(async () => {
      resolver!(makeSuccess({ id: 'e1', lifecycle: 'archived' }, 200));
      await promise!;
    });

    expect(result.current.isPending).toBe(false);
  });

  it('reset clears problem and reconciliation display state', async () => {
    mockGateway.createLibraryEntry.mockResolvedValue(
      makeProblem(422, 'validation_error')
    );

    const { result } = renderHook(() => useLibraryCommand('create_entry'), {
      wrapper,
    });

    await act(async () => {
      await result.current.run({
        body: { entry_type: 'product', display_name: '' },
      });
    });
    expect(result.current.problem).toBeDefined();

    act(() => result.current.reset());
    expect(result.current.problem).toBeUndefined();
  });

  it('dismiss hides reconciliation from display without clearing intent', async () => {
    mockGateway.withdrawLibraryVersion.mockResolvedValue(
      makeReconciliation('action_x', 5000)
    );
    mockGateway.getLibraryCommandOutcome.mockResolvedValue(
      makeProblem(404, 'not_found')
    );

    const { result } = renderHook(() => useLibraryCommand('withdraw'), {
      wrapper,
    });

    await act(async () => {
      await result.current.run({
        version_id: 'v1',
        body: { reason: 'test' },
      });
    });

    expect(result.current.reconciliation).toBeDefined();

    act(() => result.current.dismiss());
    expect(result.current.reconciliation).toBeUndefined();
  });

  it('reconciliation triggers command-outcome lookup', async () => {
    mockGateway.saveLibraryDraft.mockResolvedValue(
      makeReconciliation('action_recon', 3000)
    );
    mockGateway.getLibraryCommandOutcome.mockResolvedValue(
      makeSuccess({ id: 'e1', draft: 'saved' }, 200)
    );

    const { result } = renderHook(() => useLibraryCommand('save_draft'), {
      wrapper,
    });

    let outcome: any;
    await act(async () => {
      outcome = await result.current.run({
        entry_id: 'e1',
        body: { content: 'draft content' },
      });
    });

    expect(mockGateway.getLibraryCommandOutcome).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        commandKind: 'save_draft',
      })
    );
    expect(outcome.kind).toBe('success');
  });

  it('reconciliation lookup 404 falls back to reconciliation outcome', async () => {
    mockGateway.validateLibraryDraft.mockResolvedValue(
      makeReconciliation('action_recon2', 4000)
    );
    mockGateway.getLibraryCommandOutcome.mockResolvedValue(
      makeProblem(404, 'not_found')
    );

    const { result } = renderHook(() => useLibraryCommand('validate_draft'), {
      wrapper,
    });

    let outcome: any;
    await act(async () => {
      outcome = await result.current.run({
        entry_id: 'e1',
      });
    });

    expect(outcome.kind).toBe('reconciliation');
  });

  it('works with all 12 command kinds', async () => {
    const kinds = [
      {
        kind: 'create_entry' as const,
        mock: 'createLibraryEntry',
        params: { body: { entry_type: 'product' } },
      },
      {
        kind: 'save_draft' as const,
        mock: 'saveLibraryDraft',
        params: { entry_id: 'e1', body: {} },
      },
      {
        kind: 'validate_draft' as const,
        mock: 'validateLibraryDraft',
        params: { entry_id: 'e1' },
      },
      {
        kind: 'publish' as const,
        mock: 'publishLibraryEntry',
        params: { entry_id: 'e1', body: {} },
      },
      {
        kind: 'archive' as const,
        mock: 'archiveLibraryEntry',
        params: { entry_id: 'e1', body: {} },
      },
      {
        kind: 'bind_reference' as const,
        mock: 'bindLibraryReference',
        params: { entry_id: 'e1', body: {} },
      },
      {
        kind: 'withdraw' as const,
        mock: 'withdrawLibraryVersion',
        params: { version_id: 'v1', body: {} },
      },
      {
        kind: 'fork' as const,
        mock: 'forkLibraryVersion',
        params: { version_id: 'v1', body: {} },
      },
      {
        kind: 'clone' as const,
        mock: 'cloneLibraryProduct',
        params: { version_id: 'v1', body: {} },
      },
      {
        kind: 'apply_template' as const,
        mock: 'applyLibraryTemplate',
        params: { template_version_id: 'tv1', body: {} },
      },
      {
        kind: 'create_reference_object' as const,
        mock: 'createLibraryReferenceObject',
        params: { body: {} },
      },
      {
        kind: 'create_reference_download' as const,
        mock: 'createLibraryReferenceDownloadIntent',
        params: { version_id: 'v1' },
      },
    ];

    for (const { kind, mock, params } of kinds) {
      jest.clearAllMocks();
      (mockGateway as any)[mock].mockResolvedValue(
        makeSuccess({ id: `result_${kind}` })
      );

      const { result } = renderHook(() => useLibraryCommand(kind), {
        wrapper,
      });

      let outcome: any;
      await act(async () => {
        outcome = await result.current.run(params);
      });

      expect(outcome.kind).toBe('success');
      expect((mockGateway as any)[mock]).toHaveBeenCalledTimes(1);
    }
  });

  it('no duplicate fork after timeout — second run gets new actionInstanceId', async () => {
    let callCount = 0;
    const actionIds: string[] = [];

    mockGateway.forkLibraryVersion.mockImplementation((args: any) => {
      callCount++;
      actionIds.push(args.actionInstanceId);
      return Promise.resolve(makeSuccess({ id: `fork_${callCount}` }));
    });

    const { result } = renderHook(() => useLibraryCommand('fork'), {
      wrapper,
    });

    await act(async () => {
      await result.current.run({
        version_id: 'v1',
        body: { target_entry_id: 'e1' },
      });
    });

    await act(async () => {
      await result.current.run({
        version_id: 'v1',
        body: { target_entry_id: 'e1' },
      });
    });

    expect(callCount).toBe(2);
    expect(actionIds[0]).not.toBe(actionIds[1]);
  });
});
