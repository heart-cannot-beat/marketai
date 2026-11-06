/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { AssetDirectionGroups } from '@/components/assets/AssetDirectionGroups';
import { buildDirectionGroups } from '@/lib/test-plans/direction-groups';
import { reviewQueueStorageKey } from '@/hooks/assets/useReviewMode';
import { runOnlyAssetFilters } from '@/hooks/assets/useAssetsUrlState';
import { stashGroupExport } from '@/lib/test-plans/group-export-handoff';
import type { UseRunDirectionGroupsResult } from '@/hooks/test-plans/useRunDirectionGroups';
import type { TestPlanDetail } from '@/lib/test-plans/plan-model';
import {
  direction,
  item,
  loadDirectionsNamespace,
  version,
} from '../../__tests__/test-plans/direction-groups-fixtures';

const urlState = {
  filters: runOnlyAssetFilters('gr_1'),
  setReviewMode: jest.fn(),
  setExportId: jest.fn(),
  setOverlay: jest.fn(),
};
const selection = { selectAll: jest.fn() };

jest.mock('@/components/assets/AssetsProvider', () => ({
  useAssets: () => ({ urlState, selection }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'MEMBER' } }),
}));
jest.mock('@/hooks/assets/useAssetImageUrl', () => ({
  useAssetImageUrl: (id: string) => ({
    blobUrl: `blob:${id}`,
    isLoading: false,
    problem: undefined,
    source: 'thumbnail',
  }),
}));

const directions = [
  direction('dir-1', '雨夜车库'),
  direction('dir-2', '周末露营'),
];
const plan = {
  plan_id: 'atp_1',
  product_name: 'VoltNest',
  directions,
  runs: [{ stage: 'full', run_id: 'gr_1' }],
} as unknown as TestPlanDetail;

function grouped(): UseRunDirectionGroupsResult {
  return {
    status: 'found',
    plan,
    stage: 'full',
    isLoading: false,
    readFailed: false,
    groups: buildDirectionGroups(
      directions,
      [item('i1', 0, 'dir-1'), item('i2', 1, 'dir-1'), item('i3', 2, 'dir-2')],
      [
        version('av_i1', { effective_review_value: 'approved' }),
        version('av_i2', { effective_review_value: 'needs_changes' }),
        version('av_i3'),
      ]
    ),
  };
}

beforeAll(loadDirectionsNamespace);
beforeEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
});

describe('AssetDirectionGroups', () => {
  it('shows each direction with its images and review progress, no ids', () => {
    const { container } = render(
      <AssetDirectionGroups runId="gr_1" grouped={grouped()} />
    );
    expect(
      screen.getByText(
        'This batch holds the full test of “VoltNest”, grouped by direction.'
      )
    ).toBeInTheDocument();
    const [rain, camp] = screen.getAllByTestId('direction-group');
    expect(within(rain).getAllByTestId('group-asset')).toHaveLength(2);
    expect(within(rain).getByTestId('group-review')).toHaveTextContent(
      'Reviewed 2 / 2'
    );
    expect(within(rain).getByTestId('group-review')).toHaveTextContent(
      'Needs changes 1'
    );
    expect(within(camp).getByTestId('group-review')).toHaveTextContent(
      'Not reviewed 1'
    );
    expect(container.textContent).not.toMatch(/dir-|av_|gr_|atp_/);
  });

  it('审核本组 seeds the review queue with the group and opens review mode', () => {
    render(<AssetDirectionGroups runId="gr_1" grouped={grouped()} />);
    fireEvent.click(
      screen.getByRole('button', { name: 'Review the images of “雨夜车库”' })
    );
    const key = reviewQueueStorageKey('acme', urlState.filters);
    expect(
      JSON.parse(window.sessionStorage.getItem(key)!).orderedVersionIds
    ).toEqual(['av_i1', 'av_i2']);
    expect(urlState.setReviewMode).toHaveBeenCalledWith(true);
  });

  it('导出本组 selects the group and opens the export dialog', () => {
    render(<AssetDirectionGroups runId="gr_1" grouped={grouped()} />);
    fireEvent.click(
      screen.getByRole('button', { name: 'Export the images of “周末露营”' })
    );
    expect(selection.selectAll).toHaveBeenCalledWith(['av_i3']);
    expect(urlState.setExportId).toHaveBeenCalledWith('new');
  });

  it('opens Asset Detail from a thumbnail', () => {
    render(<AssetDirectionGroups runId="gr_1" grouped={grouped()} />);
    fireEvent.click(screen.getByRole('button', { name: '“周末露营” image 1' }));
    expect(urlState.setOverlay).toHaveBeenCalledWith('av_i3');
  });

  it('picks up an export handed over from the Run page once', () => {
    stashGroupExport('acme', 'gr_1', ['av_i3']);
    const { rerender } = render(
      <AssetDirectionGroups runId="gr_1" grouped={grouped()} />
    );
    expect(selection.selectAll).toHaveBeenCalledWith(['av_i3']);
    expect(urlState.setExportId).toHaveBeenCalledWith('new');

    jest.clearAllMocks();
    rerender(<AssetDirectionGroups runId="gr_1" grouped={grouped()} />);
    expect(selection.selectAll).not.toHaveBeenCalled();
  });

  it('ignores a handoff for another Run', () => {
    stashGroupExport('acme', 'gr_2', ['av_x']);
    render(<AssetDirectionGroups runId="gr_1" grouped={grouped()} />);
    expect(selection.selectAll).not.toHaveBeenCalled();
    expect(urlState.setExportId).not.toHaveBeenCalled();
  });
});
