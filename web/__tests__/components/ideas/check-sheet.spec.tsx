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
  check,
  checksQuote,
  finding,
  generationCapabilities,
  ideasDetail,
  plannerCapabilities,
  product,
} from './fixtures';

/**
 * The check before downloading (ADR 0018 §3, design §5.3, #198, #201): only
 * what the kept ads use, ticked once; confirming records the check, then
 * downloads each ad as its own file. Findings show one by one with three
 * ways to handle them.
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
  verifyMarketingIdeas: jest.fn(),
  downloadMarketingIdeas: jest.fn(),
  prepareMarketingIdeasFeedback: jest.fn(),
  getGenerationDraft: jest.fn(),
  createGenerationPreview: jest.fn(),
  submitGenerationRun: jest.fn(),
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
const mockDownloadBlob = jest.fn();
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => ({ downloadBlob: mockDownloadBlob }),
}));
const mockSaveBlob = jest.fn();
jest.mock('@/lib/ideas/save-file', () => ({
  saveBlob: (...args: unknown[]) => mockSaveBlob(...args),
}));
jest.mock('@/hooks/products/useWorkspaceProducts', () => ({
  useWorkspaceProducts: () => ({ refresh: jest.fn() }),
}));

function ok<T>(data: T, status = 200) {
  return { kind: 'ok' as const, status, data, meta: {} };
}
function refused(code: string, status = 409, params?: unknown) {
  return {
    kind: 'problem' as const,
    status,
    problem: {
      type: 'about:blank',
      title: '',
      status,
      detail: '',
      instance: '',
      code,
      trace_id: 't',
      retryable: false,
      field_errors: [],
      params,
    },
    meta: {},
  };
}

const open = (ids: string[]) => ({
  code: 'findings_open' as const,
  fact_ids: [],
  finding_ids: ids,
});
const reason = (code: string, factIds: string[] = []) => ({
  code: code as 'facts_unverified',
  fact_ids: factIds,
  finding_ids: [],
});

/** Two kept drafts (one made without a photo) and one ad not kept. */
function sheetDetail() {
  return ideasDetail({
    ads: [
      ad('av_1', 'c1', {
        kept: true,
        cited_fact_ids: ['f1'],
        check: check('qc_1', [
          finding('copy_consistent', {
            gate_reason: 'rendered_text_mismatch',
            rendered_text_diff: [
              {
                field: 'headline',
                expected: 'Plug in. Go to bed.',
                rendered: 'Plug in. Sleep.',
                changes: [],
              },
            ],
          }),
        ]),
        reasons: [
          reason('facts_unverified', ['f1']),
          open(['copy_consistent']),
        ],
      }),
      ad('av_3', 'c3', {
        kept: true,
        used_product_photo: false,
        cited_fact_ids: ['f2'],
        check: check('qc_3', [
          finding('product_fidelity', {
            status: 'unknown',
            gate_reason: 'product_reference_missing',
          }),
        ]),
        reasons: [
          reason('facts_unverified', ['f2']),
          reason('appearance_unconfirmed'),
          open(['product_fidelity']),
        ],
      }),
      ad('av_4', 'c5', { cited_fact_ids: ['f3'] }),
    ],
    pending: [],
  });
}

