/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { EvidenceTimeline } from '@/components/scale/detail/EvidenceTimeline';

// Every key resolves, as with the real bundles; the scale-evidence-reasons
// and scale-evidence-codes guards prove the keys have copy.
jest.mock('next-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (key: string) => `${ns}:${key}`,
    i18n: { language: 'en' },
  }),
}));

const refresh = jest.fn();
const mockUseScaleEnrollmentEvidence = jest.fn();

jest.mock('@/hooks/scale/useScaleEvidence', () => ({
  useScaleEnrollmentEvidence: (...args: unknown[]) =>
    mockUseScaleEnrollmentEvidence(...args),
}));

const evidence = {
  enrollment_id: 'enr-1',
  episode_id: 'episode-1',
  evaluation_id: 'evaluation-1',
  snapshot_id: 'snapshot-1',
  policy_version_id: 'policy-2026-09',
  target_version_id: 'target-7',
  algorithm_version: 'beta-v1',
  metric_semantic_contract_set_hash: 'contract-hash-1',
  maturity: 'economics_ready',
  data_quality_state: 'warning',
  health: 'fatigue_watch',
  // A classified health never carries a reason (`app/scale/evaluator.py`).
  health_reason: '',
  healthy_evaluations: 1,
  streak_counters: [{ signal_key: 'fatigue', consecutive_hits: 1 }],
  safe_watermark: '2026-09-07',
  last_complete_local_day: '2026-09-07',
  last_sync_at: '2026-09-08T01:00:00Z',
  freshness_reference_at: '2026-09-08T02:00:00Z',
  complete_local_days_in_episode: 14,
  active_delivery_days_in_episode: 12,
  current_window: {
    role: 'current_7d',
    start_local_day: '2026-09-01',
    end_local_day: '2026-09-08',
  },
  baseline_window: {
    role: 'baseline_7d',
    start_local_day: '2026-08-25',
    end_local_day: '2026-09-01',
  },
  rolling_window: {
    role: 'rolling_14d',
    start_local_day: '2026-08-25',
    end_local_day: '2026-09-08',
  },
  quality_issues: [
    {
      // A placement segment the source did not fulfil (`app/scale/evidence.py`).
      code: 'breakdown_segment_unknown',
      severity: 'warning',
      is_blocking: false,
      contract_type: 'breakdown_placement',
      window_role: 'current_7d',
      local_day: null,
      segment_key: 'feed',
      // `_quality_issue_rows` never writes these two.
      detail: null,
      recovery_action: null,
    },
  ],
  signals: [
    {
      signal_id: 'ctr_decline',
      state: 'hit',
      expected: null,
      observed: '0.31',
      interval_lower: '0.0021',
      interval_upper: '0.0064',
      threshold: '0.25',
      p_value: null,
      adjusted_q_value: null,
      reason_code: 'ok',
    },
    {
      signal_id: 'frequency_growth',
      state: 'unavailable',
      expected: null,
      observed: null,
      interval_lower: null,
      interval_upper: null,
      threshold: '0.2',
      p_value: null,
      adjusted_q_value: null,
      reason_code: 'unavailable_baseline',
    },
  ],
  cursor_as_of_local_day: '2026-09-07',
  cursor_source_revision: 7,
  cursor_semantic_event_sequence: 2,
  cursor_lineage_generation: 1,
  revision: 3,
  updated_at: '2026-09-08T02:00:00Z',
};

