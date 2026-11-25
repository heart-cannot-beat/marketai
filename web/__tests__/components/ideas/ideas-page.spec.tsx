/**
 * @jest-environment jsdom
 */
import type { ReactNode } from 'react';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { SWRConfig } from 'swr';
import { IdeasPage } from '@/components/ideas/IdeasPage';
import { AdCard } from '@/components/ideas/AdCard';
import {
  ad,
  ANGLES,
  checksQuote,
  COMBINATIONS,
  generationCapabilities,
  ideasDetail,
  plannerCapabilities,
  product,
} from './fixtures';

/**
 * The ad ideas page (ADR 0018, design §5.3, #196): ads grouped by claim, each
 * a draft that can be kept; the facts the AI read on the side, not checked
 * yet; images still being made, or that failed, in their own state.
 */
jest.mock('next/router', () => ({
  useRouter: () => ({ asPath: '/w/acme/p/prod_home/ideas', query: {} }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', workspaceId: 'ws_1' } }),
}));
const mockGateway = {
  getMarketingIdeas: jest.fn(),
  setMarketingAdKept: jest.fn(),
  getMarketingFirstBatch: jest.fn(),
  resumeMarketingFirstBatch: jest.fn(),
  getMarketingCapabilities: jest.fn(),
  getGenerationCapabilities: jest.fn(),
  getMarketingIdeasChecksQuote: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/components/workspace/useMarketingImageSource', () => ({
  useMarketingImageSource: (id: string) => ({
    imageUrl: `blob:image-${id}`,
    imageFailed: false,
    onImageError: jest.fn(),
  }),
}));
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => ({ downloadBlob: jest.fn() }),
}));
const mockRefreshProducts = jest.fn().mockResolvedValue(undefined);
jest.mock('@/hooks/products/useWorkspaceProducts', () => ({
  useWorkspaceProducts: () => ({ refresh: mockRefreshProducts }),
}));

function ok<T>(data: T) {
  return { kind: 'ok' as const, status: 200, data, meta: {} };
}

