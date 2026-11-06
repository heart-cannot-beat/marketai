/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent } from '@testing-library/react';
import { FilterBar } from '@/components/assets/FilterBar';

const mockSetFilter = jest.fn();

interface Facets {
  providers: Array<{ value: string; count: number }>;
  reviews: Array<{ value: string; count: number }>;
  origins: Array<{ value: string; count: number }>;
  modes: Array<{ value: string; count: number }>;
  aspects: Array<{ value: string; count: number }>;
  qc_states: Array<{ value: string; count: number }>;
  tags: Array<{ value: string; count: number }>;
}

const defaultFacets: Facets = {
  providers: [
    { value: 'openai', count: 42 },
    { value: 'stability', count: 18 },
  ],
  reviews: [
    { value: 'approved', count: 30 },
    { value: 'needs_changes', count: 8 },
    { value: 'unreviewed', count: 22 },
  ],
  origins: [],
  modes: [],
  aspects: [],
  qc_states: [],
  tags: [],
};

beforeEach(() => {
  jest.clearAllMocks();
});

describe('FilterBar', () => {
  it('renders provider filter chips from facets', () => {
    render(
      <FilterBar
        facets={defaultFacets}
        activeFilters={{}}
        onFilterChange={mockSetFilter}
      />
    );

    expect(
      screen.getByRole('checkbox', { name: /openai/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('checkbox', { name: /stability/i })
    ).toBeInTheDocument();
  });

  it('shows active state with aria-checked', () => {
    render(
      <FilterBar
        facets={defaultFacets}
        activeFilters={{ provider: 'openai' }}
        onFilterChange={mockSetFilter}
      />
    );

    const chip = screen.getByRole('checkbox', { name: /openai/i });
    expect(chip).toHaveAttribute('aria-checked', 'true');
  });

  it('calls onFilterChange when a chip is toggled on', () => {
    render(
      <FilterBar
        facets={defaultFacets}
        activeFilters={{}}
        onFilterChange={mockSetFilter}
      />
    );

    fireEvent.click(screen.getByRole('checkbox', { name: /openai/i }));
    expect(mockSetFilter).toHaveBeenCalledWith('provider', 'openai');
  });

  it('calls onFilterChange with undefined when active chip is toggled off', () => {
    render(
      <FilterBar
        facets={defaultFacets}
        activeFilters={{ provider: 'openai' }}
        onFilterChange={mockSetFilter}
      />
    );

    fireEvent.click(screen.getByRole('checkbox', { name: /openai/i }));
    expect(mockSetFilter).toHaveBeenCalledWith('provider', undefined);
  });

  it('renders review filter chips with counts', () => {
    render(
      <FilterBar
        facets={defaultFacets}
        activeFilters={{}}
        onFilterChange={mockSetFilter}
      />
    );

    expect(
      screen.getByRole('checkbox', { name: 'Approved 30' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('checkbox', { name: 'Needs changes 8' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('checkbox', { name: 'Not reviewed 22' })
    ).toBeInTheDocument();
    expect(screen.queryByText(/approved|needs_changes|unreviewed/)).toBeNull();
  });

  it('labels mode, origin and QC facets in the interface language but filters by the raw value', () => {
    render(
      <FilterBar
        facets={{
          ...defaultFacets,
          origins: [{ value: 'generation', count: 3 }],
          modes: [
            { value: 'byok', count: 2 },
            { value: 'mock', count: 1 },
          ],
          qc_states: [
            { value: 'failed_nonblocking', count: 1 },
            { value: 'some_new_state', count: 1 },
          ],
        }}
        activeFilters={{}}
        onFilterChange={mockSetFilter}
      />
    );

    expect(
      screen.getByTestId('filter-originKind-generation')
    ).toHaveTextContent('Generated here3');
    expect(screen.getByTestId('filter-executionMode-mock')).toHaveTextContent(
      'Example (controlled Mock)1'
    );
    expect(
      screen.getByTestId('filter-qcState-failed_nonblocking')
    ).toHaveTextContent('Failed (warning)1');
    expect(
      screen.getByTestId('filter-qcState-some_new_state')
    ).toHaveTextContent('QC status unknown1');

    fireEvent.click(screen.getByTestId('filter-executionMode-byok'));
    expect(mockSetFilter).toHaveBeenCalledWith('executionMode', 'byok');
  });
});
