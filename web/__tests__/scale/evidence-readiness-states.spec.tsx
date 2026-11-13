/**
 * @jest-environment jsdom
 */
import { render, screen, within } from '@testing-library/react';
import { EvidenceTimeline } from '@/components/scale/detail/EvidenceTimeline';
import { evidenceView } from '@/lib/scale/evidence-view';
import type { EnrollmentEvidence } from '@/hooks/scale/useScaleEvidence';

/**
 * The evidence states contract §22 asks of the Scale page (#114): empty,
 * waiting for the first complete day, first day collected, blocked with its
 * reason and recovery action, stale/blocked over an older Evaluation, a newer
 * day being evaluated, warming. The data is what `GET .../evidence` answers;
 * every value is fictional.
 *
 * `t` echoes the key and its interpolation values, so a test can tell which
 * day a sentence names; the locale guards prove the keys have copy.
 */
jest.mock('next-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (key: string, options?: Record<string, unknown>) => {
      const values = Object.entries(options ?? {})
        .filter(([name]) => name !== 'defaultValue')
        .map(([name, value]) => `${name}=${value}`);
      return values.length
        ? `${ns}:${key}[${values.join(',')}]`
        : `${ns}:${key}`;
    },
    i18n: { language: 'en' },
  }),
}));

const mockUseScaleEnrollmentEvidence = jest.fn();
jest.mock('@/hooks/scale/useScaleEvidence', () => ({
  useScaleEnrollmentEvidence: (...args: unknown[]) =>
    mockUseScaleEnrollmentEvidence(...args),
}));

type Readiness = NonNullable<EnrollmentEvidence['readiness']>;

/** Activated, nothing collected yet. */
const NOTHING: EnrollmentEvidence = {
  enrollment_id: 'senr_acme',
  episode_id: 'sep_acme',
  readiness: null,
  evaluation_stale: false,
  evaluation_id: null,
  snapshot_id: null,
  policy_version_id: null,
  target_version_id: null,
  algorithm_version: null,
  metric_semantic_contract_set_hash: null,
  maturity: null,
  data_quality_state: null,
  health: null,
  health_reason: null,
  healthy_evaluations: null,
  streak_counters: null,
  safe_watermark: null,
  last_complete_local_day: null,
  last_sync_at: null,
  freshness_reference_at: null,
  complete_local_days_in_episode: null,
  active_delivery_days_in_episode: null,
  current_window: null,
  baseline_window: null,
  rolling_window: null,
  quality_issues: null,
  signals: null,
  cursor_as_of_local_day: null,
  cursor_source_revision: null,
  cursor_semantic_event_sequence: null,
  cursor_lineage_generation: null,
  revision: null,
  updated_at: null,
};

/** The collector's projection after a complete manifest for 2026-11-03. */
const COMPLETE: Readiness = {
  manifest_id: 'sesm_day3',
  manifest_state: 'complete',
  source_revision: 3,
  target_local_day: '2026-11-03',
  maturity: 'warming',
  quality_state: 'pass',
  last_complete_local_day: '2026-11-03',
  freshness_reference_at: '2026-11-04T04:10:00Z',
  blocking_issue_count: 0,
  warning_issue_count: 0,
  recovery_action: null,
  issues: [],
  revision: 3,
  updated_at: '2026-11-04T04:10:00Z',
};

/** A blocked manifest for 2026-11-04 after the complete one above. */
const BLOCKED: Readiness = {
  ...COMPLETE,
  manifest_id: 'sesm_day4',
  manifest_state: 'blocked',
  source_revision: 4,
  target_local_day: '2026-11-04',
  maturity: 'insufficient',
  quality_state: 'blocked',
  blocking_issue_count: 1,
  recovery_action: 'wait_for_next_sync',
  issues: [
    {
      code: 'transport_exhausted',
      severity: 'blocking',
      is_blocking: true,
      contract_type: null,
      window_role: null,
      local_day: null,
      segment_key: null,
      detail: 'sync run msr_acme is failed',
      recovery_action: 'wait_for_next_sync',
    },
  ],
  revision: 4,
  updated_at: '2026-11-05T04:10:00Z',
};

