/**
 * @jest-environment jsdom
 */
import { useEffect, type ReactNode } from 'react';
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { SWRConfig } from 'swr';
import { IdeasPage } from '@/components/ideas/IdeasPage';
import {
  ad,
  check,
  checksQuote,
  COMBINATIONS,
  generationCapabilities,
  ideasDetail,
  plannerCapabilities,
  product,
} from './fixtures';

/**
 * Changing one ad's headline and body copy on "Edit this ad" (design §5.3,
 * #228): the copy is typed in, previewed as it is typed, saved for free after
 * saying how many images need making again, and then one image with the new
 * copy is offered at the preview's maximum. Refusals say what is wrong.
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
  startMarketingIdeasChecks: jest.fn(),
  prepareMarketingIdeasFeedback: jest.fn(),
  prepareMarketingIdeasGeneration: jest.fn(),
  createMarketingIdeasImageEdit: jest.fn(),
  editMarketingIdeasCopy: jest.fn(),
  getGenerationDraft: jest.fn(),
  createGenerationPreview: jest.fn(),
  submitGenerationRun: jest.fn(),
  startMarketingIdeasPlanning: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/components/workspace/useMarketingImageSource', () => ({
  useMarketingImageSource: (id: string, open: boolean) => ({
    imageUrl: open ? `blob:image-${id}` : null,
    imageFailed: false,
    preview: { error: null, mutate: jest.fn() },
    onImageError: jest.fn(),
  }),
}));
jest.mock('@/components/workspace/ImageSelectionCanvas', () => ({
  CHECKERBOARD: {},
  ImageSelectionCanvas: ({
    onLoad,
  }: {
    onLoad?: (size: { width: number; height: number }) => void;
  }) => {
    useEffect(() => onLoad?.({ width: 1024, height: 1280 }), [onLoad]);
    return <div data-testid="canvas" />;
  },
}));
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => ({ downloadBlob: jest.fn() }),
}));
jest.mock('@/hooks/products/useWorkspaceProducts', () => ({
  useWorkspaceProducts: () => ({ refresh: jest.fn() }),
}));

/** A domain answer as the gateway gives it: 200 `ok`, 201 `created`, 202 `accepted`. */
function ok<T>(data: T, status = 200) {
  const kind = status === 201 ? 'created' : status === 202 ? 'accepted' : 'ok';
  return { kind, status, data, meta: {} };
}
const reason = (code: string) => ({
  code: code as 'facts_unverified',
  fact_ids: [],
  finding_ids: [],
});
function refused(code: string, params: Record<string, unknown> = {}) {
  return {
    kind: 'problem',
    status: code === 'stale_marketing_revision' ? 409 : 422,
    problem: {
      type: 'about:blank',
      title: '',
      status: 422,
      detail: 'the service detail is never shown',
      instance: '',
      code,
      trace_id: 'trace_secret',
      retryable: false,
      field_errors: [],
      params,
    },
  };
}

const NEW = {
  headline: 'Charge on your schedule',
  body_copy: 'Set overnight charging from the Acme Charge app.',
};

/** c1 holds av_1 (kept) and its crop av_2; c3 holds an uploaded image; c4 an image of earlier copy. */
function copyDetail() {
  return ideasDetail({
    ads: [
      ad('av_1', 'c1', {
        kept: true,
        cited_fact_ids: ['f1'],
        check: check('qc_1'),
        reasons: [reason('facts_unverified')],
      }),
      ad('av_2', 'c1', {
        origin: 'image_edit',
        source_asset_version_id: 'av_1',
        created_at: '2026-11-17T09:00:00+00:00',
        reasons: [reason('check_missing')],
      }),
      ad('av_3', 'c3', {
        origin: 'uploaded',
        used_product_photo: false,
        reasons: [reason('appearance_unconfirmed'), reason('check_missing')],
      }),
      ad('av_4', 'c4', { kept: true, reasons: [reason('check_missing')] }),
    ],
    pending: [],
  });
}

/** The ideas as the server answers a saved copy of c1: its images are of earlier copy now. */
function savedDetail() {
  const before = copyDetail();
  return {
    ...before,
    revision: 4,
    ideas: {
      ...before.ideas!,
      combinations: COMBINATIONS.map((c) =>
        c.id === 'c1' ? { ...c, ...NEW } : c
      ),
    },
    ads: before.ads.map((item) =>
      item.combination_id === 'c1'
        ? {
            ...item,
            status: 'needs_redo' as const,
            reasons: [reason('ideas_changed')],
          }
        : item
    ),
  };
}