function exportable(detail = sheetDetail()) {
  return {
    ...detail,
    revision: detail.revision + 1,
    ads: detail.ads.map((item) =>
      item.kept ? { ...item, status: 'exportable' as const, reasons: [] } : item
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

async function openSheet(detail = sheetDetail()) {
  mockGateway.getMarketingIdeas.mockResolvedValue(ok(detail));
  const view = render(
    <Fresh>
      <IdeasPage product={product()} />
    </Fresh>
  );
  fireEvent.click(
    await screen.findByRole('button', { name: 'Download images only' })
  );
  await screen.findByRole('heading', {
    level: 1,
    name: 'Check once before downloading',
  });
  return view;
}

const confirmButton = () =>
  within(screen.getByTestId('check-confirm-bar')).getByRole('button');

beforeEach(() => {
  jest.clearAllMocks();
  mockGateway.getMarketingCapabilities.mockResolvedValue(
    ok(plannerCapabilities())
  );
  mockGateway.getGenerationCapabilities.mockResolvedValue(
    ok(generationCapabilities())
  );
  mockGateway.getMarketingIdeasChecksQuote.mockResolvedValue(
    ok(checksQuote({ quantity: 1, maximum_cost: '0.02' }))
  );
  mockDownloadBlob.mockResolvedValue(new Blob(['png']));
});

describe('CheckSheet (#201)', () => {
  it('lists only what the kept ads use and confirms only once everything is ticked', async () => {
    await openSheet();
    expect(
      screen.getByText(
        "Only what the 2 ads you kept use. What you check here doesn't need checking again."
      )
    ).toBeInTheDocument();
    // Facts: f1 and f2 (kept), not f3 (only the ad not kept cites it).
    const facts = screen.getByRole('region', { name: 'Facts to check (2)' });
    expect(within(facts).getAllByRole('checkbox')).toHaveLength(2);
    expect(
      within(facts).getByLabelText(/Schedule charging in the Acme Charge app/)
    ).toBeInTheDocument();
    expect(within(facts).queryByText(/Load balancing/)).toBeNull();
    // The no-photo ad: one look to confirm, not a fault.
    const looks = screen.getByRole('region', {
      name: 'Product photos and looks',
    });
    expect(
      within(looks).getByLabelText(/The product in this image looks right/)
    ).toBeInTheDocument();
    // The findings: only the copy mismatch, shown with what the image says.
    const findings = screen.getByRole('region', {
      name: 'What the checks found',
    });
    expect(within(findings).getAllByTestId('finding')).toHaveLength(1);
    expect(
      within(findings).getByText("The text in the image isn't the ad's copy")
    ).toBeInTheDocument();
    expect(
      within(findings).getByText(
        'The headline in the image reads “Plug in. Sleep.”; the copy says “Plug in. Go to bed.”.'
      )
    ).toBeInTheDocument();
    expect(
      within(findings).queryByText('Please confirm the product looks right')
    ).toBeNull();

    expect(confirmButton()).toHaveTextContent('Confirm and download 2 images');
    expect(confirmButton()).toBeDisabled();
    expect(screen.getByText('4 things left to tick.')).toBeInTheDocument();
    for (const box of within(facts).getAllByRole('checkbox'))
      fireEvent.click(box);
    fireEvent.click(within(looks).getByRole('checkbox'));
    expect(confirmButton()).toBeDisabled();
    fireEvent.click(
      within(findings).getByRole('button', { name: 'This is fine' })
    );
    expect(confirmButton()).toBeEnabled();
  });

  it('records the check first, then downloads each ad as its own file', async () => {
    await openSheet();
    const order: string[] = [];
    mockGateway.verifyMarketingIdeas.mockImplementation(async () => {
      order.push('verify');
      return ok(exportable());
    });
    mockGateway.downloadMarketingIdeas.mockImplementation(async () => {
      order.push('download');
      return ok(
        {
          id: 'mid_1',
          project_id: 'mp_secret',
          downloads: ['av_1', 'av_3'].map((id, i) => ({
            asset_version_id: id,
            combination_id: 'c1',
            filename: `ad-MK-00000${i}.png`,
            intent_id: `di_${i}`,
            expires_at: '2026-11-18T10:05:00+00:00',
            download_grant: {
              url: `https://files.example/${id}`,
              kind: 'signed_get',
              headers: {},
              content_disposition: 'attachment',
            },
          })),
        },
        201
      );
    });
    for (const box of screen.getAllByRole('checkbox')) fireEvent.click(box);
    fireEvent.click(screen.getByRole('button', { name: 'This is fine' }));
    fireEvent.click(confirmButton());

    expect(
      await screen.findByRole('heading', { name: 'Downloaded 2 images' })
    ).toBeInTheDocument();
    expect(order).toEqual(['verify', 'download']);
    expect(mockGateway.verifyMarketingIdeas).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      actionInstanceId: expect.any(String),
      expectedRevision: 3,
      projectId: 'mp_secret',
      body: {
        fact_ids: ['f1', 'f2'],
        appearance_confirmed: ['av_3'],
        accepted_findings: [
          {
            asset_version_id: 'av_1',
            check_id: 'qc_1',
            finding_ids: ['copy_consistent'],
          },
        ],
      },
    });
    expect(mockGateway.downloadMarketingIdeas).toHaveBeenCalledWith(
      expect.objectContaining({
        body: { asset_version_ids: ['av_1', 'av_3'] },
      })
    );
    await waitFor(() => expect(mockSaveBlob).toHaveBeenCalledTimes(2));
    expect(mockSaveBlob.mock.calls.map((call) => call[0])).toEqual([
      'ad-MK-000000.png',
      'ad-MK-000001.png',
    ]);
    expect(await screen.findAllByText('Downloaded')).toHaveLength(2);
  });

  it('downloads at once, without recording anything, when every kept ad is exportable', async () => {
    await openSheet(exportable());
    mockGateway.downloadMarketingIdeas.mockResolvedValue(
      ok({ id: 'mid_1', project_id: 'mp_secret', downloads: [] }, 201)
    );
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    fireEvent.click(confirmButton());
    await waitFor(() =>
      expect(mockGateway.downloadMarketingIdeas).toHaveBeenCalled()
    );
    expect(mockGateway.verifyMarketingIdeas).not.toHaveBeenCalled();
  });

  it('gives each finding three ways out: fine as it is, redo it at a quoted maximum, or leave it out', async () => {
    await openSheet();
    const block = screen.getByTestId('check-ad-findings');
    expect(
      within(block).getByRole('button', { name: 'This is fine' })
    ).toHaveAttribute('aria-pressed', 'false');
    expect(
      within(block).getByRole('button', { name: 'Redo this one' })
    ).toBeInTheDocument();
    expect(
      within(block).getByRole('button', { name: 'Leave this one out' })
    ).toBeInTheDocument();

    // Redo: the maximum comes from the preview; nothing is spent before the second click.
    mockGateway.prepareMarketingIdeasFeedback.mockResolvedValue(
      ok(
        {
          id: 'mfd_1',
          project_id: 'mp_secret',
          draft_id: 'gd_1',
          source_asset_version_id: 'av_1',
          combination_id: 'c1',
          kind: 'correction',
          finding_ids: ['copy_consistent'],
        },
        201
      )
    );
    mockGateway.getGenerationDraft.mockResolvedValue(
      ok({ id: 'gd_1', revision: 2 })
    );
    mockGateway.createGenerationPreview.mockResolvedValue(
      ok(
        {
          id: 'gp_1',
          preview_hash: 'ph_1',
          draft_revision: 2,
          maximum_cost: { amount: '0.06', currency: 'USD' },
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
    fireEvent.click(
      within(block).getByRole('button', { name: 'Redo this one' })
    );
    const confirm = await within(block).findByRole('button', {
      name: 'Redo this one · up to $0.06',
    });
    expect(mockGateway.prepareMarketingIdeasFeedback).toHaveBeenCalledWith(
      expect.objectContaining({
        projectId: 'mp_secret',
        body: {
          asset_version_id: 'av_1',
          note: '',
          provider: 'openai',
          model: 'image-model',
        },
      })
    );
    expect(mockGateway.createGenerationPreview).toHaveBeenCalledWith(
      expect.objectContaining({ draftId: 'gd_1', ifMatch: 2 })
    );
    expect(mockGateway.submitGenerationRun).not.toHaveBeenCalled();
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(mockGateway.submitGenerationRun).toHaveBeenCalledWith(
        expect.objectContaining({
          ifMatch: 2,
          body: { preview_id: 'gp_1', preview_hash: 'ph_1' },
        })
      )
    );

    // Leave it out: the ad is no longer kept, so it leaves this download.
    mockGateway.setMarketingAdKept.mockResolvedValue(ok(sheetDetail()));
    fireEvent.click(
      within(block).getByRole('button', { name: 'Leave this one out' })
    );
    await waitFor(() =>
      expect(mockGateway.setMarketingAdKept).toHaveBeenCalledWith(
        expect.objectContaining({
          assetVersionId: 'av_1',
          body: { kept: false },
        })
      )
    );
  });

  it('names each refused ad by its picture and says why, never by an id', async () => {
    const { container } = await openSheet();
    mockGateway.verifyMarketingIdeas.mockResolvedValue(ok(exportable()));
    mockGateway.downloadMarketingIdeas.mockResolvedValue(
      refused('ads_not_exportable', 409, {
        ads: [
          {
            asset_version_id: 'av_3',
            status: 'draft',
            reasons: [reason('appearance_unconfirmed')],
          },
        ],
      })
    );
    for (const box of screen.getAllByRole('checkbox')) fireEvent.click(box);
    fireEvent.click(screen.getByRole('button', { name: 'This is fine' }));
    fireEvent.click(confirmButton());
    const alert = await screen.findByText("These ads can't be downloaded:");
    const box = alert.closest('section')!;
    expect(
      within(box).getByRole('img', { name: 'Rain or shine.' })
    ).toHaveAttribute('src', 'blob:image-av_3');
    expect(
      within(box).getByText(
        'Made without a product photo: confirm the product looks right.'
      )
    ).toBeInTheDocument();
    const text = container.textContent ?? '';
    for (const id of ['av_1', 'av_3', 'qc_1', 'f1', 'mp_secret'])
      expect(text).not.toContain(id);
  });

  it('keeps an unchecked kept ad out of this download and offers its check at the quoted maximum', async () => {
    const detail = sheetDetail();
    detail.ads.push(
      ad('av_7', 'c6', {
        kept: true,
        origin: 'corrected',
        source_asset_version_id: 'av_x',
        cited_fact_ids: ['f3'],
        reasons: [reason('check_missing')],
      })
    );
    await openSheet(detail);
    const later = screen.getByRole('region', {
      name: "Can't be downloaded this time (1)",
    });
    expect(
      within(later).getByText('Not checked yet. Check it first.')
    ).toBeInTheDocument();
    const start = await within(later).findByRole('button', {
      name: 'Check 1 ad · up to $0.02',
    });
    mockGateway.startMarketingIdeasChecks.mockResolvedValue(ok(detail, 202));
    fireEvent.click(start);
    await waitFor(() =>
      expect(mockGateway.startMarketingIdeasChecks).toHaveBeenCalledWith(
        expect.objectContaining({
          body: { execution_mode: 'byok', quote_hash: 'q'.repeat(64) },
        })
      )
    );
    // The confirmation still covers the two ads it can download.
    expect(confirmButton()).toHaveTextContent('Confirm and download 2 images');
  });
});