function Fresh({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

async function renderPage(detail = ideasDetail(), item = product()) {
  mockGateway.getMarketingIdeas.mockResolvedValue(ok(detail));
  const view = render(
    <Fresh>
      <IdeasPage product={item} />
    </Fresh>
  );
  await screen.findByRole('heading', { level: 2, name: /claims/ });
  return view;
}

const claim = (number: number) =>
  screen.getByRole('region', { name: new RegExp(`^Claim ${number}`) });

beforeEach(() => {
  jest.clearAllMocks();
  mockGateway.getMarketingCapabilities.mockResolvedValue(
    ok(plannerCapabilities())
  );
  mockGateway.getGenerationCapabilities.mockResolvedValue(
    ok(generationCapabilities())
  );
  mockGateway.getMarketingIdeasChecksQuote.mockResolvedValue(ok(checksQuote()));
});

describe('IdeasPage (#196)', () => {
  it('groups the ads by claim, each with its way of telling, a draft mark and a keep button', async () => {
    await renderPage();
    expect(
      screen.getByRole('heading', { level: 2, name: '3 claims, 4 ads' })
    ).toBeInTheDocument();
    expect(mockGateway.getMarketingIdeas).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      projectId: 'mp_secret',
    });

    const first = claim(1);
    expect(
      within(first).getByRole('heading', {
        level: 3,
        name: 'Plug in at night and forget about it',
      })
    ).toBeInTheDocument();
    expect(
      within(first).getByText('Way of telling: Bedtime story')
    ).toBeInTheDocument();
    expect(
      within(first).getByText('Way of telling: App demo')
    ).toBeInTheDocument();
    expect(within(first).getAllByText('Draft')).toHaveLength(2);
    expect(
      within(first).getByRole('img', { name: 'Plug in. Go to bed.' })
    ).toHaveAttribute('src', 'blob:image-av_1');
    expect(within(first).getByRole('button', { name: 'Kept' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(
      within(first).getByRole('button', { name: 'Keep this one' })
    ).toHaveAttribute('aria-pressed', 'false');

    // Why the claim is worth trying folds open; it is there to read.
    expect(
      within(first).getByText("Why it's worth trying")
    ).toBeInTheDocument();
    expect(
      within(first).getByText(
        'Owners charge at home overnight and want it to just work.'
      )
    ).toBeInTheDocument();

    // S13 (#201) adds the buttons S8 left out; step 2 takes the kept ads to a test (#256).
    expect(within(first).getAllByRole('button', { name: 'Edit' })).toHaveLength(
      2
    );
    expect(
      screen.getByRole('button', { name: 'Download images only' })
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId('ideas-kept-bar')).getByRole('link', {
        name: 'Take to test',
      })
    ).toHaveAttribute('href', '/w/acme/p/prod_home/take-to-test');
  });

  it('shows two claims when the ideas have only two, never six empty slots', async () => {
    const detail = ideasDetail({
      ads: [ad('av_1', 'c1'), ad('av_2', 'c2')],
      pending: [],
    });
    detail.ideas = {
      ...detail.ideas!,
      angles: ANGLES.slice(0, 2),
      combinations: COMBINATIONS.slice(0, 4),
    };
    await renderPage(detail);
    expect(
      screen.getByRole('heading', { level: 2, name: '2 claims, 2 ads' })
    ).toBeInTheDocument();
    expect(screen.getAllByRole('region', { name: /^Claim \d/ })).toHaveLength(
      2
    );
    // The second claim's ads are not made yet: said so, not left blank.
    expect(within(claim(2)).getAllByText('Not made yet.')).toHaveLength(2);
  });

  it('marks an ad made without a product photo, and offers to add one (#233)', async () => {
    await renderPage();
    const second = claim(2);
    const cards = within(second).getAllByTestId('ad-card');
    expect(
      within(cards[0]).getByText('No product photo used')
    ).toBeInTheDocument();
    expect(within(cards[1]).queryByText('No product photo used')).toBeNull();
    expect(screen.getAllByText('No product photo used')).toHaveLength(1);
    expect(
      screen.getByText(
        'Some ads were made without a product photo. Add a product photo so the product in them looks more like the real thing.'
      )
    ).toBeInTheDocument();
    // Straight to adding a photo on the product's profile.
    expect(
      screen.getByRole('link', { name: 'Add a product photo' })
    ).toHaveAttribute('href', '/w/acme/p/prod_home/profile#add-photo');
  });

  it('suggests no photo when every ad used one', async () => {
    await renderPage(ideasDetail({ ads: [ad('av_1', 'c1')], pending: [] }));
    expect(
      screen.getByRole('heading', { level: 2, name: '3 claims, 1 ad' })
    ).toBeInTheDocument();
    expect(screen.queryByText('No product photo used')).toBeNull();
    expect(
      screen.queryByRole('link', { name: 'Add a product photo' })
    ).toBeNull();
  });

  it('keeping an ad calls setMarketingAdKept and changes the count', async () => {
    const detail = ideasDetail();
    await renderPage(detail);
    expect(screen.getByTestId('ideas-kept-bar')).toHaveTextContent('1 kept');

    const kept = ideasDetail({
      ads: detail.ads.map((item) =>
        item.asset_version_id === 'av_2' ? { ...item, kept: true } : item
      ),
    });
    let answer: (value: unknown) => void = () => undefined;
    mockGateway.setMarketingAdKept.mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      })
    );
    fireEvent.click(
      within(claim(1)).getByRole('button', { name: 'Keep this one' })
    );

    // The mark shows at once and the count follows.
    expect(screen.getByTestId('ideas-kept-bar')).toHaveTextContent('2 kept');
    expect(mockGateway.setMarketingAdKept).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      actionInstanceId: expect.any(String),
      projectId: 'mp_secret',
      assetVersionId: 'av_2',
      body: { kept: true },
    });
    await act(async () => answer(ok(kept)));
    expect(
      within(claim(1)).getAllByRole('button', { name: 'Kept' })
    ).toHaveLength(2);
    expect(screen.getByTestId('ideas-kept-bar')).toHaveTextContent('2 kept');
    await waitFor(() => expect(mockRefreshProducts).toHaveBeenCalled());

    // And back: dropping a kept ad.
    mockGateway.setMarketingAdKept.mockResolvedValue(ok(detail));
    fireEvent.click(
      within(claim(1)).getAllByRole('button', { name: 'Kept' })[1]
    );
    await waitFor(() =>
      expect(screen.getByTestId('ideas-kept-bar')).toHaveTextContent('1 kept')
    );
    expect(mockGateway.setMarketingAdKept).toHaveBeenLastCalledWith(
      expect.objectContaining({ assetVersionId: 'av_2', body: { kept: false } })
    );
  });

  it('puts the mark back and says why when keeping fails', async () => {
    await renderPage();
    mockGateway.setMarketingAdKept.mockResolvedValue({
      kind: 'problem',
      status: 404,
      problem: {
        type: 'about:blank',
        title: 'Not found',
        status: 404,
        detail: '',
        instance: '',
        code: 'ad_not_found',
        trace_id: 'trace-1',
        retryable: false,
        field_errors: [],
      },
      meta: {},
    });
    fireEvent.click(
      within(claim(1)).getByRole('button', { name: 'Keep this one' })
    );
    expect(
      await screen.findByText('This ad is no longer here. Refresh the page.')
    ).toBeInTheDocument();
    expect(screen.getByTestId('ideas-kept-bar')).toHaveTextContent('1 kept');
    expect(
      within(claim(1)).getByRole('button', { name: 'Keep this one' })
    ).toHaveAttribute('aria-pressed', 'false');
  });

  it('shows a placeholder for an image being made and a retry entry for a failed one', async () => {
    // While the first batch is still making its ads, it makes this one too:
    // trying again from here waits for it (#195, #201).
    await renderPage(
      ideasDetail({
        first_batch: {
          id: 'mfb_1',
          state: 'generating',
          needs_attention: false,
          reason: null,
        },
      })
    );
    const third = claim(3);
    const [making, failed] = within(third).getAllByTestId('ad-card');
    expect(within(making).getByRole('status')).toHaveTextContent(
      'Making this ad…'
    );
    expect(within(making).queryByRole('img')).toBeNull();
    expect(within(making).queryByRole('button', { name: /keep/i })).toBeNull();

    expect(
      within(failed).getByText("This one didn't come out.")
    ).toBeInTheDocument();
    const retry = within(failed).getByRole('button', { name: 'Try again' });
    // Only a first batch's images are tried again from here (#193, #195);
    // while it still makes its others, the entry says when it can (#204).
    expect(retry).toBeDisabled();
    expect(retry).toHaveAccessibleDescription(
      'The first batch is still making or checking its other images; you can retry here once it is done.'
    );
    expect(
      screen.getByText(
        '1 more ad is being made. It appears here when it is ready.'
      )
    ).toBeInTheDocument();
  });

  it("tries the first batch's failed ads again, the extra cost on the button (#195)", async () => {
    const stopped = {
      id: 'mfb_1',
      state: 'needs_attention' as const,
      needs_attention: true,
      reason: 'step_failed' as const,
    };
    mockGateway.getMarketingFirstBatch.mockResolvedValue(
      ok({
        id: 'mfb_1',
        state: 'needs_attention',
        attention: {
          reason: 'step_failed',
          step: 'images',
          combination_ids: ['c6'],
          resumable: true,
        },
        resume: {
          maximum_cost: '0.95',
          additional_cost: '0.04',
          currency: 'USD',
          quote_hash: 'rq_1',
          requires: [],
        },
        confirmed_max_cost: '0.91',
      })
    );
    mockGateway.resumeMarketingFirstBatch.mockResolvedValue(
      ok({ id: 'mfb_1', state: 'generating', attention: null, resume: null })
    );
    await renderPage(ideasDetail({ first_batch: stopped }));

    expect(
      screen.getByRole('link', { name: 'See what to do' })
    ).toHaveAttribute('href', '/w/acme/start/mfb_1');
    const [, failed] = within(claim(3)).getAllByTestId('ad-card');
    const retry = await within(failed).findByRole('button', {
      name: 'Try again · up to $0.04 more',
    });
    expect(retry).toBeEnabled();
    fireEvent.click(retry);
    await waitFor(() =>
      expect(mockGateway.resumeMarketingFirstBatch).toHaveBeenCalledWith(
        expect.objectContaining({
          workspaceSlug: 'acme',
          firstBatchId: 'mfb_1',
          actionInstanceId: expect.any(String),
          body: { quote_hash: 'rq_1' },
        })
      )
    );
    // The page and the product list ask again, so the new attempt shows.
    await waitFor(() =>
      expect(mockGateway.getMarketingIdeas).toHaveBeenCalledTimes(2)
    );
    expect(mockRefreshProducts).toHaveBeenCalled();
  });

  it('says nothing about the first batch once its ads are made and being checked (S9, #197)', async () => {
    await renderPage(
      ideasDetail({
        first_batch: {
          id: 'mfb_1',
          state: 'checking',
          needs_attention: false,
          reason: null,
        },
      })
    );
    expect(screen.queryByTestId('ideas-first-batch')).toBeNull();
    expect(
      screen.queryByText('The first ads are still being made.')
    ).toBeNull();
  });

  it('points back to the progress while the first batch is still running (#195)', async () => {
    await renderPage(
      ideasDetail({
        first_batch: {
          id: 'mfb_1',
          state: 'generating',
          needs_attention: false,
          reason: null,
        },
      })
    );
    expect(
      screen.getByText('The first ads are still being made.')
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'See progress' })).toHaveAttribute(
      'href',
      '/w/acme/start/mfb_1'
    );
    expect(mockGateway.getMarketingFirstBatch).not.toHaveBeenCalled();
  });

  it('asks again while images are being made, and stops when they are done', async () => {
    jest.useFakeTimers();
    try {
      const done = ideasDetail({
        ads: [...ideasDetail().ads, ad('av_5', 'c5')],
        pending: [{ combination_id: 'c6', state: 'failed' }],
      });
      mockGateway.getMarketingIdeas
        .mockResolvedValueOnce(ok(ideasDetail()))
        .mockResolvedValue(ok(done));
      render(
        <Fresh>
          <IdeasPage product={product()} />
        </Fresh>
      );
      await act(async () => {
        await Promise.resolve();
      });
      await screen.findByRole('heading', { level: 2, name: '3 claims, 4 ads' });
      await act(async () => {
        jest.advanceTimersByTime(3_500);
      });
      await screen.findByRole('heading', { level: 2, name: '3 claims, 5 ads' });
      const calls = mockGateway.getMarketingIdeas.mock.calls.length;
      await act(async () => {
        jest.advanceTimersByTime(10_000);
      });
      expect(mockGateway.getMarketingIdeas.mock.calls.length).toBe(calls);
    } finally {
      jest.useRealTimers();
    }
  });

  it('keeps the bottom-bar button unusable while nothing is kept', async () => {
    const none = ideasDetail({
      ads: ideasDetail().ads.map((item) => ({ ...item, kept: false })),
    });
    await renderPage(none);
    const bar = screen.getByTestId('ideas-kept-bar');
    expect(bar).toHaveTextContent('0 kept');
    expect(
      within(bar).getByRole('button', { name: 'Download images only' })
    ).toBeDisabled();
    expect(
      within(bar).getByRole('button', { name: 'Take to test' })
    ).toBeDisabled();
    expect(
      within(bar).queryByRole('link', { name: 'Take to test' })
    ).toBeNull();

    mockGateway.setMarketingAdKept.mockResolvedValue(
      ok(
        ideasDetail({
          ads: none.ads.map((item, i) =>
            i === 0 ? { ...item, kept: true } : item
          ),
        })
      )
    );
    fireEvent.click(
      within(claim(1)).getAllByRole('button', { name: 'Keep this one' })[0]
    );
    const button = within(bar).getByRole('button', {
      name: 'Download images only',
    });
    await waitFor(() => expect(button).toBeEnabled());
    expect(bar).toHaveTextContent('1 kept');
    expect(
      within(bar).getByRole('link', { name: 'Take to test' })
    ).toHaveAttribute('href', '/w/acme/p/prod_home/take-to-test');

    // It leads to the check before downloading (#201), not to a placeholder.
    fireEvent.click(button);
    expect(
      await screen.findByRole('heading', {
        level: 1,
        name: 'Check once before downloading',
      })
    ).toBeInTheDocument();
    expect(screen.queryByText(/aren't open yet/)).toBeNull();
  });

  it('opens on the check before downloading when "take to test" sends the user there (#256)', async () => {
    mockGateway.getMarketingIdeas.mockResolvedValue(ok(ideasDetail()));
    render(
      <Fresh>
        <IdeasPage product={product()} startWithCheck />
      </Fresh>
    );
    expect(
      await screen.findByRole('heading', {
        level: 1,
        name: 'Check once before downloading',
      })
    ).toBeInTheDocument();
  });

  it('shows the facts the AI read on the side, each not checked yet until it is', async () => {
    await renderPage();
    const panel = screen.getByRole('complementary', {
      name: 'What the AI read about the product',
    });
    const facts = within(panel).getAllByRole('listitem');
    expect(facts.map((fact) => fact.textContent)).toEqual([
      expect.stringContaining('Schedule charging in the Acme Charge app'),
      expect.stringContaining('IP55-rated enclosure'),
      expect.stringContaining('Load balancing with the home'),
    ]);
    expect(within(facts[0]).getByText('Not checked yet')).toBeInTheDocument();
    expect(within(facts[1]).getByText('Not checked yet')).toBeInTheDocument();
    expect(within(facts[2]).getByText('Checked')).toBeInTheDocument();
    expect(
      within(facts[0]).getByText(/Schedule charging from the Acme Charge app\./)
    ).toBeInTheDocument();
  });

  it('shows no competitor line without a scan, and one per claim with a scan', async () => {
    const { unmount } = await renderPage();
    expect(screen.queryByText(/competitor/i)).toBeNull();
    unmount();

    const scanned = ideasDetail();
    scanned.ideas = {
      ...scanned.ideas!,
      competitor_snapshot_id: 'snap_secret',
      angles: ANGLES.map((angle) =>
        angle.id === 'a2' ? { ...angle, competitor_overlap: ['obs_1'] } : angle
      ),
    };
    await renderPage(scanned);
    expect(
      within(claim(1)).getByText('No competitor in your scan says this yet')
    ).toBeInTheDocument();
    expect(
      within(claim(2)).getByText('Competitors in your scan say this too')
    ).toBeInTheDocument();
  });

  it('never shows an internal id', async () => {
    const { container } = await renderPage();
    const text = container.textContent ?? '';
    for (const id of [
      'mp_secret',
      'av_1',
      'c1',
      'a1',
      'e1',
      'f1',
      'pc_secret',
      'op_secret',
      'prod_home',
    ])
      expect(text).not.toContain(id);
  });

  it('says so when the product has no ad ideas yet', () => {
    render(
      <Fresh>
        <IdeasPage product={product(null)} />
      </Fresh>
    );
    expect(
      screen.getByText('This product has no ad ideas yet.')
    ).toBeInTheDocument();
    expect(mockGateway.getMarketingIdeas).not.toHaveBeenCalled();
  });

  it('says the ideas are still being prepared before there are any', async () => {
    mockGateway.getMarketingIdeas.mockResolvedValue(
      ok(
        ideasDetail({
          state: 'understanding_draft',
          ideas: null,
          ads: [],
          pending: [],
        })
      )
    );
    render(
      <Fresh>
        <IdeasPage product={product()} />
      </Fresh>
    );
    expect(
      await screen.findByText(
        'The ad ideas are still being prepared. They appear here when they are ready.'
      )
    ).toBeInTheDocument();
    // The facts already read show beside it.
    expect(
      screen.getByRole('complementary', {
        name: 'What the AI read about the product',
      })
    ).toBeInTheDocument();
  });

  it('before there are ideas, a stopped first batch says it waits for the user, not that ideas are coming (#195)', async () => {
    mockGateway.getMarketingIdeas.mockResolvedValue(
      ok(
        ideasDetail({
          state: 'understanding_draft',
          ideas: null,
          ads: [],
          pending: [],
          first_batch: {
            id: 'mfb_1',
            state: 'needs_attention',
            needs_attention: true,
            reason: 'too_few_claims',
          },
        })
      )
    );
    render(
      <Fresh>
        <IdeasPage product={product()} />
      </Fresh>
    );
    expect(
      await screen.findByText(
        'Making the first ads stopped and is waiting for you.'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: 'See what to do' })
    ).toHaveAttribute('href', '/w/acme/start/mfb_1');
    expect(
      screen.queryByText(
        'The ad ideas are still being prepared. They appear here when they are ready.'
      )
    ).toBeNull();
    // Not a failed images stop: the batch itself is not read here.
    expect(mockGateway.getMarketingFirstBatch).not.toHaveBeenCalled();
  });

  it('offers to try again when the ideas could not be loaded', async () => {
    mockGateway.getMarketingIdeas.mockResolvedValueOnce({
      kind: 'problem',
      status: 503,
      problem: {
        type: 'about:blank',
        title: 'Unavailable',
        status: 503,
        detail: '',
        instance: '',
        code: 'domain_unavailable',
        trace_id: 'trace-1',
        retryable: true,
        field_errors: [],
      },
      meta: {},
    });
    render(
      <Fresh>
        <IdeasPage product={product()} />
      </Fresh>
    );
    expect(
      await screen.findByText("Couldn't load the ad ideas.")
    ).toBeInTheDocument();
    mockGateway.getMarketingIdeas.mockResolvedValue(ok(ideasDetail()));
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(
      await screen.findByRole('heading', { level: 2, name: '3 claims, 4 ads' })
    ).toBeInTheDocument();
  });
});

