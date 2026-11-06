/**
 * @jest-environment jsdom
 */
import { renderHook, act } from '@testing-library/react';
import { createElement, type ReactNode } from 'react';

let mockSaveOutcome: unknown = {
  kind: 'success',
  data: { revision: 2 },
  meta: { etag: 'etag-2' },
};
let mockValidateOutcome: unknown = {
  kind: 'success',
  data: { findings: [], revision: 1, content_hash: 'h1' },
  meta: {},
};

const mockSaveRun = jest.fn(async () => mockSaveOutcome);
const mockSaveReset = jest.fn();
const mockValidateRun = jest.fn(async () => mockValidateOutcome);
const mockValidateReset = jest.fn();
const mockInvalidate = jest.fn(async () => {});

jest.mock('@/hooks/library/useLibraryCommand', () => ({
  useLibraryCommand: (kind: string) => {
    if (kind === 'save_draft') {
      return {
        run: mockSaveRun,
        reset: mockSaveReset,
        retry: jest.fn(),
        dismiss: jest.fn(),
        isPending: false,
        problem: undefined,
        reconciliation: undefined,
      };
    }
    if (kind === 'validate_draft') {
      return {
        run: mockValidateRun,
        reset: mockValidateReset,
        retry: jest.fn(),
        dismiss: jest.fn(),
        isPending: false,
        problem: undefined,
        reconciliation: undefined,
      };
    }
    return {
      run: jest.fn(),
      reset: jest.fn(),
      retry: jest.fn(),
      dismiss: jest.fn(),
      isPending: false,
      problem: undefined,
      reconciliation: undefined,
    };
  },
}));

jest.mock('@/hooks/library/useLibraryCache', () => ({
  useLibraryCache: () => ({ invalidate: mockInvalidate }),
}));

let mockBlockerRegistrations: Array<{
  isBlocked: () => boolean;
  onDiscard: () => void;
}> = [];

jest.mock('@/components/workspace/NavigationBlocker', () => ({
  useNavigationBlocker: () => ({
    register: (reg: { isBlocked: () => boolean; onDiscard: () => void }) => {
      mockBlockerRegistrations.push(reg);
      return () => {
        mockBlockerRegistrations = mockBlockerRegistrations.filter(
          (r) => r !== reg
        );
      };
    },
    navigate: jest.fn(),
    isBlocked: () => mockBlockerRegistrations.some((r) => r.isBlocked()),
  }),
  NavigationBlockerProvider: ({ children }: { children: ReactNode }) =>
    createElement('div', null, children),
}));

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

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => ({}),
}));

import { useDraftSession } from '@/hooks/library/useDraftSession';

function makeDraft(overrides?: Record<string, unknown>) {
  return {
    id: 'd1',
    entry_id: 'e1',
    revision: 1,
    state: 'editing',
    content_schema_version: 1,
    content: { name: 'Widget Pro', description: 'A widget' },
    content_hash: 'h1',
    etag: 'etag-1',
    ...overrides,
  };
}

