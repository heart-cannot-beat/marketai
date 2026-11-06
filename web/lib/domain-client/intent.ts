/**
 * `IntentTracker` — browser-only, persistent user-action identity (spec
 * §6.10). Generates and persists the `intentId` the `DomainGateway` sends as
 * `X-Intent-Id` on non-safe requests; the BFF derives the FastAPI
 * `Idempotency-Key` from `SHA-256(operationId:intentId)`
 * (`deriveIdempotencyKey` in `request-pipeline.ts`).
 *
 * Persisted in an injected `Storage` (production: `window.localStorage`) so
 * an `intentId` survives a page reload — a refreshed form recovers the same
 * unresolved intent instead of getting a new one that would double-submit
 * the underlying command.
 */

export type Intent = {
  intentId: string;
  actionInstanceId: string;
};

export type ReconciliationRequired = {
  kind: 'reconciliation_required';
  actionInstanceId: string;
  staleSince: number;
};

export type CommandContext = {
  commandKind: string;
  target?: string;
  paramHash?: string;
};

type StoredIntent = {
  intentId: string;
  actionInstanceId: string;
  createdAt: number;
  safeRetryUntil: number;
  localRetentionUntil: number;
  status: 'inflight' | 'retained';
  commandContext?: CommandContext;
};

export type OperationPolicy = {
  safeRetryWindowMs: number;
  localRetentionMs: number;
};

// Matches MARKETAI_IDEMPOTENCY_RETENTION_SECONDS' default (24h) — the browser
// must never retry past the point the server has forgotten the idempotency
// record.
const DEFAULT_SAFE_RETRY_MS = 24 * 60 * 60 * 1000;
// Reconciliation window: longer than the safe-retry window so a retained,
// ambiguous-outcome intent stays visible to the user for follow-up.
const DEFAULT_RETENTION_MS = 48 * 60 * 60 * 1000;

/** UUID when available, a short random fallback otherwise — mirrors `newTraceId()` in `gateway.ts`. */
function newIntentId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `intent_${Math.random().toString(36).slice(2)}`;
}

export class IntentTracker {
  private readonly storage: Storage;
  private readonly inflightPromises: Map<string, Promise<unknown>> = new Map();
  private readonly operationPolicies: Map<string, OperationPolicy> = new Map();

  constructor(config: { storage: Storage }) {
    this.storage = config.storage;
    this.recoverCrashedInflight();
  }

  /** Called at application startup for each operation that uses intents — `operationId` matches the OpenAPI operation ID, e.g. `"create_upload_intent"`. */
  registerPolicy(operationId: string, policy: OperationPolicy): void {
    this.operationPolicies.set(operationId, policy);
  }

  /**
   * Returns the persisted intent for `actionInstanceId`, creating one on
   * first call. `operationId` selects which registered policy sets the
   * intent's time bounds — it is not part of the persisted lookup key.
   */
  acquire(
    actionInstanceId: string,
    operationId: string
  ): Intent | ReconciliationRequired {
    this.cleanupExpired();

    const key = this.storageKey(actionInstanceId);
    const raw = this.storage.getItem(key);

    if (raw !== null) {
      const stored: StoredIntent = JSON.parse(raw);
      const now = Date.now();

      if (stored.status === 'retained' && now > stored.safeRetryUntil) {
        // Past the safe-retry window with an unresolved intent → always
        // reconciliation, regardless of localRetentionUntil. Retained
        // intents are NEVER silently deleted — they stay until explicitly
        // resolved (resolve()) or the user starts over
        // (dismissReconciliation()).
        return {
          kind: 'reconciliation_required',
          actionInstanceId,
          staleSince: stored.safeRetryUntil,
        };
      }

      // Within the safe-retry window, or still inflight — return the same
      // intent so a retried request replays idempotently.
      return {
        intentId: stored.intentId,
        actionInstanceId: stored.actionInstanceId,
      };
    }

    // Create a new intent — time bounds come from the operation policy
    // BEFORE the first send, so a slow/lost response can't retroactively
    // widen the retry window.
    const policy = this.operationPolicies.get(operationId);
    const now = Date.now();
    const intent: StoredIntent = {
      intentId: newIntentId(),
      actionInstanceId,
      createdAt: now,
      safeRetryUntil:
        now + (policy?.safeRetryWindowMs ?? DEFAULT_SAFE_RETRY_MS),
      localRetentionUntil:
        now + (policy?.localRetentionMs ?? DEFAULT_RETENTION_MS),
      status: 'inflight',
    };

    this.storage.setItem(key, JSON.stringify(intent));

    return {
      intentId: intent.intentId,
      actionInstanceId: intent.actionInstanceId,
    };
  }

  /** Marks the action's intent as done — the next `acquire()` for the same `actionInstanceId` starts a fresh intent. */
  resolve(intent: Intent): void {
    this.storage.removeItem(this.storageKey(intent.actionInstanceId));
    this.inflightPromises.delete(intent.actionInstanceId);
  }