describe('IdeasPage: trying a failed image again (#204)', () => {
  it('says the retry is running on the card, and the button cannot be pressed again', async () => {
    mockGateway.getMarketingFirstBatch.mockResolvedValue(
      ok({
        id: 'mfb_1',
        state: 'needs_attention',
        attention: {
          reason: 'step_failed',
          step: 'images',
          combination_ids: ['c6'],
          resumable: true,
        },
        resume: {
          maximum_cost: '0.95',
          additional_cost: '0.04',
          currency: 'USD',
          quote_hash: 'rq_1',
          requires: [],
        },
        confirmed_max_cost: '0.91',
      })
    );
    let answered: (value: unknown) => void = () => undefined;
    mockGateway.resumeMarketingFirstBatch.mockReturnValue(
      new Promise((done) => (answered = done))
    );
    await renderPage(
      ideasDetail({
        first_batch: {
          id: 'mfb_1',
          state: 'needs_attention',
          needs_attention: true,
          reason: 'step_failed',
        },
      })
    );
    const [, failed] = within(claim(3)).getAllByTestId('ad-card');
    fireEvent.click(
      await within(failed).findByRole('button', {
        name: 'Try again · up to $0.04 more',
      })
    );
    const busy = await within(failed).findByRole('button', {
      name: 'Retrying…',
    });
    expect(busy).toBeDisabled();
    answered(
      ok({ id: 'mfb_1', state: 'generating', attention: null, resume: null })
    );
    await waitFor(() =>
      expect(mockGateway.resumeMarketingFirstBatch).toHaveBeenCalledTimes(1)
    );
  });

  it('a batch stopped for another reason says to sort that out first', async () => {
    await renderPage(
      ideasDetail({
        first_batch: {
          id: 'mfb_1',
          state: 'needs_attention',
          needs_attention: true,
          reason: 'uncertain',
        },
      })
    );
    const [, failed] = within(claim(3)).getAllByTestId('ad-card');
    expect(
      within(failed).getByRole('button', { name: 'Try again' })
    ).toHaveAccessibleDescription(
      'The first batch stopped. Sort out why on its progress page first; then you can retry here.'
    );
  });
});

describe('AdCard (#196)', () => {
  it('runs the retry a later slice hands it', () => {
    const onRetry = jest.fn();
    render(
      <AdCard
        slot={{ kind: 'failed', key: 'c6', headline: null, telling: null }}
        onRetry={onRetry}
      />
    );
    const retry = screen.getByRole('button', { name: 'Try again' });
    expect(retry).toBeEnabled();
    fireEvent.click(retry);
    expect(onRetry).toHaveBeenCalled();
  });
});
