/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { PreviewValidity } from '@/components/generation/preview/PreviewValidity';

describe('PreviewValidity', () => {
  it('shows valid state when not expired and not invalidated', () => {
    const future = new Date(Date.now() + 600_000).toISOString();
    render(<PreviewValidity validUntil={future} invalidationReason={null} />);
    expect(screen.getByText(/valid for/i)).toBeInTheDocument();
  });

  it('shows expired state when past valid_until', () => {
    const past = new Date(Date.now() - 1000).toISOString();
    render(<PreviewValidity validUntil={past} invalidationReason={null} />);
    expect(screen.getByText(/expired/i)).toBeInTheDocument();
  });

  it('shows invalidation reason when present', () => {
    const future = new Date(Date.now() + 600_000).toISOString();
    render(
      <PreviewValidity
        validUntil={future}
        invalidationReason="Draft was modified after preview creation"
      />
    );
    expect(screen.getByText(/draft was modified/i)).toBeInTheDocument();
  });

  it('treats a null valid_until as not expired when there is no invalidation reason', () => {
    render(<PreviewValidity validUntil={null} invalidationReason={null} />);
    expect(screen.queryByText(/expired/i)).not.toBeInTheDocument();
  });
});
