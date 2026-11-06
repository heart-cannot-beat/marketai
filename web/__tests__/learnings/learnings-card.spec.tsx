/**
 * @jest-environment jsdom
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { LearningsCard } from '@/components/learnings/LearningsCard';
import {
  loadLearningsNamespace,
  noDataLearnings,
  readyLearnings,
  WIN_SENTENCE,
} from './fixtures';

let mockQuery: any;
const mockGateway = {
  createTestPlan: jest.fn(),
  createResultsImport: jest.fn(),
  commitResultsImport: jest.fn(),
  getResultsImport: jest.fn(),
  getResultsTemplate: jest.fn(),
  pollOperation: jest.fn(),
};

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: () => mockQuery,
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('next/router', () => ({
  useRouter: () => ({ push: jest.fn() }),
}));
jest.mock('@/hooks/domain/useUpload', () => ({
  useUpload: () => ({ startUpload: jest.fn(), state: 'idle', hashProgress: 0 }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'MEMBER' } }),
}));

const INTERNAL_ID = /prod_|lrn_|atp_|av_|people-\d|scene-\d|dir-\d/;

function query(data: unknown, extra: Record<string, unknown> = {}) {
  return {
    data,
    problem: undefined,
    isLoading: false,
    isStale: false,
    lastSuccessfulAt: null,
    refresh: jest.fn(),
    ...extra,
  };
}

beforeAll(loadLearningsNamespace);
beforeEach(() => {
  Object.values(mockGateway).forEach((fn) => fn.mockReset());
});

describe('LearningsCard', () => {
  it('says plainly that there is no ad data and shows review as a weak signal', () => {
    mockQuery = query(noDataLearnings());
    render(<LearningsCard productId="prod_1" />);
    expect(
      screen.getByRole('heading', {
        name: 'What we have learned about this product',
      })
    ).toBeInTheDocument();
    expect(screen.getByText('No delivery data yet')).toBeInTheDocument();
    const signals = screen.getByTestId('review-signals');
    expect(
      // The server's fixed Chinese `note` is not shown; the copy is ours.
      within(signals).getByText(
        'These are weak signals from human review, not delivery data'
      )
    ).toBeInTheDocument();
    expect(signals).not.toHaveTextContent('这是人工审核的弱信号');
    expect(
      within(signals).getByText('3 approved / 4 images')
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', {
        name: 'Generate the next round from these conclusions',
      })
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Import delivery results' })
    ).toBeInTheDocument();
  });

  it('shows each value with a verdict chip, an interval strip and its sentence -- never an id', () => {
    mockQuery = query(readyLearnings());
    const { container } = render(<LearningsCard productId="prod_1" />);
    expect(screen.getAllByText(WIN_SENTENCE).length).toBeGreaterThanOrEqual(2); // highlight + row
    const rows = screen.getAllByTestId('learning-row');
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByText('Better')).toBeInTheDocument();
    expect(within(rows[0]).getByText('CTR +78%')).toBeInTheDocument();
    expect(within(rows[1]).getByText('Worse')).toBeInTheDocument();
    expect(within(rows[2]).getByText('Not enough data')).toBeInTheDocument();
    // Insufficient data: a dashed track and no interval, never a number.
    expect(
      within(rows[2]).queryByTestId('lift-interval')
    ).not.toBeInTheDocument();
    expect(within(rows[0]).getByTestId('lift-interval')).toBeInTheDocument();
    expect(
      within(rows[0]).getByRole('img', { name: WIN_SENTENCE })
    ).toBeInTheDocument();
    // The winner's whole interval sits right of 持平 (50%).
    const point = within(rows[0]).getByTestId('lift-point') as HTMLElement;
    expect(parseFloat(point.style.left)).toBeGreaterThan(50);
    const interval = within(rows[0]).getByTestId(
      'lift-interval'
    ) as HTMLElement;
    expect(parseFloat(interval.style.left)).toBeGreaterThan(50);
    expect(screen.getByText('Round 1')).toBeInTheDocument();
    expect(container.textContent).not.toMatch(INTERNAL_ID);
  });

  it('labels demo data and keeps it apart', () => {
    mockQuery = query(
      readyLearnings({
        evidence_mode: 'mock',
        evidence_notice:
          '演示数据：这些素材由受控 Mock 生成，结论只用于体验流程，不代表真实投放效果',
      })
    );
    render(<LearningsCard productId="prod_1" />);
    // Worded by `evidence_mode`; the server's Chinese notice is not shown.
    expect(screen.getByRole('note')).toHaveTextContent(
      'Demo data: these images were made by controlled Mock'
    );
    expect(screen.getByRole('note')).not.toHaveTextContent('演示数据');
  });

  it('starts the next round from the snapshot with one click', async () => {
    mockQuery = query(readyLearnings());
    mockGateway.createTestPlan.mockResolvedValue({
      kind: 'accepted',
      status: 202,
      data: {
        plan_id: 'atp_2',
        status: 'drafting',
        revision: 1,
        replayed: false,
      },
      meta: {},
    });
    const onCreated = jest.fn();
    render(<LearningsCard productId="prod_1" onNextRoundCreated={onCreated} />);
    await act(async () => {
      fireEvent.click(
        screen.getByRole('button', {
          name: 'Generate the next round from these conclusions',
        })
      );
    });
    expect(mockGateway.createTestPlan).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        body: {
          product_id: 'prod_1',
          goal: 'optimize',
          scale: 'sample',
          from_learnings_snapshot_id: 'lrn_1',
        },
      })
    );
    expect(onCreated).toHaveBeenCalledWith('atp_2');
    expect(screen.getByRole('status')).toHaveTextContent(
      '1 winning elements kept, 1 losing elements skipped'
    );
  });

  it('cannot seed a round from insufficient data', () => {
    mockQuery = query(
      readyLearnings({ can_seed_next_round: false, highlights: [] })
    );
    render(<LearningsCard productId="prod_1" />);
    expect(
      screen.getByRole('button', {
        name: 'Generate the next round from these conclusions',
      })
    ).toBeDisabled();
    expect(
      screen.getByText('Not enough data for a usable conclusion yet')
    ).toBeInTheDocument();
  });

  it('offers a retry when the learnings did not load', () => {
    const refresh = jest.fn();
    mockQuery = query(undefined, {
      problem: { status: 500, code: 'internal', detail: 'x' },
      refresh,
    });
    render(<LearningsCard productId="prod_1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(refresh).toHaveBeenCalled();
  });
});