  /** Marks the intent's outcome as ambiguous (e.g. a network error with no response) without discarding it — a later `acquire()` past `safeRetryUntil` returns `ReconciliationRequired` instead of silently retrying forever. */
  retain(intent: Intent): void {
    const key = this.storageKey(intent.actionInstanceId);
    const raw = this.storage.getItem(key);
    if (raw !== null) {
      const stored: StoredIntent = JSON.parse(raw);
      stored.status = 'retained';
      this.storage.setItem(key, JSON.stringify(stored));
    }
    this.inflightPromises.delete(intent.actionInstanceId);
  }

  /** Clears a retained intent once the user has acknowledged the reconciliation warning, allowing a new action. */
  dismissReconciliation(actionInstanceId: string): void {
    this.storage.removeItem(this.storageKey(actionInstanceId));
  }

  /** Attaches command context (kind, target, paramHash) to a persisted intent so higher-level hooks can recover reconciliation metadata for command-outcome lookup. */
  setCommandContext(actionInstanceId: string, context: CommandContext): void {
    const key = this.storageKey(actionInstanceId);
    const raw = this.storage.getItem(key);
    if (raw !== null) {
      const stored: StoredIntent = JSON.parse(raw);
      stored.commandContext = context;
      this.storage.setItem(key, JSON.stringify(stored));
    }
  }

  /** Returns the command context attached to a persisted intent, or `null` if none exists. */
  getCommandContext(actionInstanceId: string): CommandContext | null {
    const key = this.storageKey(actionInstanceId);
    const raw = this.storage.getItem(key);
    if (raw === null) return null;
    const stored: StoredIntent = JSON.parse(raw);
    return stored.commandContext ?? null;
  }

  /** Returns `true` if the given `actionInstanceId` has a deduplication Promise still in flight. */
  hasPendingInflight(actionInstanceId: string): boolean {
    return this.inflightPromises.has(actionInstanceId);
  }

  /** Narrows (never widens — `Math.min`) an intent's time bounds, e.g. from a server-echoed expiry. */
  refineTimeBounds(
    actionInstanceId: string,
    bounds: { safeRetryUntil: number; localRetentionUntil: number }
  ): void {
    const key = this.storageKey(actionInstanceId);
    const raw = this.storage.getItem(key);
    if (raw !== null) {
      const stored: StoredIntent = JSON.parse(raw);
      stored.safeRetryUntil = Math.min(
        stored.safeRetryUntil,
        bounds.safeRetryUntil
      );
      stored.localRetentionUntil = Math.min(
        stored.localRetentionUntil,
        bounds.localRetentionUntil
      );
      this.storage.setItem(key, JSON.stringify(stored));
    }
  }

  /** Coalesces concurrent callers for the same `actionInstanceId` into a single in-flight promise — a double-click doesn't send a second request. */
  deduplicateInflight<T>(
    actionInstanceId: string,
    work: () => Promise<T>
  ): Promise<T> {
    const existing = this.inflightPromises.get(actionInstanceId);
    if (existing !== undefined) {
      return existing as Promise<T>;
    }

    const promise = work().finally(() => {
      this.inflightPromises.delete(actionInstanceId);
    });

    this.inflightPromises.set(actionInstanceId, promise);
    return promise;
  }

  /** A persisted `"inflight"` intent means the page crashed or reloaded before the response arrived — the outcome is ambiguous, so it is transitioned to `"retained"` rather than treated as safely retryable indefinitely. */
  private recoverCrashedInflight(): void {
    for (let i = 0; i < this.storage.length; i++) {
      const storageKey = this.storage.key(i);
      if (storageKey === null || !storageKey.startsWith('intent:')) continue;
      try {
        const stored: StoredIntent = JSON.parse(
          this.storage.getItem(storageKey) || ''
        );
        if (stored.status === 'inflight') {
          stored.status = 'retained';
          this.storage.setItem(storageKey, JSON.stringify(stored));
        }
      } catch {
        this.storage.removeItem(storageKey);
      }
    }
  }

  private cleanupExpired(): void {
    const now = Date.now();
    for (let i = 0; i < this.storage.length; i++) {
      const storageKey = this.storage.key(i);
      if (storageKey === null || !storageKey.startsWith('intent:')) continue;
      try {
        const stored: StoredIntent = JSON.parse(
          this.storage.getItem(storageKey) || ''
        );
        // Only non-retained intents are removed on TTL. Retained intents are
        // NEVER silently deleted — they surface ReconciliationRequired on
        // the next acquire() and persist until resolve() or
        // dismissReconciliation().
        if (
          stored.status !== 'retained' &&
          now > stored.localRetentionUntil &&
          stored.localRetentionUntil > 0
        ) {
          this.storage.removeItem(storageKey);
        }
      } catch {
        this.storage.removeItem(storageKey);
      }
    }
  }

  private storageKey(actionInstanceId: string): string {
    return `intent:${actionInstanceId}`;
  }
}
