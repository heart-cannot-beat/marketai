/**
 * @jest-environment jsdom
 */
import type { ReactNode } from 'react';
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { SWRConfig } from 'swr';
import { ReadingPage } from '@/components/ideas/ReadingPage';
import type { FirstBatch } from '@/lib/ideas/first-batch-view';
import { ideasDetail } from './fixtures';

/**
 * The reading page (ADR 0018, design §5.2, #195): the first batch's progress,
 * what was read with where it was read, and what to do when it stops. Once
 * the ads are made, the product list is refreshed before the ad ideas open.
 */
const mockReplace = jest.fn().mockResolvedValue(true);
const mockPush = jest.fn().mockResolvedValue(true);
jest.mock('next/router', () => ({
  useRouter: () => ({
    asPath: '/w/acme/start/mfb_1',
    query: { batchId: 'mfb_1' },
    push: mockPush,
    replace: mockReplace,
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'ws_1', role: 'OWNER' },
  }),
}));
const mockGateway = {
  getMarketingFirstBatch: jest.fn(),
  resumeMarketingFirstBatch: jest.fn(),
  getMarketingIdeas: jest.fn(),
  getProductIntake: jest.fn(),
  getMarketingFirstBatchQuote: jest.fn(),
  startMarketingFirstBatch: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/hooks/domain/useUpload', () => ({
  useUpload: () => ({ startUpload: jest.fn() }),
}));
const mockRefreshProducts = jest.fn().mockResolvedValue(undefined);
jest.mock('@/hooks/products/useWorkspaceProducts', () => ({
  useWorkspaceProducts: () => ({ refresh: mockRefreshProducts }),
}));

function ok<T>(data: T, status = 200, kind = 'ok') {
  return { kind, status, data, meta: {} };
}

function batch(extra: Partial<FirstBatch> = {}): FirstBatch {
  return {
    id: 'mfb_1',
    operation_id: 'op_secret',
    state: 'reading',
    needs_attention: false,
    attention: null,
    resume: null,
    intake_id: 'pi_secret',
    product_id: null,
    project_id: null,
    market: 'US',
    ad_language: 'en-US',
    strategy_language: 'en-US',
    execution_mode: 'byok',
    selected_variant: null,
    confirmed_max_cost: '0.8625',
    reserved_total: '0.0125',
    currency: 'USD',
    image_provider: 'openai',
    image_model: 'image-model',
    ads_planned: null,
    created_at: '2026-11-17T08:00:00+00:00',
    updated_at: '2026-11-17T08:00:00+00:00',
    ...extra,
  };
}

const READ = {
  product_id: 'prod_home',
  project_id: 'mp_secret',
};

function intake(
  input = {
    url: 'https://acmecharge.example/products/home',
    text_chars: 0,
    upload_count: 0,
  }
) {
  return ok({
    intake_id: 'pi_secret',
    status: 'ready',
    execution_mode: 'byok',
    input,
    card: { name: 'Acme Charge Home', requires_variant_selection: false },
    replayed: false,
  });
}

const RESUME = {
  maximum_cost: '0.95',
  additional_cost: '0.0875',
  currency: 'USD' as const,
  quote_hash: 'rq_1',
  requires: [] as ('selected_variant' | 'additional_text')[],
};

