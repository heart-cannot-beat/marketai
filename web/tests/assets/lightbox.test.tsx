/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent } from '@testing-library/react';

// jsdom does not implement `HTMLDialogElement.showModal`/`close` -- polyfill
// them to toggle the `open` attribute so the dialog's contents are treated
// as visible/accessible by Testing Library's role queries, matching real
// browser behavior closely enough for these component tests.
if (!HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
}
if (!HTMLDialogElement.prototype.close) {
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.removeAttribute('open');
  };
}

const mockUseDomainQuery = jest.fn();
const mockUseAssetImageUrl = jest.fn();

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (...args: unknown[]) => mockUseDomainQuery(...args),
}));

jest.mock('@/hooks/assets/useAssetImageUrl', () => ({
  useAssetImageUrl: (...args: unknown[]) => mockUseAssetImageUrl(...args),
}));

jest.mock('@/components/assets/ReviewPanel', () => ({
  ReviewPanel: () => <div data-testid="review-panel" />,
}));

jest.mock('@/components/assets/RegenerationPanel', () => ({
  RegenerationPanel: () => <div data-testid="regeneration-panel" />,
}));

import { LightboxModal } from '@/components/assets/LightboxModal';

function makeDetail(overrides: Record<string, unknown> = {}) {
  return {
    id: 'v1',
    asset_id: 'a1',
    product_id: 'p1',
    run_id: 'r1',
    provider: 'openai',
    model: 'gpt-image-1',
    status: 'ready',
    kind: 'image',
    aspect_ratio: '1:1',
    seed: { requested: null, effective: 42, unavailable_reason: null },
    prompt: 'a red bicycle',
    execution_mode: 'real',
    origin_kind: 'generated',
    tags: {},
    effective_review_value: null,
    review_head_id: null,
    reviews: [],
    qc_attempts: [],
    latest_qc_state: null,
    latest_qc_attempt_id: null,
    contextual_conclusions: [],
    parent: null,
    children: [],
    mock_evidence: null,
    byte_size: 2048,
    mime_type: 'image/png',
    content_sha256: 'sha256-v1',
    producing_run_item_id: 'ri-1',
    provider_attempt_id: 'pa-1',
    allowed_actions: [],
    created_at: '2026-09-01T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUseAssetImageUrl.mockReturnValue({ blobUrl: null, isLoading: false });
});

