/**
 * @jest-environment jsdom
 */
import { render, screen, within } from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import { EvidenceTimeline } from '@/components/scale/detail/EvidenceTimeline';
import en from '../../locales/en/scale.json';
import zhCN from '../../locales/zh-CN/scale.json';

const mockUseScaleEnrollmentEvidence = jest.fn();

jest.mock('@/hooks/scale/useScaleEvidence', () => ({
  useScaleEnrollmentEvidence: (...args: unknown[]) =>
    mockUseScaleEnrollmentEvidence(...args),
}));

const window = (role: string, start: string, end: string) => ({
  role,
  start_local_day: start,
  end_local_day: end,
});

const signal = (
  signal_id: string,
  state: string,
  reason_code: string,
  observed: string | null
) => ({
  signal_id,
  state,
  expected: null,
  observed,
  interval_lower: null,
  interval_upper: null,
  threshold: '0.25',
  p_value: null,
  adjusted_q_value: null,
  reason_code,
});

/**
 * CTR fell while CPA clearly improved: the evaluator leaves health
 * unclassified with `conflicting_signals` (`app/scale/evaluator.py`).
 */
const evidence = {
  enrollment_id: 'enr-1',
  maturity: 'economics_ready',
  data_quality_state: 'pass',
  health: null,
  health_reason: 'conflicting_signals',
  healthy_evaluations: 0,
  streak_counters: [],
  safe_watermark: '2026-09-07',
  last_sync_at: '2026-09-08T01:00:00Z',
  complete_local_days_in_episode: 14,
  active_delivery_days_in_episode: 14,
  current_window: window('current_7d', '2026-09-01', '2026-09-08'),
  baseline_window: window('baseline_7d', '2026-08-25', '2026-09-01'),
  rolling_window: window('rolling_14d', '2026-08-25', '2026-09-08'),
  quality_issues: [],
  signals: [
    signal('ctr_decline', 'hit', 'ok', '0.31'),
    signal('cpa_worsening', 'miss', 'ok', '-0.30'),
    signal('frequency_growth', 'unavailable', 'unavailable_baseline', null),
  ],
  policy_version_id: 'policy-2026-09',
  target_version_id: null,
  algorithm_version: 'beta-v1',
  metric_semantic_contract_set_hash: 'contract-hash-1',
  cursor_as_of_local_day: '2026-09-07',
  cursor_source_revision: 7,
  updated_at: '2026-09-08T02:00:00Z',
};

function renderIn(locale: string, bundle: object) {
  // A fork of the suite's instance (`jest.setup.js`), which has no `scale`.
  const i18n = getI18n().cloneInstance({
    lng: locale,
    fallbackLng: false,
    forkResourceStore: true,
  });
  i18n.addResourceBundle(locale, 'scale', bundle);
  void i18n.changeLanguage(locale);
  return render(
    <I18nextProvider i18n={i18n}>
      <EvidenceTimeline enrollmentId="enr-1" metaMode="customer_sandbox" />
    </I18nextProvider>
  );
}

beforeEach(() => {
  mockUseScaleEnrollmentEvidence.mockReset().mockReturnValue({
    data: evidence,
    problem: undefined,
    isLoading: false,
    isStale: false,
    refresh: jest.fn(),
  });
});

test.each([
  [
    'en',
    en,
    'Not classified',
    'Health not classified: CTR declined while CPA or ROAS clearly improved.',
    'No usable baseline',
    ['Hit', 'Not hit', 'Unavailable'],
  ],
  [
    'zh-CN',
    zhCN,
    '未判定',
    '未判定健康状态：点击率下降，但 CPA 或 ROAS 明显改善，信号方向相反。',
    '没有可用的基线',
    ['命中', '未命中', '不可用'],
  ],
])(
  'words the health and signal reasons in %s',
  (locale, bundle, badge, healthReason, signalReason, states) => {
    renderIn(locale, bundle);

    expect(screen.getByText(badge)).toBeInTheDocument();
    expect(screen.getByText(healthReason)).toBeInTheDocument();
    const cellsOf = (signalId: string) =>
      within(screen.getByText(signalId).closest('tr')!).getAllByRole('cell');
    expect(cellsOf('frequency_growth').at(-1)).toHaveTextContent(signalReason);
    expect(cellsOf('ctr_decline').at(-1)).toHaveTextContent(/^—$/);
    expect(
      ['ctr_decline', 'cpa_worsening', 'frequency_growth'].map(
        (signalId) => cellsOf(signalId)[1].textContent
      )
    ).toEqual(states);
    expect(document.body.textContent).not.toMatch(
      /conflicting_signals|unavailable_baseline|\bok\b/
    );
  }
);

