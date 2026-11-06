/**
 * @jest-environment jsdom
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { GenerationDefaultsSection } from '@/components/test-plans/GenerationDefaultsSection';
import { GenerationDefaultsBanner } from '@/components/test-plans/GenerationDefaultsBanner';

let mockRole = 'OWNER';
let mockDefaults: any;
const mockRefresh = jest.fn();
const mockGateway = { updateGenerationDefaults: jest.fn() };

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: mockRole } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/hooks/test-plans/useGenerationDefaults', () => ({
  useGenerationDefaults: () => ({
    defaults: mockDefaults,
    isLoading: false,
    problem: undefined,
    refresh: mockRefresh,
  }),
}));
jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: () => ({
    data: {
      models: {
        mock: {
          'mock-v1': {
            display_name: 'Mock Generator v1',
            format_limits: {
              aspect_ratios: ['1:1', '9:16'],
              max_variants_per_combination: 4,
            },
          },
        },
        openai: {
          'gpt-image-2': {
            display_name: 'GPT Image 2',
            format_limits: {
              aspect_ratios: ['1:1', '4:5', '9:16'],
              max_variants_per_combination: 3,
            },
          },
          'gpt-image-1': {
            display_name: 'GPT Image 1',
            format_limits: {
              aspect_ratios: ['1:1'],
              max_variants_per_combination: 2,
            },
          },
        },
      },
    },
    isLoading: false,
    refresh: jest.fn(),
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

describe('GenerationDefaultsSection', () => {
  it('lets the Owner switch to OpenAI and saves with If-Match 0 first', async () => {
    mockGateway.updateGenerationDefaults.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: { ...COMPUTED_MOCK, revision: 1 },
      meta: {},
    });
    render(<GenerationDefaultsSection />);
    expect(screen.getByText('Recommended, not saved yet')).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole('radio', { name: "OpenAI (the workspace's own key)" })
    );
    fireEvent.change(screen.getByLabelText('Model'), {
      target: { value: 'gpt-image-2' },
    });
    fireEvent.click(screen.getByRole('radio', { name: 'Stories 9:16' }));
    fireEvent.change(screen.getByLabelText('Small test'), {
      target: { value: '3' },
    });
    await act(async () => {
      fireEvent.click(
        screen.getByRole('button', { name: 'Save generation defaults' })
      );
    });

    expect(mockGateway.updateGenerationDefaults).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        ifMatch: 0,
        body: {
          provider: 'openai',
          model: 'gpt-image-2',
          execution_mode: 'byok',
          aspect_ratio: '9:16',
          variants: { sample: 1, small: 3, full: 2 },
        },
      })
    );
    expect(mockRefresh).toHaveBeenCalled();
  });

  it('offers only what the chosen model supports', () => {
    render(<GenerationDefaultsSection />);
    // Mock: no 4:5.
    expect(screen.queryByRole('radio', { name: 'Portrait 4:5' })).toBeNull();
    fireEvent.click(
      screen.getByRole('radio', { name: "OpenAI (the workspace's own key)" })
    );
    expect(
      screen.getByRole('radio', { name: 'Portrait 4:5' })
    ).toBeInTheDocument();
    expect(screen.getAllByRole('option', { name: /image/ }).length).toBe(9); // three scales x three variants (GPT Image 2 allows 3)
  });

  it('a revision conflict is explained and the defaults reload', async () => {
    mockDefaults = { ...COMPUTED_MOCK, source: 'workspace', revision: 2 };
    mockGateway.updateGenerationDefaults.mockResolvedValue({
      kind: 'problem',
      status: 409,
      problem: { code: 'revision_conflict', status: 409, detail: 'x' },
      meta: {},
    });
    render(<GenerationDefaultsSection />);
    fireEvent.click(screen.getByRole('radio', { name: 'Stories 9:16' }));
    await act(async () => {
      fireEvent.click(
        screen.getByRole('button', { name: 'Save generation defaults' })
      );
    });
    expect(mockGateway.updateGenerationDefaults.mock.calls[0][0].ifMatch).toBe(
      2
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      'The plan was just changed elsewhere'
    );
    expect(mockRefresh).toHaveBeenCalled();
  });

  it('shows Members the same values read-only', () => {
    mockRole = 'MEMBER';
    render(<GenerationDefaultsSection />);
    expect(
      screen.getByText('Only the Owner can change the generation defaults.')
    ).toBeInTheDocument();
    expect(
      screen.getByRole('radio', { name: 'Example mode (no charge)' })
    ).toBeChecked();
    expect(
      screen.getByRole('radio', { name: 'Example mode (no charge)' })
    ).toBeDisabled();
    expect(
      screen.queryByRole('button', { name: 'Save generation defaults' })
    ).not.toBeInTheDocument();
  });
});

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
