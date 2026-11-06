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

import { RegenerationPanel } from '@/components/assets/RegenerationPanel';

beforeEach(() => {
  jest.clearAllMocks();
  mockExecute.mockResolvedValue({
    kind: 'success',
    data: { draft_id: 'd1' },
    meta: { etag: null, retryAt: null, location: null },
  });
});

describe('RegenerationPanel', () => {
  it('renders four regeneration intent buttons', () => {
    render(<RegenerationPanel versionId="v1" latestQcAttemptId="qc-1" />);

    expect(
      screen.getByRole('button', { name: 'Another one from the same recipe' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'New random seed' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Edit the prompt' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Fix per QC feedback' })
    ).toBeInTheDocument();
  });

  it('submits same_recipe intent', async () => {
    render(<RegenerationPanel versionId="v1" />);

    fireEvent.click(
      screen.getByRole('button', { name: 'Another one from the same recipe' })
    );

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          pathParams: { version_id: 'v1' },
          body: expect.objectContaining({
            intent: 'same_recipe',
          }),
        })
      );
    });
  });

  it('submits new_seed intent', async () => {
    render(<RegenerationPanel versionId="v1" />);

    fireEvent.click(screen.getByRole('button', { name: 'New random seed' }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          pathParams: { version_id: 'v1' },
          body: expect.objectContaining({
            intent: 'new_seed',
          }),
        })
      );
    });
  });

  it('submits edit_prompt intent', async () => {
    render(<RegenerationPanel versionId="v1" />);

    fireEvent.click(screen.getByRole('button', { name: 'Edit the prompt' }));

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          pathParams: { version_id: 'v1' },
          body: expect.objectContaining({
            intent: 'edit_prompt',
          }),
        })
      );
    });
  });

  it('submits quality_feedback intent', async () => {
    render(<RegenerationPanel versionId="v1" latestQcAttemptId="qc-1" />);

    fireEvent.click(
      screen.getByRole('button', { name: 'Fix per QC feedback' })
    );

    await waitFor(() => {
      expect(mockExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          pathParams: { version_id: 'v1' },
          body: expect.objectContaining({
            intent: 'quality_feedback',
            qc_attempt_id: 'qc-1',
          }),
        })
      );
    });
  });

  it('hides the quality feedback button when there is no latest QC attempt', () => {
    render(<RegenerationPanel versionId="v1" latestQcAttemptId={null} />);

    expect(
      screen.queryByRole('button', { name: 'Fix per QC feedback' })
    ).not.toBeInTheDocument();
  });

  it('disables buttons while submitting', async () => {
    mockExecute.mockReturnValue(new Promise(() => {}));
    render(<RegenerationPanel versionId="v1" />);

    fireEvent.click(
      screen.getByRole('button', { name: 'Another one from the same recipe' })
    );

    expect(
      screen.getByRole('button', { name: 'Another one from the same recipe' })
    ).toBeDisabled();
    expect(
      screen.getByRole('button', { name: 'New random seed' })
    ).toBeDisabled();
  });
});
