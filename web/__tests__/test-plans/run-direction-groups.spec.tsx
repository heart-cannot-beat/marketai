/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { RunProgressView } from '@/components/generation/progress/RunProgressView';
import { buildDirectionGroups } from '@/lib/test-plans/direction-groups';
import { reviewQueueStorageKey } from '@/hooks/assets/useReviewMode';
import { runOnlyAssetFilters } from '@/hooks/assets/useAssetsUrlState';
import { takeGroupExport } from '@/lib/test-plans/group-export-handoff';
import type { UseRunDirectionGroupsResult } from '@/hooks/test-plans/useRunDirectionGroups';
import type { TestPlanDetail } from '@/lib/test-plans/plan-model';
import {
  direction,
  item,
  loadDirectionsNamespace,
  version,
} from './direction-groups-fixtures';

const mockPush = jest.fn();
const mockUseRunProgress = jest.fn();

jest.mock('next/router', () => ({
  useRouter: () => ({ push: mockPush, query: { slug: 'acme' } }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'OWNER' } }),
}));
jest.mock('@/components/generation/shared/useRunProgress', () => ({
  useRunProgress: (...args: unknown[]) => mockUseRunProgress(...args),
}));
jest.mock('@/hooks/products/useProductDirectory', () => ({
  useProductDirectory: () => ({ nameFor: () => 'VoltNest' }),
}));
jest.mock('@/hooks/assets/useAssetImageUrl', () => ({
  useAssetImageUrl: (id: string) => ({
    blobUrl: `blob:${id}`,
    isLoading: false,
    problem: undefined,
    source: 'thumbnail',
  }),
}));

const run = {
  id: 'gr_1',
  product_id: 'prod_1',
  provider: 'mock',
  model: 'mock-v1',
  status: 'running',
  total: 4,
  done: 2,
  failed: 1,
  execution_mode: 'mock',
  revision: 1,
  commands: [],
  allowed_actions: [],
};

const pageItem = {
  id: 'page-item',
  ordinal: 0,
  state: 'succeeded',
  combination_fingerprint: 'fp',
  variant_index: 0,
  execution_mode: 'mock',
  error_summary: '',
};

const directions = [
  direction('dir-1', '雨夜车库'),
  direction('dir-2', '周末露营'),
];
const items = [
  item('i1', 0, 'dir-1'),
  item('i2', 1, 'dir-1', { state: 'failed', bound_asset_version_id: null }),
  item('i3', 2, 'dir-2'),
  item('i4', 3, 'dir-9', { state: 'running', bound_asset_version_id: null }),
];
const versions = [
  version('av_i1', { effective_review_value: 'approved' }),
  version('av_i3'),
];

const plan = {
  plan_id: 'atp_1',
  product_name: 'VoltNest',
  directions,
  runs: [{ stage: 'sample', run_id: 'gr_1' }],
} as unknown as TestPlanDetail;

function grouped(
  overrides: Partial<UseRunDirectionGroupsResult> = {}
): UseRunDirectionGroupsResult {
  return {
    status: 'found',
    plan,
    stage: 'sample',
    groups: buildDirectionGroups(directions, items, versions),
    isLoading: false,
    readFailed: false,
    ...overrides,
  };
}

beforeAll(loadDirectionsNamespace);
beforeEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
  mockUseRunProgress.mockReturnValue({
    run,
    items: [pageItem],
    itemsPagination: { cursor: null, hasMore: false },
    latestCommandOperation: null,
    transportError: null,
    problem: null,
  });
});

