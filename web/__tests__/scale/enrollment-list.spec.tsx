/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent } from '@testing-library/react';
import { EnrollmentList } from '@/components/scale/list/EnrollmentList';

jest.mock('next-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (key: string, opts?: { defaultValue?: string }) =>
      opts?.defaultValue ? opts.defaultValue : `${ns}:${key}`,
    i18n: { language: 'en' },
  }),
}));

jest.mock('@/hooks/scale/useScaleNav', () => ({
  useScaleNav: () => ({
    buildEnrollmentUrl: (id: string) => `/w/ws/scale/${id}`,
    buildTargetsUrl: () => '/w/ws/scale/targets',
  }),
}));

jest.mock('@/lib/scale/meta-mode', () => ({
  ...jest.requireActual('@/lib/scale/meta-mode'),
  useScaleMetaMode: () => ({
    metaMode: 'customer_sandbox',
    setMetaMode: jest.fn(),
  }),
}));

jest.mock('@/components/scale/list/CandidateSelector', () => ({
  CandidateSelector: ({ open }: { open: boolean }) =>
    open ? <div data-testid="candidate-selector" /> : null,
}));

jest.mock('@/components/scale/shared/MetaModeSelect', () => ({
  MetaModeSelect: () => <select data-testid="meta-mode-select" />,
}));

const mockUseScaleEnrollments = jest.fn();
jest.mock('@/hooks/scale/useScaleEnrollments', () => ({
  useScaleEnrollments: (...args: unknown[]) => mockUseScaleEnrollments(...args),
}));

function baseListResult(overrides = {}) {
  return {
    items: [],
    hasMore: false,
    isLoading: false,
    isLoadingMore: false,
    isRefreshing: false,
    problem: undefined,
    loadMore: jest.fn(),
    refresh: jest.fn(),
    ...overrides,
  };
}

describe('EnrollmentList', () => {
  beforeEach(() => {
    mockUseScaleEnrollments.mockReset();
  });

  it('shows an empty state with no enrollments', () => {
    mockUseScaleEnrollments.mockReturnValue(baseListResult());
    render(<EnrollmentList />);
    expect(screen.getByText('scale:list.empty-title')).toBeInTheDocument();
  });

  it('renders enrollment rows with their lifecycle', () => {
    mockUseScaleEnrollments.mockReturnValue(
      baseListResult({
        items: [
          {
            id: 'enr-1',
            lifecycle: 'active',
            market_code: 'US',
            updated_at: '2026-09-01T00:00:00Z',
          },
        ],
      })
    );
    render(<EnrollmentList />);
    expect(screen.getByText('enr-1')).toBeInTheDocument();
    expect(screen.getByText('enr-1').closest('a')).toHaveAttribute(
      'href',
      '/w/ws/scale/enr-1'
    );
    expect(screen.getByText('US')).toBeInTheDocument();
  });

  it('shows an error state with a retry action when the list fails', () => {
    const refresh = jest.fn();
    mockUseScaleEnrollments.mockReturnValue(
      baseListResult({
        problem: { detail: 'boom' },
        refresh,
      })
    );
    render(<EnrollmentList />);
    fireEvent.click(screen.getByText('scale:list.retry'));
    expect(refresh).toHaveBeenCalled();
  });

  it('opens the candidate selector from the "new enrollment" button', () => {
    mockUseScaleEnrollments.mockReturnValue(
      baseListResult({
        items: [
          {
            id: 'enr-1',
            lifecycle: 'active',
            market_code: 'US',
            updated_at: '2026-09-01T00:00:00Z',
          },
        ],
      })
    );
    render(<EnrollmentList />);
    expect(screen.queryByTestId('candidate-selector')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('scale:list.new-enrollment'));
    expect(screen.getByTestId('candidate-selector')).toBeInTheDocument();
  });
});
