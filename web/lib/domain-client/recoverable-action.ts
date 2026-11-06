export interface RecoverableAction {
  actionInstanceId: string;
  operationId: string;
  pathParams: Record<string, unknown>;
  body: Record<string, unknown>;
  createdAt: number;
}

const KEY_PREFIX = 'recoverable:';
const DEFAULT_MAX_AGE_MS = 48 * 60 * 60 * 1000;

export class RecoverableActionStore {
  private readonly storage: Storage;
  private readonly maxAgeMs: number;

  constructor(config: { storage: Storage; maxAgeMs?: number }) {
    this.storage = config.storage;
    this.maxAgeMs = config.maxAgeMs ?? DEFAULT_MAX_AGE_MS;
  }

  save(action: RecoverableAction): void {
    const key = this.storageKey(action.operationId, action.actionInstanceId);
    this.storage.setItem(key, JSON.stringify(action));
  }

  get(operationId: string, actionInstanceId: string): RecoverableAction | null {
    const key = this.storageKey(operationId, actionInstanceId);
    return this.parse(this.storage.getItem(key));
  }

  remove(operationId: string, actionInstanceId: string): void {
    const key = this.storageKey(operationId, actionInstanceId);
    this.storage.removeItem(key);
  }

  listPending(operationId: string): RecoverableAction[] {
    const prefix = `${KEY_PREFIX}${operationId}:`;
    const results: RecoverableAction[] = [];
    for (let i = 0; i < this.storage.length; i++) {
      const storageKey = this.storage.key(i);
      if (storageKey === null || !storageKey.startsWith(prefix)) continue;
      const record = this.parse(this.storage.getItem(storageKey));
      if (record) results.push(record);
    }
    return results;
  }

  recoverOnStartup(onStale: (record: RecoverableAction) => void): void {
    const now = Date.now();
    for (let i = this.storage.length - 1; i >= 0; i--) {
      const storageKey = this.storage.key(i);
      if (storageKey === null || !storageKey.startsWith(KEY_PREFIX)) continue;
      const record = this.parse(this.storage.getItem(storageKey));
      if (!record) {
        this.storage.removeItem(storageKey);
        continue;
      }
      if (now - record.createdAt > this.maxAgeMs) {
        onStale(record);
        this.storage.removeItem(storageKey);
      }
    }
  }

  private storageKey(operationId: string, actionInstanceId: string): string {
    return `${KEY_PREFIX}${operationId}:${actionInstanceId}`;
  }

  private parse(raw: string | null): RecoverableAction | null {
    if (raw === null) return null;
    try {
      return JSON.parse(raw) as RecoverableAction;
    } catch {
      return null;
    }
  }
}
