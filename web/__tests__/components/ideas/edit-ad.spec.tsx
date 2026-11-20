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
  finding,
  generationCapabilities,
  ideasDetail,
  plannerCapabilities,
  product,
} from './fixtures';

/**
 * "Edit this ad" (ADR 0018, design §5.3, #200, #201): the ad's claim, way of
 * telling, facts and copy; its findings one by one; redo it from a note at a
 * quoted maximum, make it again, crop it for free or process it with the
 * image tools; every version stays, to compare or show.
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
const reason = (
  code: string,
  factIds: string[] = [],
  findingIds: string[] = []
) => ({
  code: code as 'facts_unverified',
  fact_ids: factIds,
  finding_ids: findingIds,
});
const preview = (id: string, amount: string, revision = 1) =>
  ok(
    {
      id,
      preview_hash: `${id}_hash`,
      draft_revision: revision,
      maximum_cost: { amount, currency: 'USD' },
    },
    201
  );

function editDetail() {
  return ideasDetail({
    ads: [
      ad('av_1', 'c1', {
        kept: true,
        cited_fact_ids: ['f1'],
        created_at: '2026-11-17T08:00:00+00:00',
        check: check('qc_1', [
          finding('copy_consistent', {
            gate_reason: 'rendered_text_mismatch',
            rationale: 'The headline in the image is shortened.',
            suggested_fix: 'Render the headline exactly as written.',
          }),
          finding('facts_supported', {
            status: 'unknown',
            gate_reason: 'claim_evidence_missing',
            unsupported_claims: ['Charges twice as fast'],
          }),
        ]),
        reasons: [
          reason('facts_unverified', ['f1']),
          reason('findings_open', [], ['copy_consistent', 'facts_supported']),
        ],
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
      ad('av_4', 'c4', {
        status: 'needs_redo',
        reasons: [reason('facts_removed', ['f9'])],
      }),
    ],
    pending: [],
  });
}

function Fresh({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

async function openEdit(index: number, detail = editDetail()) {
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

/** Opens a `<details>` as a browser does (jsdom fires no toggle on a click). */
function openDetails(summary: HTMLElement) {
  const details = summary.closest('details')!;
  details.open = true;
  fireEvent(details, new Event('toggle'));
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
  mockGateway.submitGenerationRun.mockResolvedValue({
    kind: 'accepted',
    status: 202,
    data: { operation_id: 'op_1', generation_run_id: 'run_1' },
    meta: {},
  });
});

