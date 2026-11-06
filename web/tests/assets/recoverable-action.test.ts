import { RecoverableActionStore } from '@/lib/domain-client/recoverable-action';
import type { RecoverableAction } from '@/lib/domain-client/recoverable-action';

function makeStorage(): Storage {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => store.set(key, value),
    removeItem: (key: string) => store.delete(key),
    clear: () => store.clear(),
    get length() {
      return store.size;
    },
    key: (index: number) => Array.from(store.keys())[index] ?? null,
  };
}

describe('RecoverableActionStore', () => {
  it('saves and retrieves a pending action', () => {
    const storage = makeStorage();
    const store = new RecoverableActionStore({ storage });

    const action: RecoverableAction = {
      actionInstanceId: 'aid-1',
      operationId: 'create_generation_asset_review',
      pathParams: { version_id: 'v1' },
      body: { review_value: 'approved' },
      createdAt: Date.now(),
    };

    store.save(action);

    const retrieved = store.get('create_generation_asset_review', 'aid-1');
    expect(retrieved).toEqual(action);
  });

  it('returns null for a non-existent action', () => {
    const storage = makeStorage();
    const store = new RecoverableActionStore({ storage });

    expect(
      store.get('create_generation_asset_review', 'aid-missing')
    ).toBeNull();
  });

  it('removes a pending action', () => {
    const storage = makeStorage();
    const store = new RecoverableActionStore({ storage });

    store.save({
      actionInstanceId: 'aid-1',
      operationId: 'create_generation_asset_review',
      pathParams: {},
      body: {},
      createdAt: Date.now(),
    });

    store.remove('create_generation_asset_review', 'aid-1');
    expect(store.get('create_generation_asset_review', 'aid-1')).toBeNull();
  });

  it('lists all pending actions for an operation', () => {
    const storage = makeStorage();
    const store = new RecoverableActionStore({ storage });

    store.save({
      actionInstanceId: 'aid-1',
      operationId: 'create_generation_asset_review',
      pathParams: {},
      body: { review_value: 'approved' },
      createdAt: 1000,
    });
    store.save({
      actionInstanceId: 'aid-2',
      operationId: 'create_generation_asset_review',
      pathParams: {},
      body: { review_value: 'needs_changes' },
      createdAt: 2000,
    });
    store.save({
      actionInstanceId: 'aid-3',
      operationId: 'create_generation_export_package',
      pathParams: {},
      body: {},
      createdAt: 3000,
    });

    const reviewActions = store.listPending('create_generation_asset_review');
    expect(reviewActions).toHaveLength(2);
    expect(reviewActions.map((a) => a.actionInstanceId)).toEqual([
      'aid-1',
      'aid-2',
    ]);
  });

  it('cleans up expired actions on startup', () => {
    const storage = makeStorage();
    const now = Date.now();

    const store1 = new RecoverableActionStore({ storage, maxAgeMs: 1000 });
    store1.save({
      actionInstanceId: 'expired',
      operationId: 'create_generation_asset_review',
      pathParams: {},
      body: {},
      createdAt: now - 5000,
    });
    store1.save({
      actionInstanceId: 'fresh',
      operationId: 'create_generation_asset_review',
      pathParams: {},
      body: {},
      createdAt: now - 500,
    });

    const staleRecords: RecoverableAction[] = [];
    const store2 = new RecoverableActionStore({ storage, maxAgeMs: 1000 });
    store2.recoverOnStartup((record) => {
      staleRecords.push(record);
    });

    expect(staleRecords).toHaveLength(1);
    expect(staleRecords[0].actionInstanceId).toBe('expired');
    expect(store2.get('create_generation_asset_review', 'expired')).toBeNull();
  });

  it('survives corrupted storage entries', () => {
    const storage = makeStorage();
    storage.setItem(
      'recoverable:create_generation_asset_review:corrupt',
      'not-json'
    );

    const store = new RecoverableActionStore({ storage });
    const actions = store.listPending('create_generation_asset_review');
    expect(actions).toHaveLength(0);
  });
});