describe('RunProgressView grouped by direction', () => {
  it('shows each direction with what it tests, status and review progress', () => {
    const { container } = render(
      <RunProgressView runId="gr_1" directionGroups={grouped()} />
    );

    expect(
      screen.getByText(
        'This batch holds the samples of the test “VoltNest”, grouped by direction.'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: 'Back to the test' })
    ).toHaveAttribute('href', '/w/acme/tests/atp_1');
    const groups = screen.getAllByTestId('direction-group');
    expect(groups).toHaveLength(3);

    const rain = within(groups[0]);
    expect(rain.getByRole('heading', { name: '雨夜车库' })).toBeInTheDocument();
    expect(rain.getByText('雨夜车库是否更打动通勤族')).toBeInTheDocument();
    expect(rain.getByTestId('group-status')).toHaveTextContent('Done 1');
    expect(rain.getByTestId('group-status')).toHaveTextContent('Failed 1');
    expect(rain.getByTestId('group-review')).toHaveTextContent(
      'Reviewed 1 / 1'
    );
    expect(rain.getByTestId('group-review')).toHaveTextContent('Approved 1');
    expect(rain.getAllByTestId('group-item')).toHaveLength(2);
    expect(
      rain.getByRole('button', { name: '“雨夜车库” image 2, failed' })
    ).toBeInTheDocument();

    const unmatched = within(groups[2]);
    expect(
      unmatched.getByRole('heading', { name: 'Not matched to a direction' })
    ).toBeInTheDocument();
    expect(unmatched.getByTestId('group-status')).toHaveTextContent(
      'Generating 1'
    );
    expect(unmatched.getByTestId('group-review')).toHaveTextContent(
      'You can review once images are generated.'
    );

    // No internal identifiers on screen (contract §1 principle 2).
    expect(container.textContent).not.toMatch(/dir-|av_|gr_|atp_|lib_/);
  });

  it('opens the existing item detail from a grouped tile', () => {
    render(<RunProgressView runId="gr_1" directionGroups={grouped()} />);
    fireEvent.click(
      screen.getByRole('button', { name: '“周末露营” image 1, done' })
    );
    expect(
      screen.getByRole('button', { name: '“周末露营” image 1, done' })
    ).toHaveAttribute('aria-pressed', 'true');
    // i3 is not on the polled page; the grouped list row feeds the panel.
    expect(screen.queryByText('Select an item to see its details.')).toBeNull();
    expect(
      screen.getByRole('heading', { name: 'Item #3' })
    ).toBeInTheDocument();
  });

  it('switches back to every item', () => {
    render(<RunProgressView runId="gr_1" directionGroups={grouped()} />);
    fireEvent.click(screen.getByRole('radio', { name: 'All items' }));
    expect(screen.queryAllByTestId('direction-group')).toHaveLength(0);
    expect(screen.getAllByTestId('run-item-tile')).toHaveLength(1);
    expect(screen.getByRole('radio', { name: 'All items' })).toHaveAttribute(
      'aria-checked',
      'true'
    );
  });

  it('falls back to the item grid for a Run without a plan', () => {
    render(
      <RunProgressView
        runId="gr_1"
        directionGroups={grouped({ status: 'none', plan: null, groups: [] })}
      />
    );
    expect(screen.queryByTestId('direction-view-toggle')).toBeNull();
    expect(screen.queryByTestId('run-direction-groups')).toBeNull();
    expect(screen.getAllByTestId('run-item-tile')).toHaveLength(1);
  });

  it('says when part of the Run could not be read', () => {
    render(
      <RunProgressView
        runId="gr_1"
        directionGroups={grouped({ readFailed: true })}
      />
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      "Some images couldn't be read, so the groups may be incomplete."
    );
  });

  it('waits instead of flashing the grid while the plan is looked up', () => {
    render(
      <RunProgressView
        runId="gr_1"
        directionGroups={grouped({ status: 'loading', plan: null, groups: [] })}
      />
    );
    expect(screen.queryAllByTestId('run-item-tile')).toHaveLength(0);
  });

  it('审核本组 opens Assets review mode on exactly this group', () => {
    render(<RunProgressView runId="gr_1" directionGroups={grouped()} />);
    fireEvent.click(
      screen.getByRole('button', { name: 'Review the images of “雨夜车库”' })
    );

    expect(mockPush).toHaveBeenCalledWith(
      '/w/acme/assets?run=gr_1&reviewMode=1'
    );
    const key = reviewQueueStorageKey('acme', runOnlyAssetFilters('gr_1'));
    const snapshot = JSON.parse(window.sessionStorage.getItem(key)!);
    expect(snapshot.orderedVersionIds).toEqual(['av_i1']);
    expect(snapshot.currentIndex).toBe(0);
  });

  it('导出本组 hands the group to the Assets export dialog', () => {
    render(<RunProgressView runId="gr_1" directionGroups={grouped()} />);
    fireEvent.click(
      screen.getByRole('button', { name: 'Export the images of “周末露营”' })
    );

    expect(mockPush).toHaveBeenCalledWith(
      '/w/acme/assets?run=gr_1&view=directions'
    );
    expect(takeGroupExport('acme', 'gr_other')).toBeNull();
    expect(takeGroupExport('acme', 'gr_1')).toEqual(['av_i3']);
    // One-shot.
    expect(takeGroupExport('acme', 'gr_1')).toBeNull();
  });

  it('去投放实验 needs an approved image; empty groups cannot review or export', () => {
    render(<RunProgressView runId="gr_1" directionGroups={grouped()} />);
    const [rain, camp, unmatched] = screen.getAllByTestId('direction-group');

    expect(
      within(rain).getByRole('link', { name: 'Go to experiments' })
    ).toHaveAttribute('href', '/w/acme/experiments');
    expect(
      within(camp).getByRole('button', { name: 'Go to experiments' })
    ).toBeDisabled();
    expect(
      within(camp).getByText(
        'Approve at least one image before taking it to a delivery experiment.'
      )
    ).toBeInTheDocument();

    expect(within(unmatched).getByTestId('group-review-action')).toBeDisabled();
    expect(within(unmatched).getByTestId('group-export-action')).toBeDisabled();
  });
});
