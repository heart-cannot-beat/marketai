import { IntentTracker } from '../../lib/domain-client/intent';

class MemoryStorage implements Storage {
  private store = new Map<string, string>();
  get length() {
    return this.store.size;
  }
  clear() {
    this.store.clear();
  }
  getItem(key: string) {
    return this.store.get(key) ?? null;
  }
  key(index: number) {
    return Array.from(this.store.keys())[index] ?? null;
  }
  removeItem(key: string) {
    this.store.delete(key);
  }
  setItem(key: string, value: string) {
    this.store.set(key, value);
  }
}

describe('IntentTracker', () => {
  it('acquire returns a new intent for unknown actionInstanceId', () => {
    const storage = new MemoryStorage();
    const tracker = new IntentTracker({ storage });
    const result = tracker.acquire('action_1', 'create_upload_intent');
    expect(result).toHaveProperty('intentId');
    expect(result).not.toHaveProperty('kind');
  });

  it('acquire returns same intent within safe retry window', () => {
    const storage = new MemoryStorage();
    const tracker = new IntentTracker({ storage });
    const first = tracker.acquire('action_1', 'op1');
    const second = tracker.acquire('action_1', 'op1');
    expect('intentId' in first && 'intentId' in second).toBe(true);
    if ('intentId' in first && 'intentId' in second) {
      expect(first.intentId).toBe(second.intentId);
    }
  });

  it('resolve removes the intent', () => {
    const storage = new MemoryStorage();
    const tracker = new IntentTracker({ storage });
    const result = tracker.acquire('action_2', 'op1');
    if ('intentId' in result) {
      tracker.resolve(result);
    }
    const next = tracker.acquire('action_2', 'op1');
    expect('intentId' in next).toBe(true);
    if ('intentId' in result && 'intentId' in next) {
      expect(next.intentId).not.toBe(result.intentId);
    }
  });

  it('constructor recovers crashed inflight intents to retained', () => {
    const storage = new MemoryStorage();
    const stored = {
      intentId: 'old_intent',
      actionInstanceId: 'action_crash',
      createdAt: Date.now() - 10_000,
      safeRetryUntil: Date.now() + 1000,
      localRetentionUntil: Date.now() + 2000,
      status: 'inflight',
    };
    storage.setItem('intent:action_crash', JSON.stringify(stored));

    new IntentTracker({ storage });
    const raw = storage.getItem('intent:action_crash');
    expect(raw).not.toBeNull();
    const recovered = JSON.parse(raw!);
    expect(recovered.status).toBe('retained');
  });

  it('retain() past safeRetryUntil produces ReconciliationRequired on next acquire', () => {
    const storage = new MemoryStorage();
    const tracker = new IntentTracker({ storage });
    tracker.registerPolicy('op1', {
      safeRetryWindowMs: 10,
      localRetentionMs: 1000,
    });
    const first = tracker.acquire('action_3', 'op1');
    if (!('intentId' in first)) throw new Error('expected Intent');
    tracker.retain(first);

    // Wait past the 10ms safeRetryWindowMs.
    const start = Date.now();
    while (Date.now() - start < 20) {
      /* busy-wait a few ms — deterministic without fake timers */
    }

    const second = tracker.acquire('action_3', 'op1');
    expect(second).toHaveProperty('kind', 'reconciliation_required');
  });

  it('dismissReconciliation clears a retained intent so a new one can be acquired', () => {
    const storage = new MemoryStorage();
    const tracker = new IntentTracker({ storage });
    tracker.registerPolicy('op1', {
      safeRetryWindowMs: 10,
      localRetentionMs: 1000,
    });
    const first = tracker.acquire('action_4', 'op1');
    if (!('intentId' in first)) throw new Error('expected Intent');
    tracker.retain(first);

    const start = Date.now();
    while (Date.now() - start < 20) {
      /* busy-wait */
    }

    const reconciliation = tracker.acquire('action_4', 'op1');
    expect(reconciliation).toHaveProperty('kind', 'reconciliation_required');

    tracker.dismissReconciliation('action_4');
    const fresh = tracker.acquire('action_4', 'op1');
    expect('intentId' in fresh).toBe(true);
    if ('intentId' in fresh) {
      expect(fresh.intentId).not.toBe(first.intentId);
    }
  });

  it('refineTimeBounds only narrows, never widens, the time bounds', () => {
    const storage = new MemoryStorage();
    const tracker = new IntentTracker({ storage });
    const intent = tracker.acquire('action_5', 'op1');
    if (!('intentId' in intent)) throw new Error('expected Intent');

    const before = JSON.parse(storage.getItem('intent:action_5')!);

    // Attempt to widen — should be ignored (Math.min keeps the narrower value).
    tracker.refineTimeBounds('action_5', {
      safeRetryUntil: before.safeRetryUntil + 100_000,
      localRetentionUntil: before.localRetentionUntil + 100_000,
    });
    const afterWiden = JSON.parse(storage.getItem('intent:action_5')!);
    expect(afterWiden.safeRetryUntil).toBe(before.safeRetryUntil);
    expect(afterWiden.localRetentionUntil).toBe(before.localRetentionUntil);

    // Narrowing should take effect.
    const narrower = before.safeRetryUntil - 1000;
    tracker.refineTimeBounds('action_5', {
      safeRetryUntil: narrower,
      localRetentionUntil: before.localRetentionUntil - 1000,
    });
    const afterNarrow = JSON.parse(storage.getItem('intent:action_5')!);
    expect(afterNarrow.safeRetryUntil).toBe(narrower);
  });

  it('deduplicateInflight coalesces concurrent calls for the same actionInstanceId', async () => {
    const storage = new MemoryStorage();
    const tracker = new IntentTracker({ storage });
    let calls = 0;
    const work = () =>
      new Promise<string>((resolve) => {
        calls++;
        setTimeout(() => resolve('done'), 5);
      });

    const [a, b] = await Promise.all([
      tracker.deduplicateInflight('action_6', work),
      tracker.deduplicateInflight('action_6', work),
    ]);

    expect(a).toBe('done');
    expect(b).toBe('done');
    expect(calls).toBe(1);
  });

  it('setCommandContext persists and getCommandContext retrieves it', () => {
    const storage = new MemoryStorage();
    const tracker = new IntentTracker({ storage });
    tracker.acquire('action_ctx', 'op1');

    tracker.setCommandContext('action_ctx', {
      commandKind: 'publish',
      target: 'entry_42',
      paramHash: 'abc',
    });

    const ctx = tracker.getCommandContext('action_ctx');
    expect(ctx).toEqual({
      commandKind: 'publish',
      target: 'entry_42',
      paramHash: 'abc',
    });
  });

  it('getCommandContext returns null for unknown actionInstanceId', () => {
    const storage = new MemoryStorage();
    const tracker = new IntentTracker({ storage });
    expect(tracker.getCommandContext('nonexistent')).toBeNull();
  });

  it('hasPendingInflight returns true while a dedup promise is in flight', async () => {
    const storage = new MemoryStorage();
    const tracker = new IntentTracker({ storage });
    let resolver: () => void;
    const work = () =>
      new Promise<void>((resolve) => {
        resolver = resolve;
      });

    const promise = tracker.deduplicateInflight('action_pending', work);
    expect(tracker.hasPendingInflight('action_pending')).toBe(true);

    resolver!();
    await promise;
    expect(tracker.hasPendingInflight('action_pending')).toBe(false);
  });
});
