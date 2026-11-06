import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

import type {
  Area,
  AssertionRecord,
  CompletionStatus,
  ConfigAttestation,
  EntityKind,
  GoldenPathReport,
  StepOutcome,
} from './schemas';

export interface RecordedStep {
  step: number;
  label: string;
  outcome: StepOutcome;
  duration_ms: number;
  failure_code: string | null;
  assertions: AssertionRecord[];
}

export interface EntityAlias {
  alias: string;
  kind: EntityKind;
  area: Area;
}

export class EvidenceRecorder {
  private readonly runKey: Buffer;
  private readonly runId: string;
  private readonly commitSha: string;
  private readonly startedAt: string;
  private readonly outputDir: string;

  private steps: RecordedStep[] = [];
  private configAttestations: ConfigAttestation[] = [];
  private entityAliases = new Map<string, { kind: EntityKind; area: Area }>();
  private lastCompletedStep = -1;
  private completionStatus: CompletionStatus = 'interrupted';
  private failureCode: string | null = null;

  constructor(opts: { runId: string; commitSha: string; outputDir: string }) {
    this.runId = opts.runId;
    this.commitSha = opts.commitSha;
    this.outputDir = opts.outputDir;
    this.startedAt = new Date().toISOString();
    this.runKey = crypto.randomBytes(32);

    this.writeInterruptedReport();
  }

  opaqueAlias(kind: EntityKind, rawId: string): string {
    const hmac = crypto
      .createHmac('sha256', this.runKey)
      .update(kind + rawId)
      .digest('hex');
    this.entityAliases.set(hmac, { kind, area: this.kindToArea(kind) });
    return hmac;
  }

  recordStepStart(step: number, label: string): StepTimer {
    return new StepTimer(this, step, label);
  }

  recordStepResult(record: RecordedStep): void {
    this.steps.push(record);
    if (record.outcome !== 'skipped') {
      this.lastCompletedStep = record.step;
    }
  }

  recordConfigAttestation(attestation: ConfigAttestation): void {
    this.configAttestations.push(attestation);
  }

  markPassed(): void {
    this.completionStatus = 'passed';
  }

  markFailed(failureCode: string): void {
    this.completionStatus = 'failed';
    this.failureCode = failureCode;
  }

  finalize(): GoldenPathReport {
    const report: GoldenPathReport = {
      schema_version: '1.0',
      run_id: this.runId,
      commit_sha: this.commitSha,
      started_at: this.startedAt,
      completed_at: new Date().toISOString(),
      completion_status: this.completionStatus,
      last_completed_step: this.lastCompletedStep,
      failure_code: this.failureCode,
      steps: this.steps,
      config_attestations: this.configAttestations,
    };

    this.writeReportAtomically(report);
    return report;
  }

  getEntityAliases(): Record<string, { kind: EntityKind; area: Area }> {
    return Object.fromEntries(this.entityAliases);
  }

  getRunId(): string {
    return this.runId;
  }

  private writeInterruptedReport(): void {
    const report: GoldenPathReport = {
      schema_version: '1.0',
      run_id: this.runId,
      commit_sha: this.commitSha,
      started_at: this.startedAt,
      completed_at: this.startedAt,
      completion_status: 'interrupted',
      last_completed_step: -1,
      failure_code: null,
      steps: [],
      config_attestations: [],
    };
    this.writeReportAtomically(report);
  }

  private writeReportAtomically(report: GoldenPathReport): void {
    fs.mkdirSync(this.outputDir, { recursive: true });

    const json = JSON.stringify(report, null, 2);
    const parsed = JSON.parse(json);
    if (parsed.schema_version !== '1.0') {
      throw new Error('Report schema validation failed');
    }

    const tmpPath = path.join(
      this.outputDir,
      `.report-${crypto.randomBytes(4).toString('hex')}.tmp`
    );
    const finalPath = path.join(this.outputDir, 'golden-path-report.json');

    fs.writeFileSync(tmpPath, json);

    const written = fs.readFileSync(tmpPath, 'utf8');
    JSON.parse(written);

    fs.renameSync(tmpPath, finalPath);
  }

  private kindToArea(kind: EntityKind): Area {
    const mapping: Record<EntityKind, Area> = {
      product: 'library',
      product_version: 'library',
      offer_version: 'library',
      persona_version: 'library',
      template_version: 'library',
      prompt_policy_version: 'library',
      data_source_version: 'library',
      generation_run: 'generation',
      asset_version: 'assets',
      experiment: 'experiments',
      winner_decision: 'experiments',
      campaign: 'experiments',
      ad_set: 'experiments',
      ad: 'experiments',
      localization_batch: 'localization',
      scale_enrollment: 'scale',
      advice_thread: 'scale',
      generation_draft: 'scale',
    };
    return mapping[kind];
  }
}

export class StepTimer {
  private readonly recorder: EvidenceRecorder;
  private readonly step: number;
  private readonly label: string;
  private readonly startMs: number;
  private readonly assertions: AssertionRecord[] = [];

  constructor(recorder: EvidenceRecorder, step: number, label: string) {
    this.recorder = recorder;
    this.step = step;
    this.label = label;
    this.startMs = Date.now();
  }

  addAssertion(assertion: AssertionRecord): void {
    this.assertions.push(assertion);
  }

  pass(): void {
    this.recorder.recordStepResult({
      step: this.step,
      label: this.label,
      outcome: 'passed',
      duration_ms: Date.now() - this.startMs,
      failure_code: null,
      assertions: this.assertions,
    });
  }

  fail(failureCode: string): void {
    this.recorder.recordStepResult({
      step: this.step,
      label: this.label,
      outcome: 'failed',
      duration_ms: Date.now() - this.startMs,
      failure_code: failureCode,
      assertions: this.assertions,
    });
  }

  skip(): void {
    this.recorder.recordStepResult({
      step: this.step,
      label: this.label,
      outcome: 'skipped',
      duration_ms: Date.now() - this.startMs,
      failure_code: null,
      assertions: this.assertions,
    });
  }
}
