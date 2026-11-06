/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockExecute = jest.fn();
jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: mockExecute,
    result: undefined,
    problem: undefined,
    isPending: false,
    reset: jest.fn(),
    retry: jest.fn(),
    reconciliation: undefined,
  }),
}));

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme' } }),
}));

import { ReviewPanel } from '@/components/assets/ReviewPanel';

beforeEach(() => {
  jest.clearAllMocks();
  mockExecute.mockResolvedValue({
    kind: 'success',
    data: { id: 'review-1', review_value: 'approved' },
    meta: { etag: null, retryAt: null, location: null },
  });
});

describe('ReviewPanel', () => {
  it('renders A/C/X buttons when no review exists', () => {
    render(
      <ReviewPanel versionId="v1" reviewHeadId={null} effectiveReview={null} />
    );

    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Needs changes' })
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument();
  });

  it('keeps the A / C / X keys beside the translated verdicts', () => {
    render(
      <ReviewPanel versionId="v1" reviewHeadId={null} effectiveReview={null} />
    );

    expect(screen.getByTestId('review-approve')).toHaveTextContent('AApprove');
    expect(screen.getByTestId('review-needs-changes')).toHaveTextContent(
      'CNeeds changes'
    );
    expect(screen.getByTestId('review-reject')).toHaveTextContent('XReject');
    expect(
      screen.getByRole('group', { name: 'Review actions' })
    ).toBeInTheDocument();
  });

  it('submits review without supersedes_review_id for first review', async () => {
    render(
      <ReviewPanel versionId="v1" reviewHeadId={null} effectiveReview={null} />
    );

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          pathParams: { version_id: 'v1' },
          body: expect.objectContaining({
            review_value: 'approved',
          }),
        })
      );
    });

    const call = mockExecute.mock.calls[0][0];
    expect(call.body.supersedes_review_id).toBeUndefined();
  });

  it('submits correction with supersedes_review_id when head exists', async () => {
    render(
      <ReviewPanel
        versionId="v1"
        reviewHeadId="head-1"
        effectiveReview="approved"
      />
    );

    fireEvent.click(screen.getByRole('button', { name: 'Needs changes' }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          pathParams: { version_id: 'v1' },
          body: expect.objectContaining({
            review_value: 'needs_changes',
            supersedes_review_id: 'head-1',
          }),
        })
      );
    });
  });

  it('shows clear button when review exists', () => {
    render(
      <ReviewPanel
        versionId="v1"
        reviewHeadId="head-1"
        effectiveReview="approved"
      />
    );

    expect(
      screen.getByRole('button', { name: 'Clear review' })
    ).toBeInTheDocument();
  });

  it('submits cleared value with supersedes_review_id', async () => {
    render(
      <ReviewPanel
        versionId="v1"
        reviewHeadId="head-1"
        effectiveReview="approved"
      />
    );

    fireEvent.click(screen.getByRole('button', { name: 'Clear review' }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          pathParams: { version_id: 'v1' },
          body: expect.objectContaining({
            review_value: 'cleared',
            supersedes_review_id: 'head-1',
          }),
        })
      );
    });
  });
});