describe('useDraftSession', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockBlockerRegistrations = [];
    mockSaveOutcome = {
      kind: 'success',
      data: { revision: 2 },
      meta: { etag: 'etag-2' },
    };
    mockValidateOutcome = {
      kind: 'success',
      data: { findings: [], revision: 1, content_hash: 'h1' },
      meta: {},
    };
  });

  it('initializes buffer from draft content', () => {
    const { result } = renderHook(() =>
      useDraftSession('e1', 'product', makeDraft())
    );
    expect(result.current.buffer).toBeDefined();
    expect(result.current.buffer?.fields.name).toBe('Widget Pro');
    expect(result.current.isDirty).toBe(false);
    expect(result.current.isSupported).toBe(true);
  });

  it('returns unsupported for unknown schema version', () => {
    const { result } = renderHook(() =>
      useDraftSession(
        'e1',
        'product',
        makeDraft({ content_schema_version: 99 })
      )
    );
    expect(result.current.isSupported).toBe(false);
    expect(result.current.buffer).toBeNull();
  });

  it('returns null buffer when no draft', () => {
    const { result } = renderHook(() => useDraftSession('e1', 'product', null));
    expect(result.current.buffer).toBeNull();
    expect(result.current.isDirty).toBe(false);
  });

  it('returns null buffer when no entryType', () => {
    const { result } = renderHook(() =>
      useDraftSession('e1', null, makeDraft())
    );
    expect(result.current.buffer).toBeNull();
  });

  it('marks dirty on updateField', () => {
    const { result } = renderHook(() =>
      useDraftSession('e1', 'product', makeDraft())
    );

    act(() => {
      result.current.updateField('name', 'Changed');
    });

    expect(result.current.isDirty).toBe(true);
    expect(result.current.buffer?.fields.name).toBe('Changed');
  });

  it('marks dirty on updateFields', () => {
    const { result } = renderHook(() =>
      useDraftSession('e1', 'product', makeDraft())
    );

    act(() => {
      result.current.updateFields({
        name: 'New Name',
        description: 'New Desc',
      });
    });

    expect(result.current.isDirty).toBe(true);
    expect(result.current.buffer?.fields.name).toBe('New Name');
    expect(result.current.buffer?.fields.description).toBe('New Desc');
  });

  it('replaceContent swaps the whole draft, dropping removed keys', () => {
    const { result } = renderHook(() =>
      useDraftSession(
        'e1',
        'product',
        makeDraft({
          content: {
            name: 'Widget Pro',
            description: 'A widget',
            must_match: [{ key: 'logo' }],
          },
        })
      )
    );
    expect(result.current.buffer?._unknown.must_match).toEqual([
      { key: 'logo' },
    ]);

    act(() => {
      result.current.replaceContent({
        name: 'Widget Max',
        selling_points: ['fast'],
        reference_bindings: [],
      });
    });

    expect(result.current.isDirty).toBe(true);
    expect(result.current.buffer?.fields.name).toBe('Widget Max');
    expect(result.current.buffer?.fields.description).toBe('');
    expect(result.current.buffer?.fields.selling_points).toEqual(['fast']);
    expect(result.current.buffer?._unknown).toEqual({ reference_bindings: [] });
  });

  it('discard restores buffer to snapshot', () => {
    const { result } = renderHook(() =>
      useDraftSession('e1', 'product', makeDraft())
    );

    act(() => {
      result.current.updateField('name', 'Changed');
    });
    expect(result.current.isDirty).toBe(true);

    act(() => {
      result.current.discard();
    });

    expect(result.current.isDirty).toBe(false);
    expect(result.current.buffer?.fields.name).toBe('Widget Pro');
  });

  describe('save', () => {
    it('saves and updates revision on success', async () => {
      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      act(() => {
        result.current.updateField('name', 'Changed');
      });

      let saved = false;
      await act(async () => {
        saved = await result.current.save();
      });

      expect(saved).toBe(true);
      expect(result.current.isDirty).toBe(false);
      expect(mockSaveRun).toHaveBeenCalledTimes(1);
      expect(mockSaveReset).toHaveBeenCalled();
      expect(mockInvalidate).toHaveBeenCalledWith({
        resources: ['entry', 'entryList', 'events'],
      });
    });

    it('sets saveProblem on non-409 failure', async () => {
      mockSaveOutcome = {
        kind: 'problem',
        problem: {
          type: 'validation',
          status: 422,
          detail: 'invalid content',
          title: 'Validation Error',
          instance: '',
          code: 'validation_error',
          trace_id: '',
          retryable: false,
          field_errors: [],
        },
        meta: {},
      };

      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      act(() => {
        result.current.updateField('name', 'Changed');
      });

      let saved = false;
      await act(async () => {
        saved = await result.current.save();
      });

      expect(saved).toBe(false);
      expect(result.current.saveProblem).toBeDefined();
      expect(result.current.saveProblem?.status).toBe(422);
    });

    it('creates conflict state on 409', async () => {
      mockSaveOutcome = {
        kind: 'problem',
        problem: {
          type: 'conflict',
          status: 409,
          detail: 'revision conflict',
          title: 'Conflict',
          instance: '',
          code: 'revision_conflict',
          trace_id: '',
          retryable: false,
          field_errors: [],
          server_revision: 3,
          server_content: { name: 'Server Version' },
        },
        meta: {},
      };

      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      act(() => {
        result.current.updateField('name', 'Changed');
      });

      await act(async () => {
        await result.current.save();
      });

      expect(result.current.conflict).toBeDefined();
      expect(result.current.conflict?.serverRevision).toBe(3);
      expect(result.current.conflict?.serverContent).toEqual({
        name: 'Server Version',
      });
    });

    it('does not save when entryId is null', async () => {
      const { result } = renderHook(() =>
        useDraftSession(null, 'product', makeDraft())
      );

      let saved = false;
      await act(async () => {
        saved = await result.current.save();
      });

      expect(saved).toBe(false);
      expect(mockSaveRun).not.toHaveBeenCalled();
    });
  });

  describe('conflict resolution', () => {
    async function setupConflict() {
      mockSaveOutcome = {
        kind: 'problem',
        problem: {
          type: 'conflict',
          status: 409,
          detail: 'revision conflict',
          title: 'Conflict',
          instance: '',
          code: 'revision_conflict',
          trace_id: '',
          retryable: false,
          field_errors: [],
          server_revision: 3,
          server_content: {
            name: 'Server Edit',
            description: 'server desc',
          },
        },
        meta: {},
      };

      const hook = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      act(() => {
        hook.result.current.updateField('name', 'Local Edit');
      });

      await act(async () => {
        await hook.result.current.save();
      });

      return hook;
    }

    it('resolveConflictWithServer loads server content', async () => {
      const { result } = await setupConflict();
      expect(result.current.conflict).toBeDefined();

      act(() => {
        result.current.resolveConflictWithServer();
      });

      expect(result.current.conflict).toBeNull();
      expect(result.current.buffer?.fields.name).toBe('Server Edit');
      expect(result.current.isDirty).toBe(false);
    });

    it('resolveConflictWithLocal keeps local buffer and bumps revision', async () => {
      const { result } = await setupConflict();

      act(() => {
        result.current.resolveConflictWithLocal();
      });

      expect(result.current.conflict).toBeNull();
      expect(result.current.buffer?.fields.name).toBe('Local Edit');
      expect(result.current.isDirty).toBe(true);
    });

    it('cancelConflict dismisses dialog without changing buffer', async () => {
      const { result } = await setupConflict();

      act(() => {
        result.current.cancelConflict();
      });

      expect(result.current.conflict).toBeNull();
      expect(result.current.buffer?.fields.name).toBe('Local Edit');
    });
  });

  describe('validation', () => {
    it('validates and stores result', async () => {
      mockValidateOutcome = {
        kind: 'success',
        data: {
          findings: [
            {
              code: 'missing_field',
              severity: 'error',
              field: 'audience',
              message: 'Required',
            },
          ],
          revision: 1,
          content_hash: 'h1',
        },
        meta: {},
      };

      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      let valid = false;
      await act(async () => {
        valid = await result.current.validate();
      });

      expect(valid).toBe(false);
      expect(result.current.validation).toBeDefined();
      expect(result.current.validation?.findings).toHaveLength(1);
      expect(result.current.validation?.findings[0].severity).toBe('error');
    });

    it('validates clean when no findings', async () => {
      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      let valid = false;
      await act(async () => {
        valid = await result.current.validate();
      });

      expect(valid).toBe(true);
      expect(result.current.validation?.findings).toHaveLength(0);
    });

    it('acknowledges warnings', async () => {
      mockValidateOutcome = {
        kind: 'success',
        data: {
          findings: [
            {
              code: 'short_description',
              severity: 'warning',
              field: 'description',
              message: 'Consider adding more detail',
            },
          ],
          revision: 1,
          content_hash: 'h1',
        },
        meta: {},
      };

      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      await act(async () => {
        await result.current.validate();
      });

      expect(result.current.validation?.acknowledged.size).toBe(0);

      act(() => {
        result.current.acknowledgeWarning('short_description');
      });

      expect(
        result.current.validation?.acknowledged.has('short_description')
      ).toBe(true);
    });
  });

  describe('canPublish', () => {
    it('returns false when dirty', () => {
      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      act(() => {
        result.current.updateField('name', 'Changed');
      });

      expect(result.current.canPublish()).toBe(false);
    });

    it('returns false when no validation', () => {
      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );
      expect(result.current.canPublish()).toBe(false);
    });

    it('returns false with unacknowledged warnings', async () => {
      mockValidateOutcome = {
        kind: 'success',
        data: {
          findings: [
            { code: 'w1', severity: 'warning', field: null, message: 'Warn' },
          ],
          revision: 1,
          content_hash: 'h1',
        },
        meta: {},
      };

      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      await act(async () => {
        await result.current.validate();
      });

      expect(result.current.canPublish()).toBe(false);
    });

    it('returns true when clean, validated, and warnings acknowledged', async () => {
      mockValidateOutcome = {
        kind: 'success',
        data: {
          findings: [
            { code: 'w1', severity: 'warning', field: null, message: 'Warn' },
          ],
          revision: 1,
          content_hash: 'h1',
        },
        meta: {},
      };

      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      await act(async () => {
        await result.current.validate();
      });

      act(() => {
        result.current.acknowledgeWarning('w1');
      });

      expect(result.current.canPublish()).toBe(true);
    });

    it('returns false with errors even if acknowledged', async () => {
      mockValidateOutcome = {
        kind: 'success',
        data: {
          findings: [
            { code: 'e1', severity: 'error', field: null, message: 'Error' },
          ],
          revision: 1,
          content_hash: 'h1',
        },
        meta: {},
      };

      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      await act(async () => {
        await result.current.validate();
      });

      expect(result.current.canPublish()).toBe(false);
    });

    it('returns false when validation revision does not match snapshot', async () => {
      mockValidateOutcome = {
        kind: 'success',
        data: {
          findings: [],
          revision: 999,
          content_hash: 'h-different',
        },
        meta: {},
      };

      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      await act(async () => {
        await result.current.validate();
      });

      expect(result.current.canPublish()).toBe(false);
    });
  });

  describe('navigation blocker integration', () => {
    it('registers a navigation blocker', () => {
      renderHook(() => useDraftSession('e1', 'product', makeDraft()));
      expect(mockBlockerRegistrations.length).toBeGreaterThan(0);
    });

    it('blocker reports blocked when dirty', () => {
      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      act(() => {
        result.current.updateField('name', 'Changed');
      });

      const blocker =
        mockBlockerRegistrations[mockBlockerRegistrations.length - 1];
      expect(blocker.isBlocked()).toBe(true);
    });

    it('blocker onDiscard resets buffer to snapshot', () => {
      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      act(() => {
        result.current.updateField('name', 'Changed');
      });

      const blocker =
        mockBlockerRegistrations[mockBlockerRegistrations.length - 1];
      act(() => {
        blocker.onDiscard();
      });

      expect(result.current.isDirty).toBe(false);
      expect(result.current.buffer?.fields.name).toBe('Widget Pro');
    });
  });

  describe('server normalization', () => {
    it('save response loss still returns false', async () => {
      mockSaveOutcome = {
        kind: 'reconciliation',
        actionInstanceId: 'a1',
        staleSince: 1,
      };

      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      act(() => {
        result.current.updateField('name', 'Changed');
      });

      let saved = false;
      await act(async () => {
        saved = await result.current.save();
      });

      expect(saved).toBe(false);
    });
  });

  describe('validation arriving after later edit', () => {
    it('save clears stale validation', async () => {
      mockValidateOutcome = {
        kind: 'success',
        data: { findings: [], revision: 1, content_hash: 'h1' },
        meta: {},
      };

      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );

      await act(async () => {
        await result.current.validate();
      });
      expect(result.current.validation).toBeDefined();

      act(() => {
        result.current.updateField('name', 'Changed');
      });

      mockSaveOutcome = {
        kind: 'success',
        data: { revision: 2 },
        meta: { etag: 'etag-2' },
      };

      await act(async () => {
        await result.current.save();
      });

      expect(result.current.validation).toBeNull();
    });
  });
});
