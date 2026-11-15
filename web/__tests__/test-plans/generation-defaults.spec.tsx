/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { GenerationDefaultsBanner } from '@/components/test-plans/GenerationDefaultsBanner';

let mockRole = 'OWNER';
let mockDefaults: any;
const mockRefresh = jest.fn();

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: mockRole } }),
}));
jest.mock('@/hooks/test-plans/useGenerationDefaults', () => ({
  useGenerationDefaults: () => ({
    defaults: mockDefaults,
    isLoading: false,
    problem: undefined,
    refresh: mockRefresh,
  }),
}));

const COMPUTED_MOCK = {
  provider: 'mock',
  model: 'mock-v1',
  execution_mode: 'mock',
  aspect_ratio: '1:1',
  variants: { sample: 1, small: 2, full: 2 },
  source: 'computed',
  revision: 0,
  credential_bound: true,
  warnings: ['mock_mode'],
};

beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'OWNER';
  mockDefaults = COMPUTED_MOCK;
});

/**
 * The banner stays only on the read-only legacy `/tests` list. Settings no
 * longer edits these defaults and the product home no longer shows the
 * banner (#181).
 */
describe('GenerationDefaultsBanner', () => {
  it('links the Owner to provider credentials in Mock mode', () => {
    render(<GenerationDefaultsBanner />);
    const banner = screen.getByTestId('generation-defaults-banner');
    expect(banner).toHaveTextContent('Example mode');
    expect(
      screen.getByRole('link', { name: 'Bind an OpenAI key' })
    ).toHaveAttribute('href', '/w/acme/settings#provider-credentials');
  });

  it('tells a Member to ask the Owner when the key is missing', () => {
    mockRole = 'MEMBER';
    mockDefaults = {
      ...COMPUTED_MOCK,
      execution_mode: 'byok',
      warnings: ['byok_credential_missing'],
    };
    render(<GenerationDefaultsBanner />);
    expect(screen.getByTestId('generation-defaults-banner')).toHaveTextContent(
      'no usable key is bound yet'
    );
    expect(screen.queryByRole('link')).toBeNull();
    expect(
      screen.getByText(
        'Ask the workspace Owner to bind an OpenAI key in Settings.'
      )
    ).toBeInTheDocument();
  });

  it('stays out of the way with a bound key', () => {
    mockDefaults = { ...COMPUTED_MOCK, execution_mode: 'byok', warnings: [] };
    const { container } = render(<GenerationDefaultsBanner />);
    expect(container).toBeEmptyDOMElement();
  });
});
