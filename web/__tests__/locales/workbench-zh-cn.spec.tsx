/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import type { ReactElement } from 'react';
import { DuplicateResolutionPanel } from '@/components/experiments/DuplicateResolutionPanel';
import { FrozenResolutionEvidence } from '@/components/experiments/FrozenResolutionEvidence';
import {
  LocalizationItemInspector,
  TruthPanel,
} from '@/components/localization/LocalizationWorkspace';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import common from '../../locales/zh-CN/common.json';
import experiments from '../../locales/zh-CN/experiments.json';
import localization from '../../locales/zh-CN/localization.json';

type Batch = components['schemas']['LocalizationBatchDetail'];
type Item = components['schemas']['LocalizationItemDetail'];

/**
 * The Advanced workbench (Experiments, Localization) renders its copy, its
 * server states and its dates in the interface language: a zh-CN user sees
 * Chinese, not the English source strings the en suites assert.
 */
function renderInChinese(ui: ReactElement) {
  // A fork of the suite's en instance (`jest.setup.js`) with its own store,
  // so the zh-CN bundles never leak into other assertions.
  const i18n = getI18n().cloneInstance({
    lng: 'zh-CN',
    fallbackLng: false,
    forkResourceStore: true,
  });
  i18n.addResourceBundle('zh-CN', 'common', common);
  i18n.addResourceBundle('zh-CN', 'experiments', experiments);
  i18n.addResourceBundle('zh-CN', 'localization', localization);
  void i18n.changeLanguage('zh-CN');
  return render(<I18nextProvider i18n={i18n}>{ui}</I18nextProvider>);
}