describe('LightboxModal', () => {
  it('shows a loading spinner while the detail query is loading', () => {
    mockUseDomainQuery.mockReturnValue({
      data: undefined,
      isLoading: true,
      problem: undefined,
    });

    render(<LightboxModal versionId="v1" onClose={jest.fn()} />);

    expect(screen.getByTestId('asset-detail-title')).toHaveTextContent(
      'Asset details'
    );
    expect(document.querySelector('.loading-spinner')).toBeInTheDocument();
  });

  it('renders sidebar detail fields once the version loads', () => {
    mockUseDomainQuery.mockReturnValue({
      data: makeDetail(),
      isLoading: false,
      problem: undefined,
    });

    render(<LightboxModal versionId="v1" onClose={jest.fn()} />);

    expect(screen.getAllByText('openai').length).toBeGreaterThan(0);
    expect(screen.getAllByText('gpt-image-1').length).toBeGreaterThan(0);
    // The prompt stays verbatim under a Chinese heading.
    expect(screen.getByText('Prompt sent to the model')).toBeInTheDocument();
    expect(screen.getByText('a red bicycle')).toBeInTheDocument();
    expect(screen.getByText('Real')).toBeInTheDocument();
    expect(screen.getByText('Seed used: 42')).toBeInTheDocument();
    expect(screen.getByTestId('asset-detail-review')).toHaveTextContent(
      'Not reviewed'
    );
  });

  it('renders the full-size image once the blob URL resolves', () => {
    mockUseDomainQuery.mockReturnValue({
      data: makeDetail(),
      isLoading: false,
      problem: undefined,
    });
    mockUseAssetImageUrl.mockReturnValue({
      blobUrl: 'blob:test-url',
      isLoading: false,
    });

    const { container } = render(
      <LightboxModal versionId="v1" onClose={jest.fn()} />
    );

    // `alt=""` makes the image decorative (role "presentation"), so query
    // it directly rather than via `getByRole('img')`.
    const img = container.querySelector('img');
    expect(img).toHaveAttribute('src', 'blob:test-url');
  });

  it('shows an error message when the detail query returns a problem', () => {
    mockUseDomainQuery.mockReturnValue({
      data: undefined,
      isLoading: false,
      problem: {
        code: 'asset_not_found',
        title: 'Not found',
        status: 404,
        detail: '',
        instance: '',
        type: '',
        trace_id: '',
        retryable: false,
        field_errors: [],
      },
    });

    render(<LightboxModal versionId="v1" onClose={jest.fn()} />);

    expect(screen.getByText("The image didn't load")).toHaveAttribute(
      'title',
      'asset_not_found'
    );
    expect(screen.queryByText('asset_not_found')).not.toBeInTheDocument();
  });

  it('calls onClose when the close button is clicked', () => {
    mockUseDomainQuery.mockReturnValue({
      data: makeDetail(),
      isLoading: false,
      problem: undefined,
    });
    const onClose = jest.fn();

    render(<LightboxModal versionId="v1" onClose={onClose} />);

    fireEvent.click(
      screen.getByRole('button', { name: 'Close asset details' })
    );
    expect(onClose).toHaveBeenCalled();
  });

  it('calls onClose when Escape is pressed', () => {
    mockUseDomainQuery.mockReturnValue({
      data: makeDetail(),
      isLoading: false,
      problem: undefined,
    });
    const onClose = jest.fn();

    render(<LightboxModal versionId="v1" onClose={onClose} />);

    fireEvent.keyDown(screen.getByTestId('asset-detail-title'), {
      key: 'Escape',
    });
    expect(onClose).toHaveBeenCalled();
  });

  it('handles the native dialog cancel event', () => {
    mockUseDomainQuery.mockReturnValue({
      data: makeDetail(),
      isLoading: false,
      problem: undefined,
    });
    const onClose = jest.fn();

    render(<LightboxModal versionId="v1" onClose={onClose} />);

    fireEvent(
      screen.getByRole('dialog'),
      new Event('cancel', { cancelable: true })
    );
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('calls onPrev and onNext on arrow key navigation', () => {
    mockUseDomainQuery.mockReturnValue({
      data: makeDetail(),
      isLoading: false,
      problem: undefined,
    });
    const onPrev = jest.fn();
    const onNext = jest.fn();

    render(
      <LightboxModal
        versionId="v1"
        onClose={jest.fn()}
        onPrev={onPrev}
        onNext={onNext}
      />
    );

    fireEvent.keyDown(screen.getByTestId('asset-detail-title'), {
      key: 'ArrowLeft',
    });
    fireEvent.keyDown(screen.getByTestId('asset-detail-title'), {
      key: 'ArrowRight',
    });

    expect(onPrev).toHaveBeenCalled();
    expect(onNext).toHaveBeenCalled();
  });

  it('renders prev/next buttons only when handlers are provided', () => {
    mockUseDomainQuery.mockReturnValue({
      data: makeDetail(),
      isLoading: false,
      problem: undefined,
    });

    const { rerender } = render(
      <LightboxModal versionId="v1" onClose={jest.fn()} />
    );
    expect(
      screen.queryByRole('button', { name: 'Previous' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Next' })
    ).not.toBeInTheDocument();

    rerender(
      <LightboxModal
        versionId="v1"
        onClose={jest.fn()}
        onPrev={jest.fn()}
        onNext={jest.fn()}
      />
    );
    expect(
      screen.getByRole('button', { name: 'Previous' })
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next' })).toBeInTheDocument();
  });

  it('renders the review panel only when review is an allowed action', () => {
    mockUseDomainQuery.mockReturnValue({
      data: makeDetail({
        allowed_actions: [{ action: 'review', allowed: true }],
      }),
      isLoading: false,
      problem: undefined,
    });

    render(<LightboxModal versionId="v1" onClose={jest.fn()} />);

    expect(screen.getByTestId('review-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('regeneration-panel')).not.toBeInTheDocument();
  });

  it('renders the regeneration panel only when regenerate is an allowed action', () => {
    mockUseDomainQuery.mockReturnValue({
      data: makeDetail({
        allowed_actions: [{ action: 'regenerate', allowed: true }],
      }),
      isLoading: false,
      problem: undefined,
    });

    render(<LightboxModal versionId="v1" onClose={jest.fn()} />);

    expect(screen.getByTestId('regeneration-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('review-panel')).not.toBeInTheDocument();
  });

  it('navigates to a lineage version when clicked in the sidebar', () => {
    mockUseDomainQuery.mockReturnValue({
      data: makeDetail({
        parent: { version_id: 'v0', intent: 'new_seed' },
      }),
      isLoading: false,
      problem: undefined,
    });
    const onNavigate = jest.fn();

    render(
      <LightboxModal
        versionId="v1"
        onClose={jest.fn()}
        onNavigate={onNavigate}
      />
    );

    fireEvent.click(screen.getByTestId('lineage-parent'));
    expect(onNavigate).toHaveBeenCalledWith('v0');
  });

  it('shows the front-flow detail in the interface language without internal ids', () => {
    mockUseDomainQuery.mockReturnValue({
      data: makeDetail({
        id: 'av_0123456789',
        effective_review_value: 'needs_changes',
        parent: { version_id: 'av_parent00001', intent: 'new_seed' },
        children: [{ version_id: 'av_child000001', intent: 'edit_prompt' }],
        contextual_conclusions: [
          {
            decision_id: 'd1',
            verdict: 'winner',
            context: { experiment_id: 'exp_123' },
          },
        ],
        mock_evidence: { scenario_id: 'scn_internal', run_id: 'gr_935bb' },
        execution_mode: 'mock',
      }),
      isLoading: false,
      problem: undefined,
    });

    const { container } = render(
      <LightboxModal versionId="av_0123456789" onClose={jest.fn()} />
    );

    for (const heading of [
      'Provenance',
      'Random seed',
      'Review',
      'Versions',
      'Experiment conclusions',
    ]) {
      expect(screen.getByText(heading)).toBeInTheDocument();
    }
    expect(screen.getByTestId('asset-detail-review')).toHaveTextContent(
      'Needs changes'
    );
    expect(screen.getByText('Winner')).toHaveAttribute('title', 'exp_123');
    expect(screen.getByText('Example (controlled Mock)')).toBeInTheDocument();
    expect(screen.getByTestId('lineage-child')).toHaveTextContent(
      'Derived version 1'
    );
    expect(container).not.toHaveTextContent(
      /needs_changes|new_seed|edit_prompt/
    );
    expect(container).not.toHaveTextContent(/av_|gr_|exp_|scn_/);

    // Mock evidence (raw ids) only renders once the internal view opens.
    fireEvent.click(screen.getByRole('button', { name: 'Show internal view' }));
    expect(container).toHaveTextContent('gr_935bb');
  });
});
