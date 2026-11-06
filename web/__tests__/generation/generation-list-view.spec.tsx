/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { DraftList } from '@/components/generation/list/DraftList';

jest.mock('@/components/generation/shared/useGenerationNav', () => ({
  useGenerationNav: () => ({
    buildDraftUrl: (id: string) => `/w/ws/generate/drafts/${id}`,
  }),
}));

jest.mock('@/components/generation/shared/ExecutionModeTag', () => ({
  ExecutionModeTag: ({ mode }: { mode: string }) => (
    <span data-testid="mode">{mode}</span>
  ),
}));

describe('DraftList', () => {
  const drafts = [
    {
      id: 'draft-1',
      title: 'Summer Campaign',
      execution_mode: 'byok',
      updated_at: '2026-09-01T12:00:00Z',
      allowed_actions: [],
    },
    {
      id: 'draft-2',
      title: 'Holiday Promo',
      execution_mode: 'mock',
      updated_at: '2026-09-02T12:00:00Z',
      allowed_actions: [],
    },
  ];

  it('renders draft names as links', () => {
    render(<DraftList drafts={drafts} />);
    expect(screen.getByText('Summer Campaign')).toBeInTheDocument();
    expect(screen.getByText('Holiday Promo')).toBeInTheDocument();
    expect(screen.getByText('Summer Campaign').closest('a')).toHaveAttribute(
      'href',
      '/w/ws/generate/drafts/draft-1'
    );
  });

  it('shows execution mode tag per draft', () => {
    render(<DraftList drafts={drafts} />);
    const modes = screen.getAllByTestId('mode');
    expect(modes).toHaveLength(2);
    expect(modes[0]).toHaveTextContent('byok');
    expect(modes[1]).toHaveTextContent('mock');
  });

  it('shows empty state when no drafts', () => {
    render(<DraftList drafts={[]} />);
    expect(screen.getByText(/no drafts/i)).toBeInTheDocument();
  });
});