/** The accepted Evaluation of 2026-11-03 (source revision 3). */
const EVALUATED: EnrollmentEvidence = {
  ...NOTHING,
  readiness: COMPLETE,
  evaluation_id: 'sev_day3',
  snapshot_id: 'sesn_day3',
  policy_version_id: 'spv_beta',
  algorithm_version: 'beta-v1',
  metric_semantic_contract_set_hash: 'contract-hash',
  maturity: 'monitorable',
  data_quality_state: 'pass',
  health: 'fatigue_watch',
  health_reason: '',
  healthy_evaluations: 0,
  streak_counters: [],
  safe_watermark: '2026-11-03',
  last_complete_local_day: '2026-11-03',
  last_sync_at: '2026-11-04T04:10:00Z',
  freshness_reference_at: '2026-11-04T04:10:00Z',
  complete_local_days_in_episode: 14,
  active_delivery_days_in_episode: 14,
  current_window: {
    role: 'current_7d',
    start_local_day: '2026-10-28',
    end_local_day: '2026-11-04',
  },
  baseline_window: {
    role: 'baseline_7d',
    start_local_day: '2026-10-21',
    end_local_day: '2026-10-28',
  },
  rolling_window: {
    role: 'rolling_14d',
    start_local_day: '2026-10-21',
    end_local_day: '2026-11-04',
  },
  quality_issues: [],
  signals: [],
  cursor_as_of_local_day: '2026-11-03',
  cursor_source_revision: 3,
  cursor_semantic_event_sequence: 0,
  cursor_lineage_generation: 0,
  revision: 3,
  updated_at: '2026-11-04T04:12:00Z',
};

function renderWith(data: EnrollmentEvidence | undefined, problem?: object) {
  mockUseScaleEnrollmentEvidence.mockReturnValue({
    data,
    problem,
    isLoading: false,
    isStale: false,
    refresh: jest.fn(),
  });
  return render(
    <EvidenceTimeline enrollmentId="senr_acme" metaMode="customer_sandbox" />
  );
}

describe('evidenceView', () => {
  it.each([
    ['nothing collected', NOTHING, { kind: 'waiting' }],
    [
      'a complete day, no Evaluation yet',
      { ...NOTHING, readiness: COMPLETE },
      { kind: 'collected', day: '2026-11-03' },
    ],
    [
      'blocked before the first Evaluation',
      { ...NOTHING, readiness: BLOCKED },
      { kind: 'blocked', stale: false },
    ],
    [
      'blocked over an older Evaluation',
      { ...EVALUATED, readiness: BLOCKED, evaluation_stale: true },
      { kind: 'blocked', stale: true },
    ],
    [
      'blocked in the manifest the Evaluation was sealed from',
      { ...EVALUATED, readiness: { ...BLOCKED, source_revision: 3 } },
      { kind: 'blocked', stale: false },
    ],
    [
      'a newer complete day being evaluated',
      {
        ...EVALUATED,
        readiness: { ...COMPLETE, source_revision: 4 },
        evaluation_stale: true,
      },
      { kind: 'evaluated', pending: true },
    ],
    [
      'the current Evaluation',
      EVALUATED,
      { kind: 'evaluated', pending: false },
    ],
    [
      'an Evaluation without a readiness projection',
      { ...EVALUATED, readiness: null },
      { kind: 'evaluated', pending: false },
    ],
  ])('%s', (_name, evidence, expected) => {
    expect(evidenceView(evidence as EnrollmentEvidence)).toEqual(expected);
  });
});

