/**
 * @jest-environment jsdom
 */
import fs from 'node:fs';
import path from 'node:path';
import { renderHook, act } from '@testing-library/react';
import { createElement, type ReactNode } from 'react';
import { RESPONSE_VALIDATORS } from '@/lib/domain-client/generated/validators';

/** One finding as FastAPI sends it: `Finding.as_dict()` (`app/library/contracts.py`). */
interface BackendFinding {
  code: string;
  message: string;
  path: string;
  severity: 'blocking' | 'warning';
}

const VARIANT_LABEL_MISSING: BackendFinding = {
  code: 'product_variant_label',
  message: '型号需要 label',
  path: 'model_variants[1].label',
  severity: 'blocking',
};
const DEPENDENCY_WARNING: BackendFinding = {
  code: 'dependency_warning',
  message: '引用的版本资格为 warning: lv_kestrel_01',
  path: 'reference_bindings[0].reference_version_id',
  severity: 'warning',
};
const PHRASE_RESTORED: BackendFinding = {
  code: 'protected_phrase_restored',
  message: "受保护短语 'acme_claim' 已被移除并自动恢复，发布前需重新确认。",
  path: 'locked_phrases',
  severity: 'warning',
};

/**
 * `POST /library/entries/{id}/validate` as FastAPI answers it:
 * `ValidationReport.as_dict()` through `LibraryValidationResult`
 * (`app/library/response_models.py`) -- blocking findings and warnings in
 * separate lists, with no `findings` list and no `revision`.
 */
function validationBody(...findings: BackendFinding[]) {
  const blocking = findings.filter((f) => f.severity === 'blocking');
  return {
    entry_type: 'product',
    ok: blocking.length === 0,
    content_hash: 'h1',
    blocking,
    warnings: findings.filter((f) => f.severity === 'warning'),
  };
}

let mockSaveOutcome: unknown = {
  kind: 'success',
  data: { revision: 2 },
  meta: { etag: 'etag-2' },
};
let mockValidateOutcome: unknown = {
  kind: 'success',
  data: validationBody(),
  meta: {},
};

function validateSucceeds(...findings: BackendFinding[]) {
  mockValidateOutcome = {
    kind: 'success',
    data: validationBody(...findings),
    meta: {},
  };
}

/**
 * The OpenAPI schema types each finding as an open object, so the runtime
 * validator cannot see a renamed finding key. The keys `Finding.as_dict()`
 * writes and the `SEVERITY_*` values are read from the backend source instead.
 */