test.each([
  ['en', en, 'A breakdown segment is incomplete', 'Warning', 'Current 7 days'],
  ['zh-CN', zhCN, '有细分数据不完整', '警告', '最近 7 天'],
])(
  'words a data-quality finding in %s',
  (locale, bundle, sentence, severity, window) => {
    mockUseScaleEnrollmentEvidence.mockReturnValue({
      data: {
        ...evidence,
        data_quality_state: 'warning',
        quality_issues: [
          {
            code: 'breakdown_segment_unknown',
            severity: 'warning',
            is_blocking: false,
            contract_type: 'breakdown_placement',
            window_role: 'current_7d',
            local_day: null,
            segment_key: 'feed',
            detail: null,
            recovery_action: null,
          },
        ],
      },
      problem: undefined,
      isLoading: false,
      isStale: false,
      refresh: jest.fn(),
    });
    renderIn(locale, bundle);

    const finding = screen.getByText(sentence).closest('li')!;
    expect(within(finding).getByText(severity)).toBeInTheDocument();
    expect(within(finding).getByText(window)).toBeInTheDocument();
    // The codes are only in the folded technical details.
    const visible = finding.cloneNode(true) as HTMLElement;
    visible.querySelectorAll('details').forEach((details) => details.remove());
    expect(visible.textContent).not.toMatch(
      /breakdown_segment_unknown|breakdown_placement|warning|current_7d/
    );
  }
);

/**
 * A new Episode younger than the 14-day window (#57): the evaluator returns
 * `warming` and withholds health with `episode_window_incomplete`; the
 * sentence says how many complete days are in.
 */
test.each([
  [
    'en',
    en,
    'Warming',
    'Health not classified yet: the Episode has data for 5 of the 14 complete days the 7-day comparison needs. Health and advice start once all 14 are in.',
    'Not computed: the Episode is younger than the 14-day window',
  ],
  [
    'zh-CN',
    zhCN,
    '预热中',
    '暂未判定健康状态：这个观察段已有 5 个完整日的数据，7 天对比需要 14 个。满 14 个完整日后才开始判定健康状态和给出建议。',
    '未计算：观察段还不满 14 天窗口',
  ],
])(
  'words a warming Episode and its day count in %s',
  (locale, bundle, maturity, healthReason, signalReason) => {
    mockUseScaleEnrollmentEvidence.mockReturnValue({
      data: {
        ...evidence,
        maturity: 'warming',
        health_reason: 'episode_window_incomplete',
        complete_local_days_in_episode: 5,
        active_delivery_days_in_episode: 5,
        signals: [
          signal(
            'ctr_decline',
            'unavailable',
            'episode_window_incomplete',
            null
          ),
        ],
      },
      problem: undefined,
      isLoading: false,
      isStale: false,
      refresh: jest.fn(),
    });
    renderIn(locale, bundle);

    expect(screen.getByText(maturity)).toBeInTheDocument();
    expect(screen.getByText(healthReason)).toBeInTheDocument();
    const cells = within(
      screen.getByText('ctr_decline').closest('tr')!
    ).getAllByRole('cell');
    expect(cells.at(-1)).toHaveTextContent(signalReason);
    expect(document.body.textContent).not.toMatch(/episode_window_incomplete/);
  }
);
