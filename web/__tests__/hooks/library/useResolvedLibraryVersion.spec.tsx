/**
 * @jest-environment jsdom
 */
import { renderHook } from '@testing-library/react';

let mockQueryResult = {
  data: undefined as unknown,
  problem: undefined as unknown,
  isLoading: false,
  isStale: false,
  lastSuccessfulAt: null,
  refresh: jest.fn(),
};

let lastQueryCall: {
  operationId: string;
  params: unknown;
  options: unknown;
} | null = null;

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (operationId: string, params: unknown, options: unknown) => {
    lastQueryCall = { operationId, params, options };
    return mockQueryResult;
  },
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

import {
  useResolvedLibraryVersion,
  type ResolvedVersion,
  type VersionActionEligibility,
} from '@/hooks/library/useResolvedLibraryVersion';
import fixtures from '../../fixtures/library-versions.json';

describe('useResolvedLibraryVersion', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    lastQueryCall = null;
    mockQueryResult = {
      data: undefined,
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: null,
      refresh: jest.fn(),
    };
  });

  it('does not query when versionId is null', () => {
    renderHook(() => useResolvedLibraryVersion(null, 'entry-1'));
    expect(lastQueryCall?.options).toEqual({ enabled: false });
  });

  it('queries get_library_version with correct params', () => {
    renderHook(() => useResolvedLibraryVersion('v-1', 'entry-1'));
    expect(lastQueryCall?.operationId).toBe('get_library_version');
    expect(lastQueryCall?.options).toEqual({ enabled: true });
  });

  it('returns version when entry_id matches', () => {
    mockQueryResult.data = {
      id: 'v-1',
      entry_id: 'entry-1',
      entry_type: 'product',
      version_no: 1,
      state: 'effective',
      eligibility: 'eligible',
    };
    const { result } = renderHook(() =>
      useResolvedLibraryVersion('v-1', 'entry-1')
    );
    expect(result.current.version).toBeDefined();
    expect(result.current.version?.id).toBe('v-1');
    expect(result.current.mismatch).toBe(false);
  });

  it('types the version the server really sends (#25)', () => {
    // `fixtures/library-versions.json` is FastAPI's own answer
    // (tests/test_library_version_web_fixtures.py); it must type-check as a
    // ResolvedVersion, so a drift in either shape fails `check-types`.
    const owner: ResolvedVersion = fixtures.owner_effective_product;
    const member: ResolvedVersion = fixtures.member_effective_product;
    mockQueryResult.data = member;
    const { result } = renderHook(() =>
      useResolvedLibraryVersion(member.id, member.entry_id)
    );

    const actions = result.current.version?.allowed_actions ?? [];
    expect(actions.map((a) => a.action)).toEqual([
      'clone_product',
      'fork',
      'withdraw',
    ]);
    const withdraw = actions.find(
      (a) => a.action === 'withdraw'
    ) as VersionActionEligibility;
    expect(withdraw).toEqual({
      action: 'withdraw',
      allowed: false,
      reason_code: 'owner_required',
      requirements: ['impact_confirmation'],
    });
    // Every action carries exactly the server's keys.
    for (const action of [
      ...(owner.allowed_actions ?? []),
      ...actions,
    ] as VersionActionEligibility[]) {
      expect(
        Object.keys(action).filter(
          (key) =>
            !['action', 'allowed', 'reason_code', 'requirements'].includes(key)
        )
      ).toEqual([]);
      expect(typeof action.allowed).toBe('boolean');
    }
  });

  it('detects mismatch when entry_id differs', () => {
    mockQueryResult.data = {
      id: 'v-1',
      entry_id: 'entry-other',
      entry_type: 'product',
      version_no: 1,
      state: 'effective',
      eligibility: 'eligible',
    };
    const { result } = renderHook(() =>
      useResolvedLibraryVersion('v-1', 'entry-1')
    );
    expect(result.current.mismatch).toBe(true);
    expect(result.current.version).toBeUndefined();
  });

  it('returns problem when query fails', () => {
    mockQueryResult.problem = {
      type: 'not_found',
      status: 404,
      detail: '',
      instance: '',
      code: 'not_found',
      trace_id: '',
      retryable: false,
      field_errors: [],
    };
    const { result } = renderHook(() =>
      useResolvedLibraryVersion('v-1', 'entry-1')
    );
    expect(result.current.problem).toBeDefined();
    expect(result.current.problem?.status).toBe(404);
  });

  it('returns isLoading when query is loading', () => {
    mockQueryResult.isLoading = true;
    const { result } = renderHook(() =>
      useResolvedLibraryVersion('v-1', 'entry-1')
    );
    expect(result.current.isLoading).toBe(true);
  });

  it('does not report mismatch when expectedEntryId is null', () => {
    mockQueryResult.data = {
      id: 'v-1',
      entry_id: 'entry-1',
      entry_type: 'product',
      version_no: 1,
      state: 'effective',
      eligibility: 'eligible',
    };
    const { result } = renderHook(() => useResolvedLibraryVersion('v-1', null));
    expect(result.current.mismatch).toBe(false);
    expect(result.current.version).toBeDefined();
  });
});
