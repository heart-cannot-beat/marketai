import { execFile } from 'node:child_process';
import path from 'node:path';
import type { MetaScenarioId } from '../artifacts/schemas';

const INSPECT_DB_SCRIPT = path.resolve(__dirname, '../scripts/inspect_db.py');
const PYTHON = process.env.PYTHON_BIN || 'python3';
const TIMEOUT_MS = 15_000;

interface InspectResult {
  ok: boolean;
  data?: unknown;
  error?: string;
}

function runInspectCommand(
  command: string,
  params: Record<string, unknown>
): Promise<InspectResult> {
  return new Promise((resolve) => {
    const child = execFile(
      PYTHON,
      [INSPECT_DB_SCRIPT],
      { timeout: TIMEOUT_MS },
      (err, stdout, stderr) => {
        if (err) {
          resolve({ ok: false, error: stderr || err.message });
          return;
        }
        try {
          resolve(JSON.parse(stdout));
        } catch {
          resolve({ ok: false, error: `Invalid JSON: ${stdout}` });
        }
      }
    );
    child.stdin?.write(JSON.stringify({ command, ...params }));
    child.stdin?.end();
  });
}

export class EvidenceInspector {
  private readonly databaseUrl: string;

  constructor(databaseUrl: string) {
    this.databaseUrl = databaseUrl;
  }

  async assertZeroBusinessEntities(workspaceRef: string): Promise<void> {
    const result = await runInspectCommand('assert_zero_business_entities', {
      workspace_ref: workspaceRef,
      database_url: this.databaseUrl,
    });
    if (!result.ok) {
      throw new Error(
        `Business entities found: ${result.error || JSON.stringify(result.data)}`
      );
    }
  }

  async queryCallEvidence(
    workspaceRef: string,
    filters?: Record<string, unknown>
  ): Promise<unknown> {
    const result = await runInspectCommand('query_call_evidence', {
      workspace_ref: workspaceRef,
      filters: filters || {},
      database_url: this.databaseUrl,
    });
    if (!result.ok) {
      throw new Error(`Call evidence query failed: ${result.error}`);
    }
    return result.data;
  }

  async queryProviderCostLedger(workspaceRef: string): Promise<{
    reservation_count: number;
    settlement_count: number;
    release_count: number;
    classification: 'consistent' | 'inconsistent';
  }> {
    const result = await runInspectCommand('query_provider_cost_ledger', {
      workspace_ref: workspaceRef,
      database_url: this.databaseUrl,
    });
    if (!result.ok) {
      throw new Error(`Cost ledger query failed: ${result.error}`);
    }
    return result.data as {
      reservation_count: number;
      settlement_count: number;
      release_count: number;
      classification: 'consistent' | 'inconsistent';
    };
  }

  async captureCanonicalSnapshot(
    workspaceRef: string
  ): Promise<Record<string, unknown>> {
    const result = await runInspectCommand('capture_canonical_snapshot', {
      workspace_ref: workspaceRef,
      database_url: this.databaseUrl,
    });
    if (!result.ok) {
      throw new Error(`Snapshot capture failed: ${result.error}`);
    }
    return result.data as Record<string, unknown>;
  }

  compareSnapshots(
    before: Record<string, unknown>,
    after: Record<string, unknown>
  ): boolean {
    return JSON.stringify(before) === JSON.stringify(after);
  }

  async assertNoWinnerDecision(
    workspaceRef: string,
    experimentRef: string
  ): Promise<void> {
    const result = await runInspectCommand('assert_no_winner_decision', {
      workspace_ref: workspaceRef,
      experiment_ref: experimentRef,
      database_url: this.databaseUrl,
    });
    if (!result.ok) {
      throw new Error(`Winner decision exists: ${result.error}`);
    }
  }

  async assertZeroMetaWrites(workspaceRef: string): Promise<void> {
    const result = await runInspectCommand('assert_zero_meta_writes', {
      workspace_ref: workspaceRef,
      database_url: this.databaseUrl,
    });
    if (!result.ok) {
      throw new Error(`Meta writes detected: ${result.error}`);
    }
  }

  async assertWorkspaceMetaProvenance(
    workspaceRef: string,
    expectedMode: string
  ): Promise<void> {
    const result = await runInspectCommand('assert_workspace_meta_provenance', {
      workspace_ref: workspaceRef,
      expected_mode: expectedMode,
      database_url: this.databaseUrl,
    });
    if (!result.ok) {
      throw new Error(`Meta provenance mismatch: ${result.error}`);
    }
  }

  async assertNonVacuousMetaEvidence(
    workspaceRef: string,
    opts: {
      expectedProvenance: 'customer_sandbox';
      expectedScenarios: MetaScenarioId[];
      frozenCandidateCount: number;
    }
  ): Promise<unknown> {
    const result = await runInspectCommand('assert_non_vacuous_meta_evidence', {
      workspace_ref: workspaceRef,
      expected_provenance: opts.expectedProvenance,
      expected_scenarios: opts.expectedScenarios,
      frozen_candidate_count: opts.frozenCandidateCount,
      database_url: this.databaseUrl,
    });
    if (!result.ok) {
      throw new Error(
        `Non-vacuous meta evidence check failed: ${result.error}`
      );
    }
    return result.data;
  }

  async assertNoRunnableOrInFlightJobs(workspaceRef: string): Promise<void> {
    const result = await runInspectCommand(
      'assert_no_runnable_or_inflight_jobs',
      {
        workspace_ref: workspaceRef,
        database_url: this.databaseUrl,
      }
    );
    if (!result.ok) {
      throw new Error(`Runnable/in-flight jobs found: ${result.error}`);
    }
  }

  async assertOperationsSettled(workspaceRef: string): Promise<void> {
    const result = await runInspectCommand('assert_operations_settled', {
      workspace_ref: workspaceRef,
      database_url: this.databaseUrl,
    });
    if (!result.ok) {
      throw new Error(`Unsettled operations found: ${result.error}`);
    }
  }

  async readJobLease(runRef: string): Promise<{
    state: string;
    lease_owner: string | null;
    lease_deadline: string | null;
  }> {
    const result = await runInspectCommand('read_job_lease', {
      run_ref: runRef,
      database_url: this.databaseUrl,
    });
    if (!result.ok) {
      throw new Error(`Job lease read failed: ${result.error}`);
    }
    return result.data as {
      state: string;
      lease_owner: string | null;
      lease_deadline: string | null;
    };
  }

  async assertCallEvidenceModeConsistency(workspaceRef: string): Promise<void> {
    const result = await runInspectCommand(
      'assert_call_evidence_mode_consistency',
      {
        workspace_ref: workspaceRef,
        database_url: this.databaseUrl,
      }
    );
    if (!result.ok) {
      throw new Error(`Mode consistency check failed: ${result.error}`);
    }
  }
}
