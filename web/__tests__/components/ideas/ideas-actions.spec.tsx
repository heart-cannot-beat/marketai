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
import { IdeasPage } from '@/components/ideas/IdeasPage';
import {
  ad,
  checksQuote,
  generationCapabilities,
  ideasDetail,
  plannerCapabilities,
  product,
} from './fixtures';

/**
 * The buttons S8 left out of the ideas page (#196, #201): another claim and
 * another way of telling at the planner's maximum, the user's own image,
 * making or retrying an image at the preview's maximum, checking new images
 * at the checks quote's maximum, image settings and guiding questions.
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
  startMarketingIdeasPlanning: jest.fn(),
  prepareMarketingIdeasGeneration: jest.fn(),
  getGenerationDraft: jest.fn(),
  createGenerationPreview: jest.fn(),
  submitGenerationRun: jest.fn(),
  placeMarketingIdeasUpload: jest.fn(),
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
jest.mock('@/hooks/products/useWorkspaceProducts', () => ({
  useWorkspaceProducts: () => ({ refresh: jest.fn() }),
}));
const mockUpload = {
  phase: 'idle' as string,
  creatives: [] as unknown[],
  uploadProgress: null,
  problem: null,
  confirming: null,
  submit: jest.fn(),
  openPending: jest.fn(),
  confirm: jest.fn(),
  reset: jest.fn(),
};
jest.mock('@/hooks/external-creatives/useExternalCreatives', () => ({
  useExternalCreatives: () => mockUpload,
}));

/** A domain answer as the gateway gives it: 200 `ok`, 201 `created`, 202 `accepted`. */
function ok<T>(data: T, status = 200) {
  const kind = status === 201 ? 'created' : status === 202 ? 'accepted' : 'ok';
  return { kind, status, data, meta: {} };
}

