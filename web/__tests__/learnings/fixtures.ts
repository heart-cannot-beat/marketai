import { getI18n } from 'react-i18next';
import learnings from '../../locales/en/learnings.json';

/** jest.setup.js loads common/assets/products; this suite adds its own. */
export function loadLearningsNamespace() {
  const i18n = getI18n();
  if (!i18n.hasResourceBundle('en', 'learnings')) {
    i18n.addResourceBundle('en', 'learnings', learnings, true, true);
  }
}

const N = (impressions: number, clicks: number, assets: number) => ({
  impressions,
  clicks,
  spend: '200.00',
  conversions: '10',
  assets,
  ads: 0,
});

export function comparison(
  metric: 'ctr' | 'cpa',
  verdict: string,
  lift: [string, string, string] | null,
  n = N(55200, 1159, 23)
) {
  return {
    metric,
    verdict,
    estimate: metric === 'ctr' ? '0.021000' : '20.000000',
    interval_low: null,
    interval_high: null,
    baseline: metric === 'ctr' ? '0.012000' : '20.000000',
    lift: lift ? lift[0] : null,
    lift_low: lift ? lift[1] : null,
    lift_high: lift ? lift[2] : null,
    confidence: '0.95',
    adjusted_alpha: '0.0500',
    reasons: lift ? [] : ['value_low_clicks'],
    flags: [],
    balance: null,
    n,
    baseline_n: n,
    shortfall: {},
  };
}

function value(
  value_id: string,
  label: string,
  verdict: string,
  sentence: string,
  lift: [string, string, string] | null
) {
  return {
    value_id,
    label,
    compared_with: '纯产品',
    verdict,
    verdict_metric: 'ctr',
    flags: [],
    sentence,
    n: N(55200, 1159, 23),
    ctr: comparison('ctr', verdict, lift),
    cpa: comparison('cpa', 'no_difference', ['0.01', '-0.3', '0.4']),
  };
}

export const WIN_SENTENCE =
  '真人使用 比 纯产品 CTR 高 78%（95% 区间 +62%~+96%，n=55,200 次展示 / 23 张素材）；CPA 差异尚不显著';

export function readyLearnings(overrides: Record<string, unknown> = {}) {
  return {
    product_id: 'prod_1',
    product_name: 'VoltNest 充电桩',
    status: 'ready',
    evidence_mode: 'production',
    evidence_notice: null,
    available_modes: ['production'],
    snapshot_id: 'lrn_1',
    computed_at: '2026-10-11T08:00:00+00:00',
    algorithm_version: 'learnings-v1',
    thresholds: {
      min_impressions: '1000',
      min_clicks: '30',
      min_conversions: '10',
      min_assets: '2',
    },
    currency: 'USD',
    highlights: [WIN_SENTENCE],
    rounds: [
      {
        round_id: 'atp_1',
        plan_id: 'atp_1',
        round_number: 1,
        label: '第 1 轮',
        goal: 'explore',
        plan_created_at: '2026-10-01T00:00:00+00:00',
        totals: {
          ...N(108000, 1800, 45),
          ctr: '0.0166',
          cpa: null,
          cpa_assets: 45,
        },
        factors: [
          {
            key: 'people',
            label: '人物',
            values: [
              value('people-1', '真人使用', 'winner', WIN_SENTENCE, [
                '0.78',
                '0.62',
                '0.96',
              ]),
              value(
                'people-2',
                '纯产品',
                'loser',
                '纯产品 比 真人使用 CTR 低 44%（95% 区间 -49%~-38%，n=52,800 次展示 / 22 张素材）',
                ['-0.44', '-0.49', '-0.38']
              ),
            ],
          },
          {
            key: 'scene',
            label: '场景',
            values: [
              value(
                'scene-1',
                '居家日常',
                'insufficient_data',
                '居家日常：样本不足，尚不能下结论（目前 420 次展示、9 次点击、1 张素材；每组至少 1,000 次展示、30 次点击、2 张素材）',
                null
              ),
            ],
          },
        ],
        directions: [],
      },
    ],
    unattributable: [],
    can_seed_next_round: true,
    review_signals: null,
    recent_imports: [],
    ...overrides,
  };
}

export function noDataLearnings() {
  return readyLearnings({
    status: 'no_data',
    snapshot_id: null,
    computed_at: null,
    highlights: [],
    rounds: [],
    can_seed_next_round: false,
    review_signals: {
      round_number: 1,
      note: '这是人工审核的弱信号，不是投放数据',
      factors: [
        {
          key: 'people',
          label: '人物',
          values: [
            {
              value_id: 'people-1',
              label: '真人使用',
              approved: 3,
              needs_changes: 0,
              rejected: 1,
              assets: 4,
            },
          ],
        },
      ],
    },
  });
}

export function importView(overrides: Record<string, unknown> = {}) {
  return {
    import_id: 'rimp_1',
    product_id: 'prod_1',
    source: 'csv',
    status: 'previewed',
    file_name: '',
    currency: 'USD',
    summary: {
      total_rows: 3,
      importable_rows: 2,
      by_status: { ready: 2, replaces: 0, unattributable: 0, rejected: 1 },
      rejected_reasons: [
        { code: 'unknown_code', reason: '找不到这个素材编号', count: 1 },
      ],
      evidence_modes: { production: 2 },
      impressions: 2000,
      clicks: 40,
      attributable_assets: 2,
      columns: {},
      ignored_columns: [],
    },
    rows: [
      {
        line: 2,
        status: 'ready',
        status_label: '可以导入',
        reason_code: null,
        reason: null,
        asset_code: 'MK-7F3K2Q',
        asset_version_id: 'av_1',
        description: '主打快充 · 居家日常 · 真人使用',
        round_number: 1,
        evidence_mode: 'production',
        input: {},
      },
      {
        line: 4,
        status: 'rejected',
        status_label: '不会导入',
        reason_code: 'unknown_code',
        reason: '找不到这个素材编号',
        asset_code: 'MK-000000',
        asset_version_id: null,
        description: null,
        round_number: null,
        evidence_mode: null,
        input: {},
      },
    ],
    rows_truncated: false,
    snapshot_ids: [],
    created_at: null,
    committed_at: null,
    duplicate: false,
    replayed: false,
    ...overrides,
  };
}