const CONTRACTS = fs.readFileSync(
  path.resolve(__dirname, '../../../../app/library/contracts.py'),
  'utf8'
);
const findingClass = (() => {
  const start = CONTRACTS.search(/^class Finding:$/m);
  expect(start).toBeGreaterThanOrEqual(0);
  const end = CONTRACTS.indexOf('\n\n\n', start);
  return CONTRACTS.slice(start, end);
})();
const FINDING_KEYS = Array.from(
  findingClass.matchAll(/"(\w+)": self\.\1\b/g),
  (m) => m[1]
);
const SEVERITIES = Array.from(
  CONTRACTS.matchAll(/^SEVERITY_[A-Z]+ = "(\w+)"$/gm),
  (m) => m[1]
);

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
    validateSucceeds();
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
    async function validated(...findings: BackendFinding[]) {
      validateSucceeds(...findings);
      const hook = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );
      let valid: boolean | undefined;
      await act(async () => {
        valid = await hook.result.current.validate();
      });
      return { ...hook, valid };
    }

    it('reads blocking findings and their content path from the response', async () => {
      const { result, valid } = await validated(
        VARIANT_LABEL_MISSING,
        DEPENDENCY_WARNING
      );

      expect(valid).toBe(false);
      expect(result.current.validation?.blocking).toEqual([
        VARIANT_LABEL_MISSING,
      ]);
      expect(result.current.validation?.warnings).toEqual([DEPENDENCY_WARNING]);
    });

    it('validates clean when there are no blocking findings or warnings', async () => {
      const { result, valid } = await validated();

      expect(valid).toBe(true);
      expect(result.current.validation?.blocking).toEqual([]);
      expect(result.current.validation?.warnings).toEqual([]);
    });

    it('passes validation with warnings alone', async () => {
      const { result, valid } = await validated(DEPENDENCY_WARNING);

      expect(valid).toBe(true);
      expect(result.current.validation?.warnings).toEqual([DEPENDENCY_WARNING]);
    });

    it('records the revision it validated, which the response does not carry', async () => {
      const { result } = await validated();

      expect(mockValidateRun).toHaveBeenCalledWith({
        entry_id: 'e1',
        ifMatch: 1,
      });
      expect(result.current.validation?.revision).toBe(1);
      expect(result.current.validation?.contentHash).toBe('h1');
    });

    it('acknowledges warnings by code', async () => {
      const { result } = await validated(DEPENDENCY_WARNING);

      expect(result.current.validation?.acknowledged.size).toBe(0);

      act(() => {
        result.current.acknowledgeWarning(DEPENDENCY_WARNING.code);
      });

      expect(
        result.current.validation?.acknowledged.has(DEPENDENCY_WARNING.code)
      ).toBe(true);
    });
  });

  describe('the report stored on the draft', () => {
    // `DraftSummary.validation`: the last validation of this revision, or
    // the row report a manual fact import wrote with the draft (#23).
    it('shows the stored report of the revision it arrives with', () => {
      const { result } = renderHook(() =>
        useDraftSession(
          'e1',
          'product',
          makeDraft({
            revision: 4,
            validation: validationBody(
              VARIANT_LABEL_MISSING,
              DEPENDENCY_WARNING
            ),
          })
        )
      );

      expect(result.current.validation?.blocking).toEqual([
        VARIANT_LABEL_MISSING,
      ]);
      expect(result.current.validation?.warnings).toEqual([DEPENDENCY_WARNING]);
      expect(result.current.validation?.revision).toBe(4);
      expect(result.current.validation?.contentHash).toBe('h1');
      expect(result.current.validation?.acknowledged.size).toBe(0);
      expect(mockValidateRun).not.toHaveBeenCalled();
    });

    it.each([
      ['no report', undefined],
      ['a cleared report', null],
      ['a value that is no report', { ok: true }],
    ])('shows nothing for %s', (_name, validation) => {
      const { result } = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft({ validation }))
      );
      expect(result.current.validation).toBeNull();
    });

    it('drops the stored report when a saved revision arrives without one', () => {
      const { result, rerender } = renderHook(
        ({ draft }) => useDraftSession('e1', 'product', draft),
        {
          initialProps: {
            draft: makeDraft({
              validation: validationBody(VARIANT_LABEL_MISSING),
            }),
          },
        }
      );
      expect(result.current.validation?.blocking).toHaveLength(1);

      rerender({
        draft: makeDraft({ revision: 2, etag: 'etag-2', validation: null }),
      });
      expect(result.current.validation).toBeNull();
    });
  });

  describe('validate response fixtures', () => {
    const validator = RESPONSE_VALIDATORS.get('validate_library_draft_200');

    it.each([
      ['clean', validationBody()],
      ['blocking', validationBody(VARIANT_LABEL_MISSING, DEPENDENCY_WARNING)],
      ['warnings only', validationBody(DEPENDENCY_WARNING, PHRASE_RESTORED)],
    ])(
      'the %s body passes the validate_library_draft_200 runtime validator',
      (_name, body) => {
        expect(validator).toBeDefined();
        const accepted = validator!(body);
        expect(validator!.errors ?? []).toEqual([]);
        expect(accepted).toBe(true);
      }
    );

    it('the runtime validator rejects a findings-only body', () => {
      expect(validator!({ findings: [], revision: 1 })).toBe(false);
    });

    it('each finding carries exactly the keys and a severity of the backend Finding', () => {
      expect(FINDING_KEYS).toEqual(['code', 'message', 'path', 'severity']);
      expect(SEVERITIES).toEqual(['blocking', 'warning']);
      for (const finding of [
        VARIANT_LABEL_MISSING,
        DEPENDENCY_WARNING,
        PHRASE_RESTORED,
      ]) {
        expect(Object.keys(finding).sort()).toEqual([...FINDING_KEYS].sort());
        expect(SEVERITIES).toContain(finding.severity);
      }
    });
  });

  describe('canPublish', () => {
    async function validated(...findings: BackendFinding[]) {
      validateSucceeds(...findings);
      const hook = renderHook(() =>
        useDraftSession('e1', 'product', makeDraft())
      );
      await act(async () => {
        await hook.result.current.validate();
      });
      return hook;
    }

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

    it('returns true when validated clean', async () => {
      const { result } = await validated();

      expect(result.current.canPublish()).toBe(true);
    });

    it('returns false while the response has a blocking finding', async () => {
      const { result } = await validated(VARIANT_LABEL_MISSING);

      expect(result.current.canPublish()).toBe(false);
    });

    it('returns false with a blocking finding even if its code was acknowledged', async () => {
      const { result } = await validated(VARIANT_LABEL_MISSING);

      act(() => {
        result.current.acknowledgeWarning(VARIANT_LABEL_MISSING.code);
      });

      expect(result.current.canPublish()).toBe(false);
    });

    it('returns false with unacknowledged warnings', async () => {
      const { result } = await validated(DEPENDENCY_WARNING);

      expect(result.current.canPublish()).toBe(false);
    });

    it('returns true only once every warning is acknowledged', async () => {
      const { result } = await validated(DEPENDENCY_WARNING, PHRASE_RESTORED);

      act(() => {
        result.current.acknowledgeWarning(DEPENDENCY_WARNING.code);
      });
      expect(result.current.canPublish()).toBe(false);

      act(() => {
        result.current.acknowledgeWarning(PHRASE_RESTORED.code);
      });
      expect(result.current.canPublish()).toBe(true);
    });

    it('returns false when the draft revision moved past the validated one', async () => {
      const { result } = await validated();
      expect(result.current.canPublish()).toBe(true);

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
          server_content: { name: 'Server Edit' },
        },
        meta: {},
      };
      act(() => {
        result.current.updateField('name', 'Local Edit');
      });
      await act(async () => {
        await result.current.save();
      });
      act(() => {
        result.current.resolveConflictWithServer();
      });

      expect(result.current.isDirty).toBe(false);
      expect(result.current.validation?.revision).toBe(1);
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
      validateSucceeds();

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