function Fresh({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

async function renderPage(detail = ideasDetail()) {
  mockGateway.getMarketingIdeas.mockResolvedValue(ok(detail));
  const view = render(
    <Fresh>
      <IdeasPage product={product()} />
    </Fresh>
  );
  await screen.findByRole('heading', { level: 2, name: /claims/ });
  return view;
}

const claim = (number: number) =>
  screen.getByRole('region', { name: new RegExp(`^Claim ${number}`) });

const call = {
  id: 'pc_2',
  operation_id: 'op_2',
  kind: 'ideas' as const,
  state: 'queued' as const,
  execution_mode: 'byok' as const,
  error_code: null,
  maximum_cost: '0.08',
};

beforeEach(() => {
  jest.clearAllMocks();
  mockUpload.phase = 'idle';
  mockUpload.creatives = [];
  mockGateway.getMarketingCapabilities.mockResolvedValue(
    ok(plannerCapabilities('0.08'))
  );
  mockGateway.getGenerationCapabilities.mockResolvedValue(
    ok(generationCapabilities())
  );
  mockGateway.getMarketingIdeasChecksQuote.mockResolvedValue(ok(checksQuote()));
  mockGateway.startMarketingIdeasPlanning.mockResolvedValue(ok(call, 202));
  mockGateway.getGenerationDraft.mockImplementation(async ({ draftId }) =>
    ok({ id: draftId, revision: 1 })
  );
  mockGateway.submitGenerationRun.mockResolvedValue({
    kind: 'accepted',
    status: 202,
    data: { operation_id: 'op_1', generation_run_id: 'run_1' },
    meta: {},
  });
});

describe('ideas page actions (#201)', () => {
  it('thinks of another claim at the planner maximum, in one click', async () => {
    await renderPage();
    const add = await screen.findByRole('button', {
      name: 'Think of another claim · up to $0.08',
    });
    fireEvent.click(add);
    await waitFor(() =>
      expect(mockGateway.startMarketingIdeasPlanning).toHaveBeenCalledWith({
        workspaceSlug: 'acme',
        actionInstanceId: expect.any(String),
        expectedRevision: 3,
        projectId: 'mp_secret',
        body: {
          kind: 'ideas',
          strategy_language: 'en-US',
          execution_mode: 'byok',
          quote_hash: 'pq_1',
          addition: { kind: 'claim', note: '', guiding_answers: [] },
        },
      })
    );
    // The page asks again so the drafting call shows.
    await waitFor(() =>
      expect(mockGateway.getMarketingIdeas).toHaveBeenCalledTimes(2)
    );
  });

  it('adds a way of telling of a chosen type, never one the claim already uses', async () => {
    await renderPage();
    const first = within(claim(1));
    expect(
      await first.findByRole('button', {
        name: 'Another way of telling · up to $0.08',
      })
    ).toBeEnabled();
    fireEvent.click(first.getByRole('button', { name: 'Choose the type' }));
    const type = first.getByRole('combobox', { name: 'Type of telling' });
    const options = within(type).getAllByRole('option') as HTMLOptionElement[];
    // Ten types and "describe my own".
    expect(options).toHaveLength(11);
    fireEvent.change(type, { target: { value: 'feature_closeup' } });
    fireEvent.change(first.getByLabelText('Anything to add (optional)'), {
      target: { value: 'Show the cable holster.' },
    });
    fireEvent.click(
      first.getByRole('button', {
        name: 'Add this way of telling · up to $0.08',
      })
    );
    await waitFor(() =>
      expect(mockGateway.startMarketingIdeasPlanning).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            addition: {
              kind: 'expression',
              angle_id: 'a1',
              note: 'Use the visual mechanism "feature_closeup" for this way of telling. Show the cable holster.',
              guiding_answers: [],
            },
          }),
        })
      )
    );
  });

  it('marks the types a claim already has, and asks for a description of a new one', async () => {
    const detail = ideasDetail();
    detail.ideas = {
      ...detail.ideas!,
      angles: detail.ideas!.angles.map((angle) =>
        angle.id === 'a1'
          ? {
              ...angle,
              expressions: angle.expressions.map((e, i) => ({
                ...e,
                visual_mechanism: i === 0 ? 'scenario_story' : 'qa_objection',
              })),
            }
          : angle
      ),
    };
    await renderPage(detail);
    const first = within(claim(1));
    fireEvent.click(first.getByRole('button', { name: 'Choose the type' }));
    const type = first.getByRole('combobox', { name: 'Type of telling' });
    const used = (within(type).getAllByRole('option') as HTMLOptionElement[])
      .filter((option) => option.disabled)
      .map((option) => option.value);
    expect(used.sort()).toEqual(['qa_objection', 'scenario_story']);
    fireEvent.change(type, { target: { value: 'other' } });
    expect(
      await first.findByRole('button', {
        name: 'Add this way of telling · up to $0.08',
      })
    ).toBeDisabled();
    fireEvent.change(first.getByLabelText('Describe the way of telling'), {
      target: { value: 'A before-and-after of the garage wall.' },
    });
    expect(
      first.getByRole('button', {
        name: 'Add this way of telling · up to $0.08',
      })
    ).toBeEnabled();
  });

  it('adds nothing while the first batch is still making its ads', async () => {
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
      (
        await screen.findAllByText(
          'You can add more once the first ads are done.'
        )
      ).length
    ).toBeGreaterThan(0);
    expect(
      await screen.findByRole('button', {
        name: 'Think of another claim · up to $0.08',
      })
    ).toBeDisabled();
  });

  it('sends the guiding answers with the next addition', async () => {
    const detail = ideasDetail();
    detail.ideas = {
      ...detail.ideas!,
      guiding_questions: [
        {
          id: 'g1',
          question: 'Do your buyers already own an electric car?',
          options: ['Most already do', 'Most are about to buy'],
          multiple: false,
          why_needed: 'It changes what the ads should explain.',
          selected: [],
          custom_answer: '',
        },
      ],
    };
    await renderPage(detail);
    fireEvent.click(screen.getByRole('button', { name: 'Answer' }));
    fireEvent.click(screen.getByLabelText('Most are about to buy'));
    fireEvent.click(
      await screen.findByRole('button', {
        name: 'Think of another claim · up to $0.08',
      })
    );
    await waitFor(() =>
      expect(mockGateway.startMarketingIdeasPlanning).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            addition: {
              kind: 'claim',
              note: '',
              guiding_answers: [
                {
                  question: 'Do your buyers already own an electric car?',
                  answer: 'Most are about to buy',
                },
              ],
            },
          }),
        })
      )
    );
  });

  it('tries a failed image again outside the first batch: quoted first, spent on the second click', async () => {
    mockGateway.prepareMarketingIdeasGeneration.mockResolvedValue(
      ok(
        {
          project_id: 'mp_secret',
          revision: 3,
          drafts: [
            { combination_id: 'c5', draft_id: 'gd_c5' },
            { combination_id: 'c6', draft_id: 'gd_c6' },
          ],
        },
        201
      )
    );
    mockGateway.createGenerationPreview.mockResolvedValue(
      ok(
        {
          id: 'gp_c6',
          preview_hash: 'h6',
          draft_revision: 1,
          maximum_cost: { amount: '0.04', currency: 'USD' },
        },
        201
      )
    );
    await renderPage();
    const [, failed] = within(claim(3)).getAllByTestId('ad-card');
    fireEvent.click(within(failed).getByRole('button', { name: 'Try again' }));
    const confirm = await within(failed).findByRole('button', {
      name: 'Try again · up to $0.04',
    });
    expect(mockGateway.createGenerationPreview).toHaveBeenCalledWith(
      expect.objectContaining({ draftId: 'gd_c6', ifMatch: 1 })
    );
    expect(mockGateway.submitGenerationRun).not.toHaveBeenCalled();
    expect(
      within(failed).queryByText("Trying again from here isn't available yet.")
    ).toBeNull();
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(mockGateway.submitGenerationRun).toHaveBeenCalledWith(
        expect.objectContaining({
          body: { preview_id: 'gp_c6', preview_hash: 'h6' },
        })
      )
    );
  });

  it("makes a new way of telling's image with the image settings the user chose", async () => {
    const detail = ideasDetail({
      ads: [ad('av_1', 'c1')],
      pending: [],
    });
    mockGateway.prepareMarketingIdeasGeneration.mockResolvedValue(
      ok(
        {
          project_id: 'mp_secret',
          revision: 3,
          drafts: [{ combination_id: 'c2', draft_id: 'gd_c2' }],
        },
        201
      )
    );
    mockGateway.createGenerationPreview.mockResolvedValue(
      ok(
        {
          id: 'gp_c2',
          preview_hash: 'h2',
          draft_revision: 1,
          maximum_cost: { amount: '0.12', currency: 'USD' },
        },
        201
      )
    );
    await renderPage(detail);
    const settings = screen.getByRole('region', { name: 'Image settings' });
    expect(within(settings).getByText('OpenAI')).toBeInTheDocument();
    expect(within(settings).getByText('4:5 portrait')).toBeInTheDocument();
    fireEvent.click(
      within(settings).getByRole('button', { name: 'Change image settings' })
    );
    fireEvent.change(
      within(settings).getByRole('combobox', { name: 'Aspect ratio' }),
      { target: { value: '1:1' } }
    );
    fireEvent.change(
      within(settings).getByRole('combobox', { name: 'Per way of telling' }),
      { target: { value: '2' } }
    );
    const [, notMade] = within(claim(1)).getAllByTestId('ad-card');
    fireEvent.click(
      within(notMade).getByRole('button', { name: 'Make this ad' })
    );
    expect(
      await within(notMade).findByRole('button', {
        name: 'Make this ad · up to $0.12',
      })
    ).toBeInTheDocument();
    expect(mockGateway.prepareMarketingIdeasGeneration).toHaveBeenCalledWith(
      expect.objectContaining({
        body: {
          provider: 'openai',
          model: 'image-model',
          aspect_ratio: '1:1',
          image_size: '1K',
          variants: 2,
        },
      })
    );
  });

  it('makes an image with the settings this version was prepared with when the chosen ones are refused', async () => {
    mockGateway.getMarketingFirstBatch.mockResolvedValue(
      ok({
        id: 'mfb_1',
        state: 'done',
        image_provider: 'openai',
        image_model: 'image-model',
      })
    );
    mockGateway.prepareMarketingIdeasGeneration.mockImplementation(
      async ({ body }) =>
        body.aspect_ratio === '4:5'
          ? ok(
              {
                project_id: 'mp_secret',
                revision: 3,
                drafts: [{ combination_id: 'c6', draft_id: 'gd_c6' }],
              },
              201
            )
          : {
              kind: 'problem',
              status: 409,
              problem: {
                type: 'about:blank',
                title: '',
                status: 409,
                detail: '',
                instance: '',
                code: 'ideas_drafts_already_prepared',
                trace_id: 't',
                retryable: false,
                field_errors: [],
              },
              meta: {},
            }
    );
    mockGateway.createGenerationPreview.mockResolvedValue(
      ok(
        {
          id: 'gp_c6',
          preview_hash: 'h6',
          draft_revision: 1,
          maximum_cost: { amount: '0.04', currency: 'USD' },
        },
        201
      )
    );
    await renderPage(
      ideasDetail({
        first_batch: {
          id: 'mfb_1',
          state: 'done',
          needs_attention: false,
          reason: null,
        },
      })
    );
    const settings = screen.getByRole('region', { name: 'Image settings' });
    fireEvent.click(
      await within(settings).findByRole('button', {
        name: 'Change image settings',
      })
    );
    fireEvent.change(
      within(settings).getByRole('combobox', { name: 'Aspect ratio' }),
      { target: { value: '1:1' } }
    );
    const [, failed] = within(claim(3)).getAllByTestId('ad-card');
    fireEvent.click(within(failed).getByRole('button', { name: 'Try again' }));
    expect(
      await within(failed).findByRole('button', {
        name: 'Try again · up to $0.04',
      })
    ).toBeInTheDocument();
    expect(
      mockGateway.prepareMarketingIdeasGeneration.mock.calls.map(
        ([call]) => call.body.aspect_ratio
      )
    ).toEqual(['1:1', '4:5']);
  });

  it('offers to check new images at the quoted maximum', async () => {
    const detail = ideasDetail({
      ads: [
        ad('av_1', 'c1'),
        ad('av_2', 'c1', {
          origin: 'corrected',
          source_asset_version_id: 'av_1',
          reasons: [{ code: 'check_missing', fact_ids: [], finding_ids: [] }],
        }),
      ],
      pending: [],
    });
    mockGateway.startMarketingIdeasChecks.mockResolvedValue(ok(detail, 202));
    await renderPage(detail);
    expect(
      screen.getByText(
        "Some new images aren't checked yet. New images are checked only when you ask."
      )
    ).toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole('button', { name: 'Check 2 ads · up to $0.04' })
    );
    await waitFor(() =>
      expect(mockGateway.startMarketingIdeasChecks).toHaveBeenCalledWith({
        workspaceSlug: 'acme',
        actionInstanceId: expect.any(String),
        projectId: 'mp_secret',
        body: { execution_mode: 'byok', quote_hash: 'q'.repeat(64) },
      })
    );
  });

  it("shows the first batch's ads as still being checked, not as clear", async () => {
    await renderPage(
      ideasDetail({
        first_batch: {
          id: 'mfb_1',
          state: 'checking' as unknown as 'done',
          needs_attention: false,
          reason: null,
        },
        pending: [],
      })
    );
    expect(
      screen.getByText(
        'Every ad is made. The checks are still running; each result appears on its ad as it finishes.'
      )
    ).toBeInTheDocument();
    expect(screen.getAllByText('Still being checked…')).toHaveLength(4);
  });

  it("uploads the user's own image and places it under the chosen way of telling", async () => {
    const view = await renderPage();
    fireEvent.click(
      screen.getByRole('button', { name: 'Upload my own image' })
    );
    const form = within(screen.getByTestId('upload-own'));
    fireEvent.change(form.getByRole('combobox', { name: 'Put it under' }), {
      target: { value: 'c4' },
    });
    const file = new File(['png'], 'own.png', { type: 'image/png' });
    fireEvent.change(form.getByLabelText(/Image \(PNG/), {
      target: { files: [file] },
    });
    const start = form.getByRole('button', { name: 'Upload and place it' });
    expect(start).toBeDisabled();
    fireEvent.click(
      form.getByLabelText('I have the right to use this image in ads')
    );
    fireEvent.click(start);
    expect(mockUpload.submit).toHaveBeenCalledWith({
      productId: 'prod_home',
      files: [file],
      madeBy: 'self',
      permissionConfirmed: true,
    });
    // The upload's own checks settle; the image is then placed, once.
    mockUpload.phase = 'review';
    mockUpload.creatives = [
      {
        external_creative_id: 'xc_1',
        status: 'ready',
        asset_version_id: 'av_up',
      },
    ];
    mockGateway.placeMarketingIdeasUpload.mockResolvedValue(
      ok(ideasDetail(), 201)
    );
    view.rerender(
      <Fresh>
        <IdeasPage product={product()} />
      </Fresh>
    );
    await waitFor(() =>
      expect(mockGateway.placeMarketingIdeasUpload).toHaveBeenCalledWith({
        workspaceSlug: 'acme',
        actionInstanceId: expect.any(String),
        projectId: 'mp_secret',
        body: { external_creative_id: 'xc_1', combination_id: 'c4' },
      })
    );
    expect(
      await screen.findByText(
        "Your image is placed. It's a draft until it's checked."
      )
    ).toBeInTheDocument();
  });
});