describe('EvidenceTimeline', () => {
  beforeEach(() => {
    refresh.mockReset().mockResolvedValue(undefined);
    mockUseScaleEnrollmentEvidence.mockReset().mockReturnValue({
      data: evidence,
      problem: undefined,
      isLoading: false,
      isStale: false,
      refresh,
    });
  });

  it('renders frozen windows, versions, data quality, watermark, and intervals', () => {
    render(
      <EvidenceTimeline enrollmentId="enr-1" metaMode="customer_sandbox" />
    );

    expect(screen.getByText('2026-09-01 → 2026-09-08')).toBeInTheDocument();
    expect(screen.getByText('policy-2026-09')).toBeInTheDocument();
    expect(screen.getByText('target-7')).toBeInTheDocument();
    expect(screen.getByText('2026-09-07')).toBeInTheDocument();
    expect(screen.getByText('[0.0021, 0.0064]')).toBeInTheDocument();
  });

  it('words a data-quality finding and folds its codes away', () => {
    render(
      <EvidenceTimeline enrollmentId="enr-1" metaMode="customer_sandbox" />
    );
    const finding = screen
      .getByText('scale:evidence.quality-issue.breakdown_segment_unknown')
      .closest('li')!;

    expect(
      within(finding).getByText('scale:evidence.quality-severity.warning')
    ).toBeInTheDocument();
    expect(
      within(finding).getByText('scale:evidence.window.current_7d')
    ).toBeInTheDocument();
    const technical = within(finding)
      .getByText('scale:evidence.technical-details')
      .closest('details')!;
    expect(technical).not.toHaveAttribute('open');
    expect(technical).toHaveTextContent(
      'breakdown_segment_unknown · breakdown_placement · feed'
    );
    expect(
      screen.queryByText(/^(breakdown_segment_unknown|warning|current_7d)$/)
    ).toBeNull();
  });

  it('shows the missing day, and keeps any detail in technical details', () => {
    mockUseScaleEnrollmentEvidence.mockReturnValue({
      data: {
        ...evidence,
        data_quality_state: 'blocked',
        quality_issues: [
          {
            code: 'daily_slot_missing',
            severity: 'blocking',
            is_blocking: true,
            contract_type: 'daily',
            window_role: null,
            local_day: '2026-09-05',
            segment_key: null,
            // Not written on main; a free-text detail stays support-only.
            detail: 'No daily row was returned.',
            recovery_action: 'resync_day',
          },
        ],
      },
      problem: undefined,
      isLoading: false,
      isStale: false,
      refresh,
    });
    render(
      <EvidenceTimeline enrollmentId="enr-1" metaMode="customer_sandbox" />
    );
    const finding = screen
      .getByText('scale:evidence.quality-issue.daily_slot_missing')
      .closest('li')!;
    const technical = within(finding)
      .getByText('scale:evidence.technical-details')
      .closest('details')!;

    expect(
      within(finding).getByText('scale:evidence.quality-severity.blocking')
    ).toBeInTheDocument();
    expect(within(finding).getByText('2026-09-05')).not.toBeNull();
    expect(technical).not.toContainElement(
      within(finding).getByText('2026-09-05')
    );
    expect(technical).toContainElement(
      screen.getByText('No daily row was returned.')
    );
    expect(technical).toContainElement(
      screen.getByText('scale:evidence.recovery-action')
    );
  });

  it('words a signal reason and shows a normal signal as a dash', () => {
    render(
      <EvidenceTimeline enrollmentId="enr-1" metaMode="customer_sandbox" />
    );
    const reasonOf = (signalId: string) =>
      within(screen.getByText(signalId).closest('tr')!)
        .getAllByRole('cell')
        .at(-1);

    expect(reasonOf('frequency_growth')).toHaveTextContent(
      'scale:evidence.signal-reason.unavailable_baseline'
    );
    expect(reasonOf('ctr_decline')).toHaveTextContent(/^—$/);
    expect(screen.queryByText(/^(ok|unavailable_baseline)$/)).toBeNull();
  });

  it('words a signal state', () => {
    render(
      <EvidenceTimeline enrollmentId="enr-1" metaMode="customer_sandbox" />
    );
    const stateOf = (signalId: string) =>
      within(screen.getByText(signalId).closest('tr')!).getAllByRole('cell')[1];

    expect(stateOf('ctr_decline')).toHaveTextContent(
      'scale:evidence.signal-states.hit'
    );
    expect(stateOf('frequency_growth')).toHaveTextContent(
      'scale:evidence.signal-states.unavailable'
    );
    expect(screen.queryByText(/^(hit|unavailable)$/)).toBeNull();
  });

  it('words why health is unclassified, and says so on the badge', () => {
    mockUseScaleEnrollmentEvidence.mockReturnValue({
      data: { ...evidence, health: null, health_reason: 'conflicting_signals' },
      problem: undefined,
      isLoading: false,
      isStale: false,
      refresh,
    });
    render(
      <EvidenceTimeline enrollmentId="enr-1" metaMode="customer_sandbox" />
    );

    expect(
      screen.getByText('scale:evidence.health-reason.conflicting_signals')
    ).toBeInTheDocument();
    expect(screen.getByText('scale:health.unclassified')).toBeInTheDocument();
    expect(screen.queryByText('scale:health.no-evidence')).toBeNull();
    expect(screen.queryByText('conflicting_signals')).toBeNull();
  });

  it('refreshes the API-owned evidence projection', () => {
    render(
      <EvidenceTimeline enrollmentId="enr-1" metaMode="customer_sandbox" />
    );
    fireEvent.click(screen.getByText('scale:evidence.refresh'));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it('treats missing first evidence as a warming state', () => {
    mockUseScaleEnrollmentEvidence.mockReturnValue({
      data: undefined,
      problem: { status: 404 },
      isLoading: false,
      isStale: false,
      refresh,
    });
    render(
      <EvidenceTimeline enrollmentId="enr-1" metaMode="customer_sandbox" />
    );
    expect(
      screen.getByText('scale:evidence.no-evidence-yet')
    ).toBeInTheDocument();
  });
});