function Fresh({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

async function openEdit(index: number, detail = copyDetail()) {
  mockGateway.getMarketingIdeas.mockResolvedValue(ok(detail));
  const view = render(
    <Fresh>
      <IdeasPage product={product()} />
    </Fresh>
  );
  await screen.findByRole('heading', { level: 2, name: /claims/ });
  fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[index]);
  await screen.findByRole('heading', { level: 1, name: 'Edit this ad' });
  return view;
}

const copyRegion = () => within(screen.getByRole('region', { name: 'Copy' }));
const preview = () => within(screen.getByTestId('copy-preview'));

function type(label: 'Headline' | 'Body text', value: string) {
  fireEvent.change(copyRegion().getByLabelText(label), {
    target: { value },
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockGateway.getMarketingCapabilities.mockResolvedValue(
    ok(plannerCapabilities())
  );
  mockGateway.getGenerationCapabilities.mockResolvedValue(
    ok(generationCapabilities())
  );
  mockGateway.getMarketingIdeasChecksQuote.mockResolvedValue(
    ok(checksQuote({ quantity: 2 }))
  );
  mockGateway.getGenerationDraft.mockImplementation(async ({ draftId }) =>
    ok({ id: draftId, revision: 1 })
  );
  mockGateway.prepareMarketingIdeasGeneration.mockResolvedValue(
    ok(
      {
        project_id: 'mp_secret',
        revision: 4,
        drafts: [
          { combination_id: 'c1', draft_id: 'gd_c1' },
          { combination_id: 'c2', draft_id: 'gd_c2' },
        ],
      },
      201
    )
  );
  mockGateway.createGenerationPreview.mockResolvedValue(
    ok(
      {
        id: 'gp_c1',
        preview_hash: 'gp_c1_hash',
        draft_revision: 1,
        maximum_cost: { amount: '0.04', currency: 'USD' },
      },
      201
    )
  );
  mockGateway.submitGenerationRun.mockResolvedValue({
    kind: 'accepted',
    status: 202,
    data: { operation_id: 'op_1', generation_run_id: 'run_1' },
    meta: {},
  });
});

describe('Changing the copy (#228)', () => {
  it('previews the copy as it is typed and says what saving does to the images first', async () => {
    const { container } = await openEdit(0);
    expect(copyRegion().getByLabelText('Headline')).toHaveValue(
      'Plug in. Go to bed.'
    );
    expect(preview().getByText('Plug in. Go to bed.')).toBeInTheDocument();
    // Nothing changed: nothing to save, nothing said about the images.
    expect(
      copyRegion().getByRole('button', { name: 'Save the copy' })
    ).toBeDisabled();
    expect(copyRegion().queryByRole('note')).toBeNull();

    type('Headline', NEW.headline);
    expect(preview().getByText(NEW.headline)).toBeInTheDocument();
    expect(preview().queryByText('Plug in. Go to bed.')).toBeNull();
    type('Body text', NEW.body_copy);
    expect(preview().getByText(NEW.body_copy)).toBeInTheDocument();
    // Both images of this way of telling -- the ad and its crop -- need
    // making again; one of them is kept.
    expect(copyRegion().getByRole('note')).toHaveTextContent(
      'Once saved, the 2 images already made for this way of telling need making again with the new copy before they can be downloaded. You kept 1 of them.'
    );
    expect(copyRegion().getByText(/Saving the copy is free/)).toBeVisible();
    expect(mockGateway.editMarketingIdeasCopy).not.toHaveBeenCalled();
    for (const id of ['av_1', 'c1', 'a1', 'mp_secret'])
      expect(container.textContent).not.toContain(id);
  });

  it('saves for free, then offers one image with the new copy at the preview maximum', async () => {
    await openEdit(0);
    mockGateway.editMarketingIdeasCopy.mockResolvedValue(ok(savedDetail()));
    mockGateway.getMarketingIdeas.mockResolvedValue(ok(savedDetail()));
    type('Headline', `  ${NEW.headline} `);
    type('Body text', NEW.body_copy);
    fireEvent.click(
      copyRegion().getByRole('button', { name: 'Save the copy' })
    );
    // The copy as the user meant it, at the revision they saw.
    await waitFor(() =>
      expect(mockGateway.editMarketingIdeasCopy).toHaveBeenCalledWith({
        workspaceSlug: 'acme',
        actionInstanceId: expect.any(String),
        expectedRevision: 3,
        projectId: 'mp_secret',
        combinationId: 'c1',
        body: NEW,
      })
    );
    expect(
      await copyRegion().findByText(
        /Copy saved\. Images made with the old copy/
      )
    ).toBeVisible();
    // The new image is prepared at the new revision and quoted; nothing is
    // spent until the button carrying the maximum is clicked.
    const confirm = await copyRegion().findByRole('button', {
      name: 'Make one with the new copy · up to $0.04',
    });
    expect(mockGateway.prepareMarketingIdeasGeneration).toHaveBeenCalledWith(
      expect.objectContaining({ expectedRevision: 4 })
    );
    expect(mockGateway.submitGenerationRun).not.toHaveBeenCalled();
    // "Make it again" would offer the same image: it is not offered twice.
    expect(
      screen.queryByRole('button', { name: 'Make it again as it is' })
    ).toBeNull();
    // The form now holds the saved copy, and the preview shows it.
    expect(copyRegion().getByLabelText('Headline')).toHaveValue(NEW.headline);
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(mockGateway.submitGenerationRun).toHaveBeenCalledWith(
        expect.objectContaining({
          body: { preview_id: 'gp_c1', preview_hash: 'gp_c1_hash' },
        })
      )
    );
    expect(await copyRegion().findByText(/^Started\./)).toBeVisible();
    expect(mockGateway.editMarketingIdeasCopy).toHaveBeenCalledTimes(1);
  });

  it('names the words no fact of this ad supports, and where a source is added', async () => {
    await openEdit(0);
    mockGateway.editMarketingIdeasCopy.mockResolvedValue(
      refused('copy_unsupported', { claims: ['22 kW', 'UL 2594'] })
    );
    type('Headline', 'Charges at 22 kW, UL 2594 listed');
    fireEvent.click(
      copyRegion().getByRole('button', { name: 'Save the copy' })
    );
    const alert = await copyRegion().findByRole('alert');
    expect(alert).toHaveTextContent(
      "These aren't in the facts this ad uses: 22 kW, UL 2594."
    );
    expect(
      within(alert).getByRole('link', {
        name: 'Add a source in the product profile',
      })
    ).toHaveAttribute('href', '/w/acme/p/prod_home/profile');
    expect(alert).not.toHaveTextContent('the service detail');
    // Nothing was saved: no image is offered.
    expect(mockGateway.prepareMarketingIdeasGeneration).not.toHaveBeenCalled();
    expect(
      copyRegion().getByRole('button', { name: 'Save the copy' })
    ).toBeEnabled();
  });

  it.each([
    [
      'copy_language_mismatch',
      {},
      "Write the headline and body text in the ads' language.",
    ],
    [
      'ad_copy_too_long',
      { over: 12 },
      'too long to fit when the image is made. Cut about 12 characters.',
    ],
    [
      'validation_error',
      {},
      'Write both a headline and body text, each up to 2000 characters.',
    ],
    ['first_batch_running', {}, 'Your first ads are still being made.'],
  ])('explains a %s refusal', async (code, params, sentence) => {
    await openEdit(0);
    mockGateway.editMarketingIdeasCopy.mockResolvedValue(refused(code, params));
    type('Headline', 'Ein neuer Titel');
    fireEvent.click(
      copyRegion().getByRole('button', { name: 'Save the copy' })
    );
    expect(await copyRegion().findByRole('alert')).toHaveTextContent(sentence);
  });

  it('asks to refresh when the ideas changed first', async () => {
    await openEdit(0);
    mockGateway.editMarketingIdeasCopy.mockResolvedValue(
      refused('stale_marketing_revision')
    );
    type('Body text', NEW.body_copy);
    fireEvent.click(
      copyRegion().getByRole('button', { name: 'Save the copy' })
    );
    const alert = await copyRegion().findByRole('alert');
    expect(alert).toHaveTextContent('These ideas just changed.');
    const calls = mockGateway.getMarketingIdeas.mock.calls.length;
    fireEvent.click(within(alert).getByRole('button', { name: 'Refresh' }));
    await waitFor(() =>
      expect(mockGateway.getMarketingIdeas.mock.calls.length).toBeGreaterThan(
        calls
      )
    );
  });

  it('cannot save empty copy, and can undo the changes', async () => {
    await openEdit(0);
    type('Headline', '   ');
    expect(
      copyRegion().getByRole('button', { name: 'Save the copy' })
    ).toBeDisabled();
    expect(
      copyRegion().getByText(
        'Write both a headline and body text, each up to 2000 characters.'
      )
    ).toBeVisible();
    expect(copyRegion().getByLabelText('Headline')).toHaveAttribute(
      'maxLength',
      '2000'
    );
    fireEvent.click(copyRegion().getByRole('button', { name: 'Undo changes' }));
    expect(copyRegion().getByLabelText('Headline')).toHaveValue(
      'Plug in. Go to bed.'
    );
    expect(mockGateway.editMarketingIdeasCopy).not.toHaveBeenCalled();
  });

  it("shows an uploaded image's copy without a form: its words are the user's own", async () => {
    await openEdit(2);
    expect(copyRegion().queryByRole('textbox')).toBeNull();
    expect(copyRegion().getByText('Rain or shine.')).toBeVisible();
    expect(
      copyRegion().getByText(/changing the copy doesn't affect it/)
    ).toBeVisible();
  });
});

describe('The ideas page after a copy change (#228)', () => {
  it('shows the new copy where its ad waits to be made, beside the images of the old copy', async () => {
    mockGateway.getMarketingIdeas.mockResolvedValue(ok(savedDetail()));
    render(
      <Fresh>
        <IdeasPage product={product()} />
      </Fresh>
    );
    await screen.findByRole('heading', { level: 2, name: /claims/ });
    const cards = screen.getAllByTestId('ad-card');
    // The images of the old copy keep their place and need making again.
    const waiting = cards.find((card) =>
      within(card).queryByText(NEW.headline)
    )!;
    expect(waiting).toHaveTextContent(`Headline: ${NEW.headline}`);
    expect(waiting).toHaveTextContent('Not made yet.');
    expect(
      within(waiting).getByRole('button', { name: 'Make this ad' })
    ).toBeEnabled();
    expect(
      cards.filter((card) => within(card).queryByText('Needs redoing'))
    ).toHaveLength(2);
  });
});
