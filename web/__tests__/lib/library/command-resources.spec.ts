import {
  getInvalidationSet,
  shouldInvalidate,
  type LibraryResource,
} from '@/lib/library/command-resources';
import type { LibraryCommandKind } from '@/hooks/library/command-definitions';

const ALL_RESOURCES: LibraryResource[] = [
  'entry',
  'entryList',
  'versions',
  'events',
  'impact',
  'bindings',
  'entryTypes',
  'internalEntries',
];

describe('command-resources', () => {
  it('publish invalidates entry, entryList, versions, events, impact, bindings', () => {
    const set = getInvalidationSet('publish');
    expect(set).toEqual(
      new Set([
        'entry',
        'entryList',
        'versions',
        'events',
        'impact',
        'bindings',
      ])
    );
  });

  it('withdraw invalidates the same resources as publish', () => {
    const publish = getInvalidationSet('publish');
    const withdraw = getInvalidationSet('withdraw');
    expect(withdraw).toEqual(publish);
  });

  it('archive invalidates entry, entryList, events', () => {
    const set = getInvalidationSet('archive');
    expect(set).toEqual(new Set(['entry', 'entryList', 'events']));
  });

  it('bind_reference invalidates entry, bindings, events', () => {
    const set = getInvalidationSet('bind_reference');
    expect(set).toEqual(new Set(['entry', 'bindings', 'events']));
  });

  it('fork invalidates entryList, versions, events, entryTypes', () => {
    const set = getInvalidationSet('fork');
    expect(set).toEqual(
      new Set(['entryList', 'versions', 'events', 'entryTypes'])
    );
  });

  it('clone invalidates entryList, entryTypes', () => {
    const set = getInvalidationSet('clone');
    expect(set).toEqual(new Set(['entryList', 'entryTypes']));
  });

  it('apply_template invalidates entryList, internalEntries, events, entryTypes', () => {
    const set = getInvalidationSet('apply_template');
    expect(set).toEqual(
      new Set(['entryList', 'internalEntries', 'events', 'entryTypes'])
    );
  });

  it('create_entry invalidates entryList, entryTypes', () => {
    const set = getInvalidationSet('create_entry');
    expect(set).toEqual(new Set(['entryList', 'entryTypes']));
  });

  it('create_internal_entry invalidates both entry listings and entryTypes', () => {
    const set = getInvalidationSet('create_internal_entry');
    expect(set).toEqual(
      new Set(['entryList', 'internalEntries', 'entryTypes'])
    );
  });

  it('save_draft invalidates entry, events', () => {
    const set = getInvalidationSet('save_draft');
    expect(set).toEqual(new Set(['entry', 'events']));
  });

  it('validate_draft invalidates entry only', () => {
    const set = getInvalidationSet('validate_draft');
    expect(set).toEqual(new Set(['entry']));
  });

  it('create_reference_object invalidates entry, bindings, events', () => {
    const set = getInvalidationSet('create_reference_object');
    expect(set).toEqual(new Set(['entry', 'bindings', 'events']));
  });

  it('create_reference_download invalidates nothing', () => {
    const set = getInvalidationSet('create_reference_download');
    expect(set.size).toBe(0);
  });

  it('shouldInvalidate returns true for included resources', () => {
    expect(shouldInvalidate('publish', 'entry')).toBe(true);
    expect(shouldInvalidate('publish', 'impact')).toBe(true);
  });

  it('shouldInvalidate returns false for excluded resources', () => {
    expect(shouldInvalidate('publish', 'internalEntries')).toBe(false);
    expect(shouldInvalidate('clone', 'entry')).toBe(false);
  });

  it('every command kind has a defined invalidation set', () => {
    const allKinds: LibraryCommandKind[] = [
      'create_entry',
      'create_internal_entry',
      'save_draft',
      'validate_draft',
      'publish',
      'archive',
      'bind_reference',
      'withdraw',
      'fork',
      'clone',
      'apply_template',
      'create_reference_object',
      'create_reference_download',
    ];

    for (const kind of allKinds) {
      const set = getInvalidationSet(kind);
      expect(set).toBeDefined();
      expect(set).toBeInstanceOf(Set);
    }
  });

  it('no invalidation set contains a resource outside the known set', () => {
    const allKinds: LibraryCommandKind[] = [
      'create_entry',
      'create_internal_entry',
      'save_draft',
      'validate_draft',
      'publish',
      'archive',
      'bind_reference',
      'withdraw',
      'fork',
      'clone',
      'apply_template',
      'create_reference_object',
      'create_reference_download',
    ];
    const knownResources = new Set<string>(ALL_RESOURCES);

    for (const kind of allKinds) {
      for (const resource of Array.from(getInvalidationSet(kind))) {
        expect(knownResources.has(resource)).toBe(true);
      }
    }
  });
});
