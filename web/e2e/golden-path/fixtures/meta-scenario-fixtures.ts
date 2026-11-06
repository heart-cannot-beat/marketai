import type {
  EntityRef,
  MetaObservationAdvancedResult,
  MetaScenarioId,
} from '../artifacts/schemas';
import type { OwnerSessionCapability } from './onboarding-harness';

export class MetaScenarioFixture {
  private workspaceSlug: string;

  constructor(workspaceSlug: string) {
    this.workspaceSlug = workspaceSlug;
  }

  setWorkspaceSlug(workspaceSlug: string): void {
    this.workspaceSlug = workspaceSlug;
  }

  async advanceMetaObservation(
    session: OwnerSessionCapability,
    namespace: string,
    opts: {
      scenario: MetaScenarioId;
      experiment?: EntityRef<'experiment'>;
      enrollment?: EntityRef<'scale_enrollment'>;
      candidateRefs?: EntityRef<'asset_version'>[];
    }
  ): Promise<MetaObservationAdvancedResult> {
    const page = session.page();

    const body: Record<string, unknown> = {
      namespace,
      scenario: opts.scenario,
    };
    if (opts.experiment) {
      body.experiment_id = opts.experiment.runtimeId;
    }
    if (opts.enrollment) {
      body.enrollment_id = opts.enrollment.runtimeId;
    }
    if (opts.candidateRefs) {
      body.candidate_ids = opts.candidateRefs.map((r) => r.runtimeId);
    }

    const result = await page.evaluate(
      async (args: { slug: string; body: Record<string, unknown> }) => {
        const res = await fetch(
          `/api/domain/v1/w/${args.slug}/test-support/meta-observation/advance`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(args.body),
          }
        );
        if (!res.ok) {
          throw new Error(
            `Meta observation advance failed: ${res.status}: ${await res.text()}`
          );
        }
        return res.json();
      },
      { slug: this.workspaceSlug, body }
    );

    const typed = result as MetaObservationAdvancedResult;
    if (typed.provenance !== 'customer_sandbox') {
      throw new Error(`Unexpected provenance: ${typed.provenance}`);
    }

    return typed;
  }
}