describe('Advanced workbench in zh-CN', () => {
  it('renders the Truth panel copy, states and dates in Chinese', () => {
    const batch: Batch = {
      id: 'batch-1',
      phase: 'ready',
      outcome: 'none',
      product_version_id: 'product-v1',
      offer_version_id: 'offer-v1',
      provider_execution_mode: 'mock',
      source_count: 1,
      market_count: 1,
      item_count: 0,
      revision: 3,
      created_at: '2026-09-09T00:00:00Z',
      updated_at: '2026-09-09T00:00:00Z',
      truth_data_mode: 'mock',
      max_attempts: 3,
      truth_status: 'degraded',
      truth_freshness: 'expired',
      truth_valid_until: '2026-10-26T15:04:05Z',
      markets: [
        {
          market: 'DE',
          market_policy_version_id: 'market-policy-v1',
          locale: 'de-DE',
          currency: 'EUR',
          readiness: 'degraded',
          facts: [],
          source_attempts: [],
        },
      ],
    };

    renderInChinese(<TruthPanel batch={batch} />);

    expect(screen.getByText('真值包')).toBeInTheDocument();
    expect(screen.getByText('真值已过时，无法执行')).toBeInTheDocument();
    expect(screen.getByText(/新鲜度为 已过期/)).toBeInTheDocument();
    expect(screen.getByText('这个市场没有已保存的事实。')).toBeInTheDocument();
    expect(screen.getAllByText('降级').length).toBeGreaterThan(0);
    // zh-CN date order (year first), whatever the test machine's time zone.
    expect(screen.getByText(/有效期至 2026\/10\/2[67]/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Truth package|Valid until/);
  });

  it('renders duplicate resolution and frozen evidence in Chinese', () => {
    renderInChinese(
      <>
        <DuplicateResolutionPanel
          stateKey="analysis-1"
          onResolutionsChange={() => undefined}
          groups={[
            {
              duplicate_group_hash: 'group-1',
              dimension_key: 'hook',
              concept_key_id: 'concept-1',
              concept_key_name: 'Free shipping',
              members: [
                {
                  source_entry_id: 'entry-1',
                  source_candidate_fingerprint: 'fp-1',
                  display_label: 'Candidate A',
                  asset_version_id: 'asset-version-000001',
                },
                {
                  source_entry_id: 'entry-2',
                  source_candidate_fingerprint: 'fp-2',
                  display_label: 'Candidate B',
                  asset_version_id: 'asset-version-000002',
                },
              ],
              allowed_actions: ['allow_controlled_duplicate'],
              remap_targets: [],
            },
          ]}
        />
        <FrozenResolutionEvidence
          evidence={{
            concept_resolutions_hash: 'hash-1',
            had_duplicates: false,
          }}
        />
      </>
    );

    expect(screen.getByText('重复概念处理')).toBeInTheDocument();
    expect(screen.getByText('2 个候选共用这个概念')).toBeInTheDocument();
    expect(
      screen.getByRole('radio', { name: '允许（受控重复）' })
    ).toBeInTheDocument();
    expect(screen.getByLabelText('原因（必填）')).toBeInTheDocument();
    expect(screen.getByText('冻结时没有发现重复概念。')).toBeInTheDocument();
  });

  it('words an item reason and a failed call in Chinese', () => {
    const item: Item = {
      id: 'item-1',
      batch_id: 'batch-1',
      state: 'cancelled',
      target_market: 'DE',
      source_asset_version_id: 'asset-source-v1',
      decision_id: 'decision-1',
      truth_package_id: 'truth-1',
      truth_package_market_id: 'truth-market-1',
      market_policy_version_id: 'market-policy-v1',
      inventory_id: 'inventory-1',
      claim_decision_set_id: 'claims-1',
      copy_version_id: 'copy-1',
      attempt_count: 1,
      max_attempts: 3,
      revision: 5,
      reason_code: 'batch_cancelled',
      reason_detail: 'batch cancelled by user-1',
      lineage: {
        source_asset_version_id: 'asset-source-v1',
        decision_id: 'decision-1',
        offer_version_id: 'offer-v1',
        truth_package_id: 'truth-1',
        copy_version_id: 'copy-1',
        localized_asset_version_id: null,
      },
      attempts: [
        {
          id: 'attempt-1',
          attempt_number: 1,
          state: 'failed',
          provider: 'mock-edit',
          model: 'edit-v1',
          created_at: '2026-09-09T00:00:00Z',
          output: null,
          publication: null,
          provider_calls: [
            {
              step: 'localization_edit',
              dispatch_state: 'failed',
              chargeability: 'free_mock',
              provider: 'mock-edit',
              model: 'edit-v1',
              error_code: 'provider_error',
            },
          ],
          qc: [],
        },
      ],
      allowed_actions: [
        { action: 'retry', allowed: false, reason_code: 'item_not_retry_wait' },
      ],
    };

    renderInChinese(
      <LocalizationItemInspector
        item={item}
        onRetry={() => undefined}
        onResume={() => undefined}
        isPending={false}
      />
    );

    expect(screen.getByText('批次已取消。')).toBeInTheDocument();
    expect(screen.getByText('技术信息')).toBeInTheDocument();
    expect(
      screen.getByText(
        /错误：AI 服务返回了错误，这次调用没有完成，请稍后重试。/
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText('无法重试：只有等待重试的条目可以重试')
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /^图片编辑: mock-edit\/edit-v1 · 失败 · 免费（Mock） · 未预留费用上限/
      )
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(
      /The batch was cancelled|Technical details|item_not_retry_wait|localization_edit|free_mock/
    );
  });

  it('words Truth readiness, attempt sources and a binding in Chinese', () => {
    const batch: Batch = {
      id: 'batch-1',
      phase: 'preparing',
      outcome: 'none',
      product_version_id: 'product-v1',
      offer_version_id: 'offer-v1',
      provider_execution_mode: 'mock',
      source_count: 1,
      market_count: 2,
      item_count: 0,
      revision: 3,
      created_at: '2026-09-09T00:00:00Z',
      updated_at: '2026-09-09T00:00:00Z',
      truth_data_mode: 'mock',
      max_attempts: 3,
      truth_status: 'degraded',
      truth_freshness: 'current',
      markets: [
        {
          market: 'DE',
          market_policy_version_id: 'market-policy-de',
          locale: 'de-DE',
          currency: 'EUR',
          readiness: 'pending',
          readiness_detail: {},
          facts: [],
          source_attempts: [],
        },
        {
          market: 'CA',
          market_policy_version_id: 'market-policy-ca',
          locale: 'en-CA',
          currency: 'CAD',
          readiness: 'degraded',
          readiness_detail: {
            reason: 'fact_missing_permitted',
            missing: ['discount'],
          },
          facts: [],
          source_attempts: [
            {
              id: 'attempt-mock',
              fact_type: 'discount',
              adapter_kind: 'controlled_mock',
              data_source_version_id: 'source-v1',
              outcome: 'failed',
              failure_code: 'fact_type_not_in_mock_scenario',
            },
            {
              id: 'attempt-skip',
              fact_type: 'discount',
              adapter_kind: 'policy',
              data_source_version_id: 'source-v1',
              outcome: 'skipped',
              skip_code: 'on_failure_skip',
            },
          ],
        },
        {
          market: 'FR',
          market_policy_version_id: 'market-policy-fr',
          locale: 'fr-FR',
          currency: 'EUR',
          readiness: 'failed',
          readiness_detail: {
            reason: 'fact_missing_blocked',
            blocked: ['delivery_time'],
            missing: ['delivery_time', 'tax'],
          },
          facts: [],
          source_attempts: [
            {
              id: 'attempt-block',
              fact_type: 'delivery_time',
              adapter_kind: 'policy',
              data_source_version_id: 'source-v1',
              outcome: 'skipped',
              skip_code: 'on_failure_block',
            },
          ],
        },
      ],
    };
    const item: Item = {
      id: 'item-1',
      batch_id: 'batch-1',
      state: 'succeeded',
      target_market: 'DE',
      source_asset_version_id: 'asset-source-v1',
      decision_id: 'decision-1',
      truth_package_id: 'truth-1',
      truth_package_market_id: 'truth-market-1',
      market_policy_version_id: 'market-policy-v1',
      inventory_id: 'inventory-1',
      claim_decision_set_id: 'claims-1',
      copy_version_id: 'copy-1',
      attempt_count: 1,
      max_attempts: 3,
      revision: 5,
      lineage: {
        source_asset_version_id: 'asset-source-v1',
        decision_id: 'decision-1',
        offer_version_id: 'offer-v1',
        truth_package_id: 'truth-1',
        copy_version_id: 'copy-1',
        localized_asset_version_id: 'asset-localized-v1',
      },
      binding: {
        id: 'binding-1',
        kind: 'localized',
        bound_asset_version_id: 'asset-localized-v1',
        review_state: 'pending_review',
        truth_freshness: 'current',
        ready_for_experiment: false,
      },
      attempts: [],
      allowed_actions: [],
    };

    renderInChinese(
      <>
        <TruthPanel batch={batch} />
        <LocalizationItemInspector
          item={item}
          onRetry={() => undefined}
          onResume={() => undefined}
          isPending={false}
        />
      </>
    );

    expect(
      screen.getByText('还没有解析真值，开始准备时才会解析。')
    ).toBeInTheDocument();
    // The missing fact types stay as they are.
    expect(
      screen.getByText(
        '部分事实未能解析，这个市场会在缺少它们的情况下继续。缺少：discount。'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText('discount（经 受控 Mock）：模拟场景中没有这个事实。')
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'discount（经 兜底规则）：没有来源解析出这个事实（策略：跳过）'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        '策略要求的事实未能解析（策略：阻断），这个市场不执行。阻断的事实：delivery_time。缺少：delivery_time、tax。'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'delivery_time（经 兜底规则）：没有来源解析出这个事实（策略：阻断，这个市场不执行）'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        '当前绑定：本地化版本 · 审核：待审核 · 真值：最新 · 还不能用于新实验'
      )
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(
      /\{\}|Truth has not|Missing|Blocking|via|Active binding|fact_missing_permitted|fact_missing_blocked|on_failure_block|controlled_mock|policy|pending_review|false/
    );
  });
});
