/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent } from '@testing-library/react';
import { SelectionBar } from '@/components/assets/SelectionBar';

const mockClear = jest.fn();
const mockOnExport = jest.fn();
const mockOnReviewMode = jest.fn();

beforeEach(() => {
  jest.clearAllMocks();
});

describe('SelectionBar', () => {
  it('renders selection count', () => {
    render(
      <SelectionBar
        count={5}
        onClear={mockClear}
        onExport={mockOnExport}
        onReviewMode={mockOnReviewMode}
        canExport={true}
        canReview={true}
      />
    );

    expect(screen.getByText('5 selected')).toBeInTheDocument();
    expect(screen.getByTestId('selection-review')).toHaveTextContent('Review');
    expect(screen.getByTestId('selection-export')).toHaveTextContent(
      'Export assets'
    );
  });

  it('hides when count is zero', () => {
    const { container } = render(
      <SelectionBar
        count={0}
        onClear={mockClear}
        onExport={mockOnExport}
        onReviewMode={mockOnReviewMode}
        canExport={true}
        canReview={true}
      />
    );

    expect(container.firstChild).toBeNull();
  });

  it('calls onClear when clear button is clicked', () => {
    render(
      <SelectionBar
        count={3}
        onClear={mockClear}
        onExport={mockOnExport}
        onReviewMode={mockOnReviewMode}
        canExport={true}
        canReview={true}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }));
    expect(mockClear).toHaveBeenCalled();
  });

  it('disables export button when canExport is false', () => {
    render(
      <SelectionBar
        count={3}
        onClear={mockClear}
        onExport={mockOnExport}
        onReviewMode={mockOnReviewMode}
        canExport={false}
        canReview={true}
      />
    );

    expect(
      screen.getByRole('button', { name: 'Export selected assets' })
    ).toBeDisabled();
  });

  it('announces selection changes for screen readers', () => {
    render(
      <SelectionBar
        count={3}
        onClear={mockClear}
        onExport={mockOnExport}
        onReviewMode={mockOnReviewMode}
        canExport={true}
        canReview={true}
      />
    );

    const live = screen.getByRole('status');
    expect(live).toHaveAttribute('aria-live', 'polite');
  });
});
