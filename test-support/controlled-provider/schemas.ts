import crypto from 'node:crypto';

export function hashKey(key: string): string {
  return crypto.createHash('sha256').update(key).digest('hex');
}

export function fingerprint(body: Record<string, unknown>): string {
  const canonical = JSON.stringify(body, Object.keys(body).sort());
  return crypto.createHash('sha256').update(canonical).digest('hex').slice(0, 16);
}

export interface GenerateRequest {
  namespace: string;
  idempotency_key: string;
  run_item?: string;
  effect_kind?: string;
  payload: Record<string, unknown>;
}

export interface ReconcileRequest {
  namespace: string;
  idempotency_key: string;
}

export interface EffectRow {
  id: number;
  namespace: string;
  idempotency_key: string;
  request_fingerprint: string;
  run_item: string | null;
  effect_kind: string | null;
  effect_data: string | null;
  success: number;
  created_at: string;
}

export interface RequestAttemptRow {
  sequence: number;
  namespace: string;
  idempotency_key_hash: string;
  request_kind: string;
  request_fingerprint: string;
  received_at: string;
}