describe('EditAd (#201)', () => {
  it('shows what the ad stands on, its copy, and each finding on its own', async () => {
    const { container } = await openEdit(0);
    expect(
      screen.getByText(
        'Claim 1 “Plug in at night and forget about it” · Way of telling: Bedtime story · Facts it uses: Schedule charging in the Acme Charge app'
      )
    ).toBeInTheDocument();
    // The copy can be changed here (#228); the old "not yet" note is gone.
    const copy = screen.getByRole('region', { name: 'Copy' });
    expect(within(copy).getByLabelText('Headline')).toHaveValue(
      'Plug in. Go to bed.'
    );
    expect(within(copy).getByLabelText('Body text')).toHaveValue(
      'Plug in. Go to bed. Acme Charge Home.'
    );
    expect(copy).not.toHaveTextContent(/isn't available yet/);
    const findings = within(
      screen.getByRole('region', { name: 'Check' })
    ).getAllByTestId('finding');
    expect(findings).toHaveLength(2);
    expect(findings[0]).toHaveTextContent(
      "The text in the image isn't the ad's copy"
    );
    expect(findings[0]).toHaveTextContent(
      'Suggested change: Render the headline exactly as written.'
    );
    expect(findings[1]).toHaveTextContent(
      "The image says something the facts don't back up"
    );
    expect(findings[1]).toHaveTextContent('Charges twice as fast');
    // Two versions: the original and its crop.
    const versions = screen.getByRole('region', { name: 'Versions (2)' });
    expect(
      within(versions).getAllByText('Version 2 · Cropped or rotated').length
    ).toBeGreaterThan(0);
    for (const id of ['av_1', 'av_2', 'qc_1', 'f1', 'c1', 'a1', 'mp_secret'])
      expect(container.textContent).not.toContain(id);
  });

  it('redoes the ad from a note at the preview maximum, and only the second click spends', async () => {
    await openEdit(0);
    fireEvent.click(screen.getByRole('button', { name: 'Product bigger' }));
    const note = screen.getByLabelText('Or say it in your own words');
    expect(note).toHaveValue('Make the product larger in the frame.');
    mockGateway.prepareMarketingIdeasFeedback.mockResolvedValue(
      ok(
        {
          id: 'mfd_1',
          project_id: 'mp_secret',
          draft_id: 'gd_fix',
          source_asset_version_id: 'av_1',
          combination_id: 'c1',
          kind: 'correction',
          finding_ids: ['copy_consistent'],
        },
        201
      )
    );
    mockGateway.createGenerationPreview.mockResolvedValue(
      preview('gp_fix', '0.06')
    );
    const redo = within(screen.getByRole('region', { name: 'Redo this ad' }));
    fireEvent.click(redo.getByRole('button', { name: 'Redo this one' }));
    const confirm = await redo.findByRole('button', {
      name: 'Redo this one · up to $0.06',
    });
    expect(mockGateway.prepareMarketingIdeasFeedback).toHaveBeenCalledWith(
      expect.objectContaining({
        body: {
          asset_version_id: 'av_1',
          note: 'Make the product larger in the frame.',
          provider: 'openai',
          model: 'image-model',
        },
      })
    );
    expect(mockGateway.submitGenerationRun).not.toHaveBeenCalled();
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(mockGateway.submitGenerationRun).toHaveBeenCalledWith(
        expect.objectContaining({
          body: { preview_id: 'gp_fix', preview_hash: 'gp_fix_hash' },
        })
      )
    );
    expect(await redo.findByRole('status')).toHaveTextContent(/Started/);
  });

  it('makes the ad again from its own draft at the current revision and image settings', async () => {
    await openEdit(0);
    mockGateway.prepareMarketingIdeasGeneration.mockResolvedValue(
      ok(
        {
          project_id: 'mp_secret',
          revision: 3,
          drafts: [
            { combination_id: 'c1', draft_id: 'gd_c1' },
            { combination_id: 'c2', draft_id: 'gd_c2' },
          ],
        },
        201
      )
    );
    mockGateway.createGenerationPreview.mockResolvedValue(
      preview('gp_c1', '0.04')
    );
    fireEvent.click(
      screen.getByRole('button', { name: 'Make it again as it is' })
    );
    const confirm = await screen.findByRole('button', {
      name: 'Make it again · up to $0.04',
    });
    expect(mockGateway.prepareMarketingIdeasGeneration).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      actionInstanceId: expect.any(String),
      expectedRevision: 3,
      projectId: 'mp_secret',
      body: {
        provider: 'openai',
        model: 'image-model',
        aspect_ratio: '4:5',
        image_size: '1K',
        variants: 1,
      },
    });
    expect(mockGateway.createGenerationPreview).toHaveBeenCalledWith(
      expect.objectContaining({ draftId: 'gd_c1' })
    );
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(mockGateway.submitGenerationRun).toHaveBeenCalled()
    );
  });

  it('crops for free and processes with the image tools at a quoted maximum', async () => {
    await openEdit(0);
    const work = within(
      screen.getByRole('region', { name: 'Work on this image' })
    );
    // Crop and rotate (the round's editor, with the project's command).
    openDetails(work.getByText('Crop and rotate'));
    fireEvent.change(
      work.getByRole('combobox', { name: 'Clockwise rotation' }),
      {
        target: { value: '90' },
      }
    );
    mockGateway.createMarketingIdeasImageEdit.mockResolvedValue(
      ok(
        {
          ...editDetail(),
          image_edits: [
            {
              id: 'mie_1',
              operation_id: 'op_e',
              source_asset_version_id: 'av_1',
              result_asset_version_id: null,
              state: 'queued',
              error_code: null,
            },
          ],
        },
        202
      )
    );
    fireEvent.click(
      work.getByRole('button', { name: 'Create edited version' })
    );
    await waitFor(() =>
      expect(mockGateway.createMarketingIdeasImageEdit).toHaveBeenCalledWith(
        expect.objectContaining({
          projectId: 'mp_secret',
          body: { asset_version_id: 'av_1', rotation: 90, crop: null },
        })
      )
    );
    expect(
      await work.findByText('Cropping… You can leave and come back.')
    ).toBeInTheDocument();

    // Image tools: the first click asks for the cost only.
    openDetails(work.getByText('Upscale, remove background or erase'));
    mockGateway.prepareMarketingIdeasFeedback.mockResolvedValue(
      ok(
        {
          id: 'mfd_2',
          project_id: 'mp_secret',
          draft_id: 'gd_tool',
          source_asset_version_id: 'av_1',
          combination_id: 'c1',
          kind: 'image_tool',
          finding_ids: [],
        },
        201
      )
    );
    mockGateway.createGenerationPreview.mockResolvedValue(
      preview('gp_tool', '0.08')
    );
    fireEvent.click(
      await work.findByRole('button', { name: 'See what it costs' })
    );
    const confirm = await work.findByRole('button', {
      name: 'Process the image · up to $0.08',
    });
    expect(mockGateway.prepareMarketingIdeasFeedback).toHaveBeenCalledWith(
      expect.objectContaining({
        body: expect.objectContaining({
          asset_version_id: 'av_1',
          image_tool: expect.objectContaining({ kind: 'upscale', scale: 2 }),
        }),
      })
    );
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(mockGateway.submitGenerationRun).toHaveBeenCalledWith(
        expect.objectContaining({
          body: { preview_id: 'gp_tool', preview_hash: 'gp_tool_hash' },
        })
      )
    );
  });

  it('says why an ad that needs redoing cannot be processed, and why a removed fact blocks redoing', async () => {
    // Its claim still names the deleted fact f9 (#224).
    const detail = editDetail();
    detail.ideas = {
      ...detail.ideas!,
      angles: detail.ideas!.angles.map((angle) =>
        angle.id === 'a2' ? { ...angle, fact_ids: ['f2', 'f9'] } : angle
      ),
    };
    await openEdit(3, detail);
    expect(
      screen.getAllByText(
        "This ad needs redoing, so cropping or processing it won't make it downloadable. Redo it with a note instead."
      ).length
    ).toBeGreaterThan(0);
    expect(screen.queryByText('Crop and rotate')).toBeNull();
    expect(
      screen.getByText(
        'This claim still uses a fact that was deleted, so a new image would need redoing too. Change the claim first and take that fact off.'
      )
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Redo this one' })).toBeNull();
    // Making it again as it is would need redoing too.
    expect(
      screen.queryByRole('button', { name: 'Make it again as it is' })
    ).toBeNull();
    expect(screen.getByText('A fact it uses was deleted.')).toBeInTheDocument();
    // The way out: change the claim, on the ideas page with its form open.
    fireEvent.click(screen.getByRole('button', { name: 'Change this claim' }));
    const form = await screen.findByRole('form', { name: 'Change claim 2' });
    expect(form).toHaveTextContent(
      'This claim still names a deleted fact. Saving takes it off.'
    );
  });

  it('lets an image of a claim changed since be redone once its claim no longer names the deleted fact', async () => {
    await openEdit(3);
    expect(
      screen.queryByText(/This claim still uses a fact that was deleted/)
    ).toBeNull();
    expect(
      screen.getByRole('button', { name: 'Make it again as it is' })
    ).toBeInTheDocument();
  });

  it('lets an uploaded image be cropped, never redone or processed', async () => {
    await openEdit(2);
    expect(
      screen.getByText(/Your own image can't be redone or processed by AI/)
    ).toBeInTheDocument();
    expect(screen.getByText('Crop and rotate')).toBeInTheDocument();
    expect(
      screen.queryByText('Upscale, remove background or erase')
    ).toBeNull();
    expect(screen.queryByRole('button', { name: 'Redo this one' })).toBeNull();
    expect(
      screen.queryByRole('button', { name: 'Make it again as it is' })
    ).toBeNull();
  });

  it('says to upload again when your own image needs redoing, never to redo it (#204)', async () => {
    const detail = ideasDetail({
      ads: [
        ad('av_3', 'c3', {
          origin: 'uploaded',
          used_product_photo: false,
          status: 'needs_redo',
          reasons: [reason('facts_changed', ['f1'])],
        }),
      ],
      pending: [],
    });
    await openEdit(0, detail);
    expect(
      screen.getByText(
        "This ad needs redoing, but your own image can't be redone or processed by AI: upload it again under the right way of telling."
      )
    ).toBeInTheDocument();
    expect(
      screen.queryByText(
        "This ad needs redoing, so cropping or processing it won't make it downloadable. Redo it with a note instead."
      )
    ).toBeNull();
    expect(screen.queryByRole('button', { name: 'Redo this one' })).toBeNull();
  });

  it("treats a crop of an uploaded image as the user's own image", async () => {
    const detail = ideasDetail({
      ads: [
        ad('av_3', 'c3', {
          origin: 'uploaded',
          used_product_photo: false,
          created_at: '2026-11-17T08:00:00+00:00',
        }),
        ad('av_5', 'c3', {
          origin: 'image_edit',
          source_asset_version_id: 'av_3',
          used_product_photo: false,
          created_at: '2026-11-17T09:00:00+00:00',
        }),
      ],
      pending: [],
    });
    await openEdit(1, detail);
    expect(
      screen.getByText(/Your own image can't be redone or processed by AI/)
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Redo this one' })).toBeNull();
    expect(
      screen.queryByText('Upscale, remove background or erase')
    ).toBeNull();
  });

  it('shows no copy as its own for an ad whose content is no longer in the ideas', async () => {
    const detail = ideasDetail({
      ads: [
        ad('av_1', 'c1', {
          status: 'needs_redo',
          reasons: [reason('ideas_changed')],
        }),
      ],
      pending: [],
    });
    await openEdit(0, detail);
    const copy = within(screen.getByRole('region', { name: 'Copy' }));
    expect(copy.getAllByText('No longer in the ideas')).toHaveLength(2);
    expect(copy.queryByText('Plug in. Go to bed.')).toBeNull();
    expect(
      screen.getByText(
        'It no longer matches its claim and copy as they are now.'
      )
    ).toBeInTheDocument();
  });

  it('shows another version, offers its check, and goes back to the ideas', async () => {
    await openEdit(0);
    const versions = screen.getByRole('region', { name: 'Versions (2)' });
    fireEvent.click(
      within(versions).getAllByRole('button', { name: 'Show this version' })[0]
    );
    expect(
      await within(screen.getByRole('region', { name: 'Check' })).findByText(
        "This image isn't checked yet."
      )
    ).toBeInTheDocument();
    expect(
      await screen.findByRole('button', { name: 'Check 2 ads · up to $0.04' })
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole('button', { name: '← Back to the ad ideas' })
    );
    expect(
      await screen.findByRole('heading', { level: 2, name: /claims/ })
    ).toBeInTheDocument();
  });
});
