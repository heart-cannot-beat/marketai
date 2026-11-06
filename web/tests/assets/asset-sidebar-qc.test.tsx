/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';

import {
  AssetSidebar,
  type AssetVersionDetail,
} from '@/components/assets/AssetSidebar';

function detail(
  overrides: Partial<AssetVersionDetail> = {}
): AssetVersionDetail {
  return {
    id: 'v1',
    asset_id: 'a1',
    product_id: 'p1',
    run_id: 'r1',
    provider: 'openai',
    model: 'gpt-image-2',
    status: 'ok',
    kind: 'generated',
    aspect_ratio: '1:1',
    seed: { requested: null, effective: null, unavailable_reason: null },
    prompt: 'a charger',
    execution_mode: 'byok',
    origin_kind: 'generation',
    tags: {},
    effective_review_value: 'needs_changes',
    review_head_id: 'qr1',
    reviews: [
      {
        id: 'qr1',
        asset_version_id: 'v1',
        review_value: 'needs_changes',
        reason: '自动质检未通过：外壳是亮银色，参考图是哑光黑。',
        actor_id: 'system:vlm_qc',
        source: 'machine_qc',
      },
    ],
    qc_attempts: [
      {
        id: 'qca1',
        state: 'failed_blocking',
        attempt_sequence: 1,
        feedback_round: 1,
        execution_mode: 'byok',
        summary: '外观与参考图不一致。',
        reasons: ['外壳是亮银色，参考图是哑光黑。'],
        consecutive_failures: 2,
        projected_review: 'needs_changes',
        controlled_mock: false,
        checks: [
          {
            rule_id: 'must_match:mm-color',
            check_type: 'vlm',
            severity: 'must_match',
            outcome: 'fail',
            verdict: 'fail',
            reason: '外壳是亮银色，参考图是哑光黑。',
            description: 'Matte black housing',
          },
          {
            rule_id: 'product_present',
            check_type: 'vlm',
            severity: 'must_match',
            outcome: 'pass',
            verdict: 'pass',
            reason: '产品在画面中央。',
          },
        ],
      },
    ],
    latest_qc_state: 'failed_blocking',
    latest_qc_attempt_id: 'qca1',
    contextual_conclusions: [],
    parent: null,
    children: [],
    mock_evidence: null,
    byte_size: 2048,
    mime_type: 'image/png',
    content_sha256: 'sha',
    producing_run_item_id: 'ri1',
    provider_attempt_id: 'pa1',
    allowed_actions: [],
    created_at: '2026-10-12T00:00:00Z',
    ...overrides,
  } as AssetVersionDetail;
}

describe('AssetSidebar quality checks (Issue #169)', () => {
  it('shows the failed checks with their reasons and the machine review', () => {
    render(<AssetSidebar detail={detail()} onNavigateVersion={jest.fn()} />);

    const findings = screen.getAllByTestId('qc-finding');
    expect(findings).toHaveLength(1);
    expect(findings[0]).toHaveTextContent(
      'Matte black housing: 外壳是亮银色，参考图是哑光黑。'
    );
    expect(screen.getByText('外观与参考图不一致。')).toBeInTheDocument();
    expect(screen.getByTestId('qc-state')).toHaveTextContent(
      'Failed (blocking)'
    );
    expect(screen.getByTestId('qc-state')).toHaveClass('badge-error');
    expect(
      screen.getByText('This combination has failed QC 2 times in a row')
    ).toBeInTheDocument();
    expect(screen.getByText('(automatic QC)')).toBeInTheDocument();
    // Review values read as Chinese labels, never the raw enum.
    expect(screen.getByTestId('asset-detail-review')).toHaveTextContent(
      'Needs changes'
    );
    expect(screen.queryByText(/failed_blocking|needs_changes/)).toBeNull();
  });

  it('marks an uncertain check as a warning, never as passed', () => {
    render(
      <AssetSidebar
        detail={detail({
          effective_review_value: null,
          reviews: [],
          qc_attempts: [
            {
              id: 'qca2',
              state: 'uncertain',
              attempt_sequence: 1,
              feedback_round: 0,
              execution_mode: 'byok',
              summary: '参考图未能读取。',
              reasons: ['1 张参考图未能读取，外观无法与参考图对照。'],
              consecutive_failures: 0,
              controlled_mock: false,
              checks: [
                {
                  rule_id: 'reference_images',
                  check_type: 'vlm',
                  severity: 'should_match',
                  outcome: 'error',
                  verdict: 'uncertain',
                  reason: '1 张参考图未能读取，外观无法与参考图对照。',
                },
              ],
            },
          ],
        })}
        onNavigateVersion={jest.fn()}
      />
    );

    expect(screen.getByText('Inconclusive')).toHaveClass('badge-warning');
    expect(screen.getByTestId('asset-detail-review')).toHaveTextContent(
      'Not reviewed'
    );
    expect(screen.getByTestId('qc-finding')).toHaveClass('text-warning');
  });
});
