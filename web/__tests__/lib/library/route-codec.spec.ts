import {
  parseLibraryRoute,
  serializeLibraryRoute,
  normalizeLibraryRoute,
  buildLibraryPath,
  routesEqual,
  type LibraryRouteState,
} from '@/lib/library/route-codec';

function emptyState(overrides?: Partial<LibraryRouteState>): LibraryRouteState {
  return {
    cat: 'product',
    entry: null,
    tab: null,
    version: null,
    resource_type: null,
    resource: null,
    resource_tab: null,
    resource_version: null,
    parent_type: null,
    parent: null,
    ...overrides,
  };
}

describe('parseLibraryRoute', () => {
  it('defaults to product category with empty query', () => {
    expect(parseLibraryRoute({})).toEqual(emptyState());
  });

  it('parses valid category', () => {
    expect(parseLibraryRoute({ cat: 'person' }).cat).toBe('person');
    expect(parseLibraryRoute({ cat: 'template' }).cat).toBe('template');
    expect(parseLibraryRoute({ cat: 'datasource' }).cat).toBe('datasource');
  });

  it('defaults invalid category to product', () => {
    expect(parseLibraryRoute({ cat: 'invalid' }).cat).toBe('product');
    expect(parseLibraryRoute({ cat: '' }).cat).toBe('product');
  });

  it('parses entry and tab', () => {
    const state = parseLibraryRoute({ entry: 'e1', tab: 'draft' });
    expect(state.entry).toBe('e1');
    expect(state.tab).toBe('draft');
  });

  it('rejects invalid tab', () => {
    const state = parseLibraryRoute({ entry: 'e1', tab: 'bogus' });
    expect(state.tab).toBeNull();
  });

  it('parses resource state with parent', () => {
    const state = parseLibraryRoute({
      cat: 'product',
      entry: 'e1',
      resource_type: 'offer',
      resource: 'r1',
      parent_type: 'product',
      parent: 'p1',
    });
    expect(state.resource_type).toBe('offer');
    expect(state.resource).toBe('r1');
    expect(state.parent_type).toBe('product');
    expect(state.parent).toBe('p1');
  });

  it('rejects invalid resource_type', () => {
    const state = parseLibraryRoute({
      entry: 'e1',
      resource_type: 'not_a_type',
      resource: 'r1',
    });
    expect(state.resource_type).toBeNull();
  });

  it('handles array query values (takes first)', () => {
    const state = parseLibraryRoute({ cat: ['person', 'product'] });
    expect(state.cat).toBe('person');
  });
});

describe('serializeLibraryRoute', () => {
  it('omits product category (default)', () => {
    const q = serializeLibraryRoute(emptyState());
    expect(q).toEqual({});
  });

  it('includes non-default category', () => {
    const q = serializeLibraryRoute(emptyState({ cat: 'template' }));
    expect(q).toEqual({ cat: 'template' });
  });

  it('includes entry/tab/version', () => {
    const q = serializeLibraryRoute(
      emptyState({ entry: 'e1', tab: 'versions', version: 'v1' })
    );
    expect(q).toEqual({ entry: 'e1', tab: 'versions', version: 'v1' });
  });

  it('omits null fields', () => {
    const q = serializeLibraryRoute(emptyState({ entry: 'e1' }));
    expect(Object.keys(q)).toEqual(['entry']);
  });
});

