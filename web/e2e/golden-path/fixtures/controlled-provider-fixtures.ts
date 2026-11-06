import { execFile } from 'node:child_process';
import path from 'node:path';
import type {
  BarrierRef,
  EntityRef,
  RequestAttemptSummary,
  SanitizedEffectSummary,
  TerminalCheckpointState,
} from '../artifacts/schemas';

const SIDECAR_URL =
  process.env.CONTROLLED_PROVIDER_URL || 'http://controlled-provider:9090';
const CHECKPOINT_SCRIPT = path.resolve(
  __dirname,
  '../scripts/wait_adapter_checkpoint.py'
);
const PYTHON = process.env.PYTHON_BIN || 'python3';
const CHECKPOINT_TIMEOUT_MS = 60_000;

export class ProviderControlFixture {
  private readonly baseUrl: string;

  constructor(baseUrl?: string) {
    this.baseUrl = baseUrl || SIDECAR_URL;
  }

  async registerBarrier(
    namespace: string,
    opts: {
      runRef: EntityRef<'generation_run'>;
      matchOrdinal: number;
    }
  ): Promise<BarrierRef> {
    const barrierName = 'before-sigkill';
    const response = await fetch(
      `${this.baseUrl}/test/runs/${namespace}/barriers`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          // The sidecar checkpoint API names barriers explicitly and uses
          // camelCase fields (the worker-facing run id is carried by the
          // namespace/run ledger, not by this control endpoint).
          name: barrierName,
          matchOrdinal: opts.matchOrdinal,
        }),
      }
    );

    if (!response.ok) {
      throw new Error(`Failed to register barrier: ${response.status}`);
    }

    const data = (await response.json()) as { name: string };
    return { name: data.name, namespace };
  }

  async awaitCheckpoint(
    namespace: string,
    barrierName: string,
    opts: { expected: TerminalCheckpointState }
  ): Promise<void> {
    const result = await new Promise<{
      ok: boolean;
      state?: TerminalCheckpointState;
      error?: string;
    }>((resolve) => {
      execFile(
        PYTHON,
        [
          CHECKPOINT_SCRIPT,
          '--url',
          `${this.baseUrl}/test/runs/${namespace}/checkpoints/${barrierName}`,
          '--timeout',
          String(CHECKPOINT_TIMEOUT_MS / 1000),
        ],
        { timeout: CHECKPOINT_TIMEOUT_MS + 5000 },
        (err, stdout) => {
          if (err) {
            resolve({
              ok: false,
              error: `Checkpoint poll failed: exit ${err.code}`,
            });
            return;
          }
          try {
            const parsed = JSON.parse(stdout.trim());
            resolve({ ok: true, state: parsed });
          } catch {
            resolve({ ok: false, error: 'Invalid checkpoint JSON' });
          }
        }
      );
    });

    if (!result.ok) {
      throw new Error(result.error || 'Checkpoint failed');
    }

    const state = result.state!;
    if (
      state.phase !== opts.expected.phase ||
      state.remote_effect_committed !== opts.expected.remote_effect_committed ||
      state.response_received_by_adapter !==
        opts.expected.response_received_by_adapter ||
      state.worker_barrier_waiting !== opts.expected.worker_barrier_waiting
    ) {
      throw new Error(`Checkpoint state mismatch: ${JSON.stringify(state)}`);
    }
  }

  async releaseBarrier(namespace: string, barrierName: string): Promise<void> {
    const response = await fetch(
      `${this.baseUrl}/test/runs/${namespace}/barriers/${barrierName}`,
      { method: 'DELETE' }
    );
    if (!response.ok && response.status !== 404) {
      throw new Error(`Failed to release barrier: ${response.status}`);
    }
  }

  async inspectEffects(
    namespace: string,
    opts: { limit: number }
  ): Promise<SanitizedEffectSummary[]> {
    const response = await fetch(
      `${this.baseUrl}/test/runs/${namespace}/effects?limit=${opts.limit}`
    );

    if (!response.ok) {
      throw new Error(`Failed to inspect effects: ${response.status}`);
    }

    const data = (await response.json()) as {
      effects: SanitizedEffectSummary[];
    };
    return data.effects;
  }

  async inspectRequestAttempts(
    namespace: string,
    opts: {
      idempotencyKeyHash: string;
      requestKind: 'generate' | 'reconcile';
      limit: number;
    }
  ): Promise<RequestAttemptSummary[]> {
    const params = new URLSearchParams({
      idempotency_key_hash: opts.idempotencyKeyHash,
      request_kind: opts.requestKind,
      limit: String(opts.limit),
    });
    const response = await fetch(
      `${this.baseUrl}/test/runs/${namespace}/request_attempts?${params}`
    );

    if (!response.ok) {
      throw new Error(`Failed to inspect request attempts: ${response.status}`);
    }

    const data = (await response.json()) as {
      attempts: RequestAttemptSummary[];
    };
    return data.attempts;
  }

  async cleanup(namespace: string): Promise<void> {
    try {
      const effectsRes = await fetch(
        `${this.baseUrl}/test/runs/${namespace}/effects?limit=100`
      );
      if (effectsRes.ok) {
        const { effects } = (await effectsRes.json()) as {
          effects: { name?: string }[];
        };
        for (const e of effects) {
          if (e.name) {
            await this.releaseBarrier(namespace, e.name).catch(() => {});
          }
        }
      }
    } catch {
      /* cleanup is best-effort */
    }
  }
}
