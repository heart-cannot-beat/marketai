import {
  domainQueryKey,
  paginatedQueryKey,
  matchesOperation,
  matchesWorkspace,
  workspacePrefix,
} from '@/lib/domain-client/query-keys';

describe('domainQueryKey', () => {
  it('produces a stable 4-tuple', () => {
    const key = domainQueryKey('acme', 'get_library_entry', { entry_id: 'e1' });
    expect(key).toEqual([
      'domain',
      'acme',
      'get_library_entry',
      '{"entry_id":"e1"}',
    ]);
  });

  it('strips undefined values', () => {
    const key = domainQueryKey('acme', 'list_library_entries', {
      entry_type: 'product',
      cursor: undefined,
    });
    expect(key[3]).toBe('{"entry_type":"product"}');
  });

  it('sorts keys for structural stability', () => {
    const a = domainQueryKey('acme', 'list_library_entries', {
      limit: 50,
      entry_type: 'product',
    });
    const b = domainQueryKey('acme', 'list_library_entries', {
      entry_type: 'product',
      limit: 50,
    });
    expect(a).toEqual(b);
  });

  it('returns empty object JSON for undefined params', () => {
    const key = domainQueryKey('acme', 'get_identity_probe');
    expect(key[3]).toBe('{}');
  });

  it('different params produce different keys', () => {
    const a = domainQueryKey('acme', 'get_library_entry', { entry_id: 'e1' });
    const b = domainQueryKey('acme', 'get_library_entry', { entry_id: 'e2' });
    expect(a[3]).not.toBe(b[3]);
  });

  it('different workspaces produce different keys', () => {
    const a = domainQueryKey('acme', 'get_identity_probe');
    const b = domainQueryKey('other', 'get_identity_probe');
    expect(a[1]).not.toBe(b[1]);
  });
});

describe('paginatedQueryKey', () => {
  it('produces a key for paginated operations', () => {
    const key = paginatedQueryKey('acme', 'list_library_entries', {
      entry_type: 'product',
    });
    expect(key[0]).toBe('domain');
    expect(key[2]).toBe('list_library_entries');
  });
});

describe('matchesOperation', () => {
  it('returns true for matching operationId', () => {
    const key = domainQueryKey('acme', 'get_library_entry', { entry_id: 'e1' });
    expect(matchesOperation(key, 'get_library_entry')).toBe(true);
  });

  it('returns false for non-matching operationId', () => {
    const key = domainQueryKey('acme', 'get_library_entry', { entry_id: 'e1' });
    expect(matchesOperation(key, 'get_identity_probe')).toBe(false);
  });
});

describe('matchesWorkspace', () => {
  it('returns true for matching workspace', () => {
    const key = domainQueryKey('acme', 'get_identity_probe');
    expect(matchesWorkspace(key, 'acme')).toBe(true);
  });

  it('returns false for different workspace', () => {
    const key = domainQueryKey('acme', 'get_identity_probe');
    expect(matchesWorkspace(key, 'other')).toBe(false);
  });
});

describe('workspacePrefix', () => {
  it('returns a 2-tuple prefix', () => {
    expect(workspacePrefix('acme')).toEqual(['domain', 'acme']);
  });
});