function Fresh({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

async function renderPage(first: FirstBatch) {
  mockGateway.getMarketingFirstBatch.mockResolvedValue(ok(first));
  const view = render(
    <Fresh>
      <ReadingPage batchId="mfb_1" />
    </Fresh>
  );
  await screen.findByRole('heading', { level: 1 });
  return view;
}

const stepStates = () =>
  within(screen.getByTestId('reading-progress'))
    .getAllByRole('listitem')
    .map((item) => item.getAttribute('data-state'));

beforeEach(() => {
  jest.clearAllMocks();
  mockGateway.getProductIntake.mockResolvedValue(intake());
  mockGateway.getMarketingIdeas.mockResolvedValue(ok(ideasDetail()));
  mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(
    ok({
      available: true,
      unavailable_reason: null,
      execution_mode: 'byok',
      maximum_cost: '0.8625',
      currency: 'USD',
      quote_hash: 'qh_1',
      image_provider: 'openai',
      image_model: 'image-model',
      steps: [],
    })
  );
});

describe('ReadingPage (#195)', () => {
  it('shows the progress while reading, and that the page can be closed', async () => {
    await renderPage(batch());
    expect(
      screen.getByRole('heading', {
        level: 1,
        name: 'Reading Acme Charge Home',
      })
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'You can close this page; it keeps going. Come back any time to see how far it got.'
      )
    ).toBeInTheDocument();
    expect(stepStates()).toEqual(['active', 'pending', 'pending', 'pending']);
    expect(
      screen.getByText(
        'Facts about the product appear here as soon as they are read.'
      )
    ).toBeInTheDocument();
    expect(mockGateway.getMarketingFirstBatch).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      firstBatchId: 'mfb_1',
    });
    expect(screen.queryByTestId('reading-stop')).toBeNull();
    // Internal ids never show.
    expect(document.body.textContent).not.toMatch(/mfb_|pi_|mp_|prod_|op_/);
  });

  it('lists the facts read, each with its source, and the claims once there are some', async () => {
    await renderPage(batch({ ...READ, state: 'ideas' }));
    const facts = await screen.findByTestId('reading-facts');
    expect(
      within(facts).getByText('Schedule charging in the Acme Charge app')
    ).toBeInTheDocument();
    expect(
      within(facts).getByText(
        'Source: “Schedule charging from the Acme Charge app.”'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "None of these is checked yet. That doesn't hold anything up; you check them once before downloading."
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText('Plug in at night and forget about it')
    ).toBeInTheDocument();
    expect(stepStates()).toEqual(['done', 'done', 'active', 'pending']);
    // The new product is put in the sidebar as soon as it exists.
    await waitFor(() => expect(mockRefreshProducts).toHaveBeenCalled());
  });

  it('says a fact came from the uploaded images when only images were pasted', async () => {
    mockGateway.getProductIntake.mockResolvedValue(
      intake({ url: null as unknown as string, text_chars: 0, upload_count: 2 })
    );
    await renderPage(batch({ ...READ, state: 'ideas' }));
    expect(
      await screen.findByText(
        'From your uploaded images: “Schedule charging from the Acme Charge app.”'
      )
    ).toBeInTheDocument();
  });

  it('counts the ads by what was planned, never a fixed six', async () => {
    const detail = ideasDetail();
    await renderPage(batch({ ...READ, state: 'generating', ads_planned: 4 }));
    await screen.findByTestId('reading-facts');
    expect(
      await screen.findByText(`Make the ads (${detail.ads.length} of 4)`)
    ).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: 'See the ads made so far' })
    ).toHaveAttribute('href', '/w/acme/p/prod_home/ideas');
  });

  it('refreshes the product list, then opens the ad ideas, once the ads are made', async () => {
    await renderPage(batch({ ...READ, state: 'done', ads_planned: 6 }));
    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith('/w/acme/p/prod_home/ideas')
    );
    const refreshed = mockRefreshProducts.mock.invocationCallOrder;
    expect(refreshed.length).toBeGreaterThan(0);
    expect(Math.max(...refreshed)).toBeLessThan(
      mockReplace.mock.invocationCallOrder[0]
    );
    expect(mockReplace).toHaveBeenCalledTimes(1);
    expect(stepStates()).toEqual(['done', 'done', 'done', 'done']);
  });

  it('once the ads are made and being checked, opens the ad ideas like when done (S9, #197)', async () => {
    await renderPage(
      batch({
        ...READ,
        state: 'checking' as unknown as FirstBatch['state'],
        ads_planned: 6,
      })
    );
    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith('/w/acme/p/prod_home/ideas')
    );
    expect(
      Math.max(...mockRefreshProducts.mock.invocationCallOrder)
    ).toBeLessThan(mockReplace.mock.invocationCallOrder[0]);
    expect(stepStates()).toEqual(['done', 'done', 'done', 'done']);
  });

  it('a stop at the check: says which step, links to the made ads, offers to try again (S9, #197)', async () => {
    await renderPage(
      batch({
        ...READ,
        state: 'needs_attention',
        needs_attention: true,
        ads_planned: 6,
        attention: {
          reason: 'step_failed',
          step: 'checks' as unknown as 'images',
          combination_ids: ['c1', 'c2'],
          resumable: true,
        },
        resume: RESUME,
      })
    );
    const stop = screen.getByTestId('reading-stop');
    expect(
      within(stop).getByRole('heading', { name: "“Check the ads” didn't work" })
    ).toBeInTheDocument();
    expect(within(stop).queryByText(/ads? didn't come out/)).toBeNull();
    expect(
      within(stop).getByRole('button', { name: 'Try again · up to $0.09 more' })
    ).toBeEnabled();
    expect(
      screen.getByRole('link', { name: 'See the ads made so far' })
    ).toHaveAttribute('href', '/w/acme/p/prod_home/ideas');
    expect(stepStates()).toEqual(['done', 'done', 'done', 'done', 'stopped']);
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it('several models: choose one, then continue with it', async () => {
    mockGateway.resumeMarketingFirstBatch.mockResolvedValue(
      ok(batch({ state: 'reading' }))
    );
    await renderPage(
      batch({
        state: 'needs_attention',
        needs_attention: true,
        attention: {
          reason: 'several_models',
          step: 'reading',
          variants: ['7 kW', '11 kW'],
          resumable: true,
        },
        resume: {
          ...RESUME,
          additional_cost: '0',
          requires: ['selected_variant'],
        },
      })
    );
    const stop = screen.getByTestId('reading-stop');
    expect(
      within(stop).getByRole('heading', {
        name: 'This link has several models',
      })
    ).toBeInTheDocument();
    const go = within(stop).getByRole('button', {
      name: 'Continue with this model',
    });
    expect(go).toBeDisabled();
    expect(stepStates()[0]).toBe('stopped');
    fireEvent.click(within(stop).getByRole('radio', { name: '11 kW' }));
    fireEvent.click(go);
    await waitFor(() =>
      expect(mockGateway.resumeMarketingFirstBatch).toHaveBeenCalledWith(
        expect.objectContaining({
          firstBatchId: 'mfb_1',
          actionInstanceId: expect.any(String),
          body: { quote_hash: 'rq_1', selected_variant: '11 kW' },
        })
      )
    );
    // The answer is shown at once: reading again, the stop gone.
    await waitFor(() => expect(stepStates()[0]).toBe('active'));
    expect(screen.queryByTestId('reading-stop')).toBeNull();
  });

  it('page could not be read: paste a description or a screenshot and start again', async () => {
    await renderPage(
      batch({
        state: 'failed',
        attention: {
          reason: 'page_unreadable',
          step: 'reading',
          code: 'not_product_page',
          resumable: false,
        },
      })
    );
    const stop = screen.getByTestId('reading-stop');
    expect(
      within(stop).getByRole('heading', { name: "We couldn't read this page" })
    ).toBeInTheDocument();
    expect(
      within(stop).getByLabelText('Product description or screenshots')
    ).toBeInTheDocument();
    // Starting again is a new start with its own maximum on the button.
    expect(
      await within(stop).findByRole('button', { name: 'Start · up to $0.87' })
    ).toBeInTheDocument();
    expect(
      within(stop).queryByRole('button', { name: /^Continue/ })
    ).toBeNull();
    expect(
      within(stop).queryByRole('button', { name: /^Try again/ })
    ).toBeNull();
  });

  it('too little to go on: add material and continue, the extra cost on the button', async () => {
    mockGateway.resumeMarketingFirstBatch.mockResolvedValue(
      ok(batch({ ...READ, state: 'understanding' }))
    );
    await renderPage(
      batch({
        ...READ,
        state: 'needs_attention',
        needs_attention: true,
        attention: { reason: 'too_few_claims', step: 'ideas', resumable: true },
        resume: { ...RESUME, requires: ['additional_text'] },
      })
    );
    const stop = screen.getByTestId('reading-stop');
    expect(
      within(stop).getByText(/No ads were made, so nothing was spent on images/)
    ).toBeInTheDocument();
    const go = within(stop).getByRole('button', {
      name: 'Add and continue · up to $0.09 more',
    });
    expect(go).toBeDisabled();
    fireEvent.change(within(stop).getByLabelText('More about the product'), {
      target: { value: 'Works with any electric car sold in the US.' },
    });
    fireEvent.click(go);
    await waitFor(() =>
      expect(mockGateway.resumeMarketingFirstBatch).toHaveBeenCalledWith(
        expect.objectContaining({
          body: {
            quote_hash: 'rq_1',
            additional_text: 'Works with any electric car sold in the US.',
          },
        })
      )
    );
  });

  it('ads that did not come out: try again for those, the extra cost on the button', async () => {
    mockGateway.resumeMarketingFirstBatch.mockResolvedValue(
      ok(batch({ ...READ, state: 'generating' }))
    );
    await renderPage(
      batch({
        ...READ,
        state: 'needs_attention',
        needs_attention: true,
        ads_planned: 6,
        attention: {
          reason: 'step_failed',
          step: 'images',
          combination_ids: ['c5', 'c6'],
          resumable: true,
        },
        resume: RESUME,
      })
    );
    const stop = screen.getByTestId('reading-stop');
    expect(
      within(stop).getByRole('heading', { name: "2 ads didn't come out" })
    ).toBeInTheDocument();
    fireEvent.click(
      within(stop).getByRole('button', {
        name: 'Try again · up to $0.09 more',
      })
    );
    await waitFor(() =>
      expect(mockGateway.resumeMarketingFirstBatch).toHaveBeenCalledWith(
        expect.objectContaining({ body: { quote_hash: 'rq_1' } })
      )
    );
  });

  it.each([
    [
      'over_budget',
      'The next step would go over your maximum',
      'Continue · up to $0.09 more',
    ],
    [
      'ideas_refused',
      "The claims didn't pass our checks twice",
      'Continue · up to $0.09 more',
    ],
    [
      'step_failed',
      "“Think of claims to try” didn't work",
      'Try again · up to $0.09 more',
    ],
  ] as const)(
    '%s: says what stopped and offers to continue',
    async (reason, title, action) => {
      await renderPage(
        batch({
          ...READ,
          state: 'needs_attention',
          needs_attention: true,
          attention: { reason, step: 'ideas', resumable: true },
          resume: RESUME,
        })
      );
      const stop = screen.getByTestId('reading-stop');
      expect(
        within(stop).getByRole('heading', { name: title })
      ).toBeInTheDocument();
      expect(within(stop).getByRole('button', { name: action })).toBeEnabled();
      expect(
        within(stop).getByText('Everything done so far is kept.')
      ).toBeInTheDocument();
    }
  );

  it('keeps the progress on screen and keeps asking when a re-read fails', async () => {
    mockGateway.getMarketingFirstBatch
      .mockResolvedValueOnce(ok(batch()))
      .mockResolvedValue({
        kind: 'problem',
        status: 502,
        problem: {
          type: 'about:blank',
          title: 'x',
          status: 502,
          detail: 'x',
          instance: '/x',
          code: 'invalid_domain_response',
          trace_id: 'tr',
          retryable: false,
          field_errors: [],
        },
        meta: {},
      });
    render(
      <Fresh>
        <ReadingPage batchId="mfb_1" />
      </Fresh>
    );
    await screen.findByRole('heading', { level: 1 });
    // The next poll fails; what was shown stays.
    await waitFor(
      () =>
        expect(
          mockGateway.getMarketingFirstBatch.mock.calls.length
        ).toBeGreaterThan(1),
      { timeout: 4000 }
    );
    expect(screen.getByTestId('reading-progress')).toBeInTheDocument();
    expect(screen.queryByText("Couldn't load the progress.")).toBeNull();
  });

  it('an images stop with no ad admitted reads as a failed step, with its reason', async () => {
    await renderPage(
      batch({
        ...READ,
        state: 'needs_attention',
        needs_attention: true,
        attention: {
          reason: 'step_failed',
          step: 'images',
          code: 'budget_exceeded',
          resumable: true,
        },
        resume: RESUME,
      })
    );
    const stop = screen.getByTestId('reading-stop');
    expect(
      within(stop).getByRole('heading', { name: "“Make the ads” didn't work" })
    ).toBeInTheDocument();
    expect(
      within(stop).getByText(
        "It would have gone over the workspace's spending limit. The Owner can raise it in Settings."
      )
    ).toBeInTheDocument();
    expect(within(stop).queryByText(/ads? didn't come out/)).toBeNull();
  });

  it('an uncertain call offers no retry until it has been checked', async () => {
    await renderPage(
      batch({
        ...READ,
        state: 'needs_attention',
        needs_attention: true,
        attention: { reason: 'uncertain', step: 'images', resumable: false },
        resume: null,
      })
    );
    const stop = screen.getByTestId('reading-stop');
    expect(
      within(stop).getByRole('heading', {
        name: "We don't know yet whether a step went through",
      })
    ).toBeInTheDocument();
    expect(within(stop).queryByRole('button')).toBeNull();
    // Nothing clears this on its own: the Owner has to check the step.
    expect(
      within(stop).getByText(
        'To avoid paying twice, it is never sent again on its own. Someone has to check whether it went through first: ask the workspace Owner. Once it has been checked, you can continue here.'
      )
    ).toBeInTheDocument();
    expect(
      within(stop).getByText(
        'Waiting for that check. This page looks again every few seconds.'
      )
    ).toBeInTheDocument();
  });

  it('says why a refused continue did not go through', async () => {
    mockGateway.resumeMarketingFirstBatch.mockResolvedValue({
      kind: 'problem',
      status: 409,
      problem: {
        type: 'about:blank',
        title: 'x',
        status: 409,
        detail: 'x',
        instance: '/x',
        code: 'first_batch_quote_changed',
        trace_id: 'tr',
        retryable: false,
        field_errors: [],
      },
      meta: {},
    });
    await renderPage(
      batch({
        ...READ,
        state: 'needs_attention',
        needs_attention: true,
        attention: { reason: 'over_budget', step: 'images', resumable: true },
        resume: RESUME,
      })
    );
    fireEvent.click(
      screen.getByRole('button', { name: 'Continue · up to $0.09 more' })
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The cost changed. Refresh and confirm the new maximum.'
    );
  });

  it('a continue that gets no answer says so, and the next click replays the same continue', async () => {
    mockGateway.resumeMarketingFirstBatch
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(ok(batch({ ...READ, state: 'ideas' })));
    await renderPage(
      batch({
        ...READ,
        state: 'needs_attention',
        needs_attention: true,
        attention: { reason: 'over_budget', step: 'ideas', resumable: true },
        resume: RESUME,
      })
    );
    fireEvent.click(
      screen.getByRole('button', { name: 'Continue · up to $0.09 more' })
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(
      "We couldn't confirm whether it continued. Click again; it won't run twice."
    );
    fireEvent.click(
      screen.getByRole('button', { name: 'Continue · up to $0.09 more' })
    );
    await waitFor(() =>
      expect(mockGateway.resumeMarketingFirstBatch).toHaveBeenCalledTimes(2)
    );
    const [first, second] = mockGateway.resumeMarketingFirstBatch.mock.calls;
    expect(second[0].actionInstanceId).toBe(first[0].actionInstanceId);
  });

  it('a start from another workspace is not found here', async () => {
    mockGateway.getMarketingFirstBatch.mockResolvedValue({
      kind: 'problem',
      status: 404,
      problem: {
        type: 'about:blank',
        title: 'x',
        status: 404,
        detail: 'x',
        instance: '/x',
        code: 'first_batch_not_found',
        trace_id: 'tr',
        retryable: false,
        field_errors: [],
      },
      meta: {},
    });
    render(
      <Fresh>
        <ReadingPage batchId="mfb_other" />
      </Fresh>
    );
    expect(
      await screen.findByText(
        "This start can't be found here. It may belong to another workspace."
      )
    ).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: 'Back to the start page' })
    ).toBeInTheDocument();
  });
});