describe('normalizeLibraryRoute', () => {
  it('clears all downstream state when entry is null', () => {
    const state = normalizeLibraryRoute(
      emptyState({
        tab: 'draft',
        version: 'v1',
        resource_type: 'offer',
        resource: 'r1',
      })
    );
    expect(state.tab).toBeNull();
    expect(state.version).toBeNull();
    expect(state.resource_type).toBeNull();
    expect(state.resource).toBeNull();
  });

  it('clears version when tab is not versions/impact/bindings', () => {
    const state = normalizeLibraryRoute(
      emptyState({ entry: 'e1', tab: 'draft', version: 'v1' })
    );
    expect(state.version).toBeNull();
  });

  it('preserves version for versions tab', () => {
    const state = normalizeLibraryRoute(
      emptyState({ entry: 'e1', tab: 'versions', version: 'v1' })
    );
    expect(state.version).toBe('v1');
  });

  it('preserves version for impact tab', () => {
    const state = normalizeLibraryRoute(
      emptyState({ entry: 'e1', tab: 'impact', version: 'v1' })
    );
    expect(state.version).toBe('v1');
  });

  it('preserves version for bindings tab', () => {
    const state = normalizeLibraryRoute(
      emptyState({ entry: 'e1', tab: 'bindings', version: 'v1' })
    );
    expect(state.version).toBe('v1');
  });

  it('clears unpaired resource_type (no resource id)', () => {
    const state = normalizeLibraryRoute(
      emptyState({ entry: 'e1', resource_type: 'offer' })
    );
    expect(state.resource_type).toBeNull();
  });

  it('clears unpaired resource (no resource_type)', () => {
    const state = normalizeLibraryRoute(
      emptyState({ entry: 'e1', resource: 'r1' })
    );
    expect(state.resource).toBeNull();
  });

  it('clears unpaired parent_type (no parent id)', () => {
    const state = normalizeLibraryRoute(
      emptyState({ entry: 'e1', parent_type: 'product' })
    );
    expect(state.parent_type).toBeNull();
  });

  it('clears unpaired parent (no parent_type)', () => {
    const state = normalizeLibraryRoute(
      emptyState({ entry: 'e1', parent: 'p1' })
    );
    expect(state.parent).toBeNull();
  });

  it('clears resource-level state when no resource', () => {
    const state = normalizeLibraryRoute(
      emptyState({
        entry: 'e1',
        resource_tab: 'draft',
        resource_version: 'rv1',
      })
    );
    expect(state.resource_tab).toBeNull();
    expect(state.resource_version).toBeNull();
  });

  it('rejects top-level type in resource_type position', () => {
    const state = normalizeLibraryRoute(
      emptyState({
        entry: 'e1',
        resource_type: 'product',
        resource: 'r1',
      })
    );
    expect(state.resource_type).toBeNull();
    expect(state.resource).toBeNull();
  });

  it('allows internal type in resource_type position', () => {
    const state = normalizeLibraryRoute(
      emptyState({
        entry: 'e1',
        resource_type: 'offer',
        resource: 'r1',
      })
    );
    expect(state.resource_type).toBe('offer');
    expect(state.resource).toBe('r1');
  });

  it('parse-normalize-serialize is idempotent', () => {
    const inputs = [
      {},
      { cat: 'person', entry: 'e1', tab: 'draft' },
      { cat: 'product', entry: 'e1', tab: 'versions', version: 'v1' },
      {
        entry: 'e1',
        resource_type: 'offer',
        resource: 'r1',
        resource_tab: 'overview',
        parent_type: 'product',
        parent: 'p1',
      },
      { cat: 'datasource', entry: 'e1', tab: 'events' },
    ];

    for (const q of inputs) {
      const first = normalizeLibraryRoute(parseLibraryRoute(q));
      const serialized = serializeLibraryRoute(first);
      const second = normalizeLibraryRoute(parseLibraryRoute(serialized));
      expect(routesEqual(first, second)).toBe(true);
    }
  });
});

describe('routesEqual', () => {
  it('returns true for identical states', () => {
    const a = emptyState({ cat: 'person', entry: 'e1' });
    const b = emptyState({ cat: 'person', entry: 'e1' });
    expect(routesEqual(a, b)).toBe(true);
  });

  it('returns false when any field differs', () => {
    const a = emptyState({ entry: 'e1' });
    const b = emptyState({ entry: 'e2' });
    expect(routesEqual(a, b)).toBe(false);
  });
});

describe('buildLibraryPath', () => {
  it('builds path with no query for default state', () => {
    expect(buildLibraryPath('acme', emptyState())).toBe('/w/acme/library');
  });

  it('includes non-default category', () => {
    const path = buildLibraryPath('acme', emptyState({ cat: 'person' }));
    expect(path).toBe('/w/acme/library?cat=person');
  });

  it('includes entry and tab', () => {
    const path = buildLibraryPath(
      'acme',
      emptyState({ entry: 'e1', tab: 'draft' })
    );
    expect(path).toContain('entry=e1');
    expect(path).toContain('tab=draft');
  });
});

describe('no scoped link normalizes to unscoped', () => {
  it('scoped resource link preserves resource context', () => {
    const scoped = normalizeLibraryRoute(
      emptyState({
        entry: 'e1',
        tab: 'overview',
        resource_type: 'reference_asset',
        resource: 'ra1',
        parent_type: 'reference_group',
        parent: 'rg1',
      })
    );
    expect(scoped.resource_type).toBe('reference_asset');
    expect(scoped.resource).toBe('ra1');
    expect(scoped.parent_type).toBe('reference_group');
    expect(scoped.parent).toBe('rg1');
  });

  it('resource without parent keeps resource but drops parent', () => {
    const state = normalizeLibraryRoute(
      emptyState({
        entry: 'e1',
        resource_type: 'offer',
        resource: 'o1',
        parent: 'p1',
      })
    );
    expect(state.resource_type).toBe('offer');
    expect(state.resource).toBe('o1');
    expect(state.parent_type).toBeNull();
    expect(state.parent).toBeNull();
  });
});
