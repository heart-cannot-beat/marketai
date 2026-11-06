/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { CostDisplay } from '@/components/generation/shared/CostDisplay';

describe('CostDisplay', () => {
  it('formats USD cost with 2 decimal places', () => {
    render(<CostDisplay amount="12.50" currency="USD" />);
    expect(screen.getByText('$12.50')).toBeInTheDocument();
  });

  it('formats EUR cost', () => {
    render(<CostDisplay amount="99.99" currency="EUR" />);
    expect(screen.getByText('€99.99')).toBeInTheDocument();
  });

  it('renders null amount as dash', () => {
    render(<CostDisplay amount={null} currency="USD" />);
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});