describe('EvidenceTimeline states (contract §22)', () => {
  it('empty: an enrollment without an active episode', () => {
    renderWith(undefined, { status: 404, code: 'no_active_episode' });
    expect(
      screen.getByText('scale:evidence.no-active-episode')
    ).toBeInTheDocument();
  });

  it('collecting: waits for the first complete day, with nothing invented', () => {
    renderWith(NOTHING);
    const notice = screen.getByRole('status');
    expect(notice).toHaveTextContent('scale:evidence.state.waiting-title');
    expect(notice).toHaveTextContent('scale:evidence.state.waiting-body');
    // No maturity, health, quality, window or version before any evidence.
    expect(screen.queryByText(/scale:maturity\./)).toBeNull();
    expect(screen.queryByText(/scale:health\./)).toBeNull();
    expect(screen.queryByText(/scale:evidence\.quality\./)).toBeNull();
    expect(screen.queryByText(/→/)).toBeNull();
    expect(screen.queryByText('scale:evidence.policy-version')).toBeNull();
  });

  it('collecting: the first complete day is in and its evaluation runs', () => {
    renderWith({ ...NOTHING, readiness: COMPLETE });
    expect(screen.getByRole('status')).toHaveTextContent(
      'scale:evidence.state.collected-body[day=2026-11-03]'
    );
    expect(screen.getByText('scale:maturity.warming')).toBeInTheDocument();
    expect(screen.getByText('scale:evidence.quality.pass')).toBeInTheDocument();
    expect(screen.queryByText(/scale:health\./)).toBeNull();
  });

  it('blocked: says why and what to do, before any evaluation', () => {
    renderWith({ ...NOTHING, readiness: BLOCKED });
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('scale:evidence.state.blocked-title');
    expect(alert).toHaveTextContent(
      'scale:evidence.state.blocked-body[day=2026-11-04]'
    );
    expect(alert).toHaveTextContent(
      'scale:evidence.state.last-complete-day[day=2026-11-03]'
    );
    expect(alert).toHaveTextContent(
      'scale:evidence.state.what-to-do scale:evidence.recovery.wait_for_next_sync'
    );
    const issue = within(alert)
      .getByText('scale:evidence.source-issue.transport_exhausted')
      .closest('li')!;
    expect(
      within(issue).getByText('scale:evidence.quality-severity.blocking')
    ).toBeInTheDocument();
    const technical = within(issue)
      .getByText('scale:evidence.technical-details')
      .closest('details')!;
    expect(technical).not.toHaveAttribute('open');
    expect(technical).toHaveTextContent('transport_exhausted');
    expect(technical).toHaveTextContent('sync run msr_acme is failed');
    expect(screen.getByText('scale:maturity.insufficient')).toBeInTheDocument();
    expect(
      screen.getByText('scale:evidence.quality.blocked')
    ).toBeInTheDocument();
    expect(screen.queryByText(/scale:health\./)).toBeNull();
    // Raw codes stay in the folded technical details, never the sentence.
    for (const raw of screen.queryAllByText(
      /^(transport_exhausted|wait_for_next_sync)$/
    ))
      expect(technical).toContainElement(raw);
  });

  it('stale/blocked: the older evaluation is history, with no current health', () => {
    renderWith({ ...EVALUATED, readiness: BLOCKED, evaluation_stale: true });
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent(
      'scale:evidence.state.stale-body[day=2026-11-03]'
    );
    expect(alert).toHaveTextContent(
      'scale:evidence.source-issue.transport_exhausted'
    );
    // Contract §5: no deterministic health while data quality is blocked.
    expect(screen.queryByText('scale:health.fatigue_watch')).toBeNull();
    expect(screen.queryByText('scale:maturity.monitorable')).toBeNull();
    expect(screen.getByText('scale:maturity.insufficient')).toBeInTheDocument();
    // The older evaluation stays readable, labelled with its day.
    expect(
      screen.getByText('scale:evidence.last-evaluation[day=2026-11-03]')
    ).toBeInTheDocument();
    expect(screen.getByText('2026-10-28 → 2026-11-04')).toBeInTheDocument();
  });

  it('a newer day being evaluated: says so over the current evaluation', () => {
    renderWith({
      ...EVALUATED,
      readiness: {
        ...COMPLETE,
        manifest_id: 'sesm_day4',
        source_revision: 4,
        target_local_day: '2026-11-04',
        last_complete_local_day: '2026-11-04',
      },
      evaluation_stale: true,
    });
    expect(screen.getByRole('status')).toHaveTextContent(
      'scale:evidence.state.pending-body[day=2026-11-04,evaluated=2026-11-03]'
    );
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText('scale:health.fatigue_watch')).toBeInTheDocument();
  });

  it('warming: the evaluation says how far the Episode is', () => {
    renderWith({
      ...EVALUATED,
      maturity: 'warming',
      health: null,
      health_reason: 'episode_window_incomplete',
      complete_local_days_in_episode: 5,
    });
    expect(screen.getByText('scale:maturity.warming')).toBeInTheDocument();
    expect(
      screen.getByText(
        'scale:evidence.health-reason.episode_window_incomplete[days=5]'
      )
    ).toBeInTheDocument();
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('current: an up-to-date evaluation needs no notice', () => {
    renderWith(EVALUATED);
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText('scale:health.fatigue_watch')).toBeInTheDocument();
    expect(screen.queryByText(/scale:evidence\.last-evaluation/)).toBeNull();
  });
});
