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
import { ProductProfile } from '@/components/ideas/ProductProfile';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import {
  ad,
  check,
  generationCapabilities,
  ideasDetail,
  photo,
  plannerCapabilities,
  product,
} from './fixtures';

/**
 * Product photos in the check before downloading (#199, #229): an ad made
 * with an unchecked photo becomes downloadable once the photo is checked
 * here, with no redo; rejecting a photo asks first and makes the ads made
 * with it need redoing. The ideas page and the edit screen say why such an
 * ad needs redoing and what to do; the product profile shows the same photo
 * the same way.
 */
type Detail = components['schemas']['IdeasProjectDetail'];

jest.mock('next/router', () => ({
  useRouter: () => ({ asPath: '/w/acme/p/prod_home/ideas', query: {} }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'ws_1', role: 'OWNER' },
  }),
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
  editMarketingIdeasUnderstanding: jest.fn(),
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
jest.mock('@/hooks/products/useIntakeImageUrl', () => ({
  useIntakeImageUrl: (intakeId: string, imageId: string) => ({
    blobUrl: `blob:intake-${intakeId}-${imageId}`,
    isLoading: false,
    failed: false,
  }),
}));
/** The reads the profile's brand and product list make; none matter here. */
jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: () => ({
    data: undefined,
    problem: undefined,
    isLoading: false,
    isStale: false,
    lastSuccessfulAt: null,
    refresh: jest.fn(),
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

/** A domain answer as the gateway gives it: 200 `ok`, 201 `created`. */
function ok<T>(data: T, status = 200) {
  const kind = status === 201 ? 'created' : 'ok';
  return { kind, status, data, meta: {} };
}
function refused(code: string, status = 409) {
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
    },
    meta: {},
  };
}

const reason = (code: string) => ({
  code: code as 'photo_unverified',
  fact_ids: [],
  finding_ids: [],
});

/**
 * Two kept ads made with the unchecked main photo p1, their check clear and
 * their facts checked, so only the photo keeps them drafts; a third ad on
 * p1 is not kept. p2, a close-up nothing used, is unchecked too.
 */
function photoDetail(): Detail {
  return ideasDetail({
    photos: [
      photo('p1', { asset_version_ids: ['av_1', 'av_2', 'av_4'] }),
      photo('p2', { role: 'detail', image_id: 'img_p2' }),
    ],
    ads: [
      ad('av_1', 'c1', {
        kept: true,
        photo_ids: ['p1'],
        cited_fact_ids: ['f3'],
        check: check('qc_1'),
        reasons: [reason('photo_unverified')],
      }),
      ad('av_2', 'c2', {
        kept: true,
        photo_ids: ['p1'],
        cited_fact_ids: ['f3'],
        check: check('qc_2'),
        reasons: [reason('photo_unverified')],
      }),
      ad('av_4', 'c4', {
        photo_ids: ['p1'],
        check: check('qc_4'),
        reasons: [reason('photo_unverified')],
      }),
    ],
    pending: [],
  });
}

/** The answer once p1 is checked: the same images, now exportable. */
function checked(detail = photoDetail()): Detail {
  return {
    ...detail,
    revision: detail.revision + 1,
    photos: detail.photos!.map((item) =>
      item.photo_id === 'p1'
        ? { ...item, status: 'verified' as const, reference_version_id: 'rv_1' }
        : item
    ),
    ads: detail.ads.map((item) => ({
      ...item,
      status: 'exportable' as const,
      reasons: [],
    })),
  };
}

/** The answer once p1 is rejected: every ad made with it needs redoing. */
function rejected(detail = photoDetail()): Detail {
  return {
    ...detail,
    revision: detail.revision + 1,
    photos: detail.photos!.map((item) =>
      item.photo_id === 'p1' ? { ...item, status: 'rejected' as const } : item
    ),
    ads: detail.ads.map((item) => ({
      ...item,
      status: 'needs_redo' as const,
      reasons: [reason('photo_unavailable')],
    })),
  };
}

function Fresh({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

async function openSheet(detail = photoDetail()) {
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

const DECLARATION =
  'This is a real photo of Acme Charge Home, and I have the right to use it in ads.';
const confirmButton = () =>
  within(screen.getByTestId('check-confirm-bar')).getByRole('button');
const looks = () =>
  screen.getByRole('region', { name: 'Product photos and looks' });

beforeEach(() => {
  jest.clearAllMocks();
  mockGateway.getMarketingCapabilities.mockResolvedValue(
    ok(plannerCapabilities())
  );
  mockGateway.getGenerationCapabilities.mockResolvedValue(
    ok(generationCapabilities())
  );
  mockDownloadBlob.mockResolvedValue(new Blob(['png']));
});

describe('product photos in the check before downloading (#229)', () => {
  it('checks an unchecked photo here, then downloads the same images with no redo', async () => {
    const { container } = await openSheet();
    const section = looks();
    const rows = within(section).getAllByTestId('check-photo');
    // Only the photo the kept ads used, not the close-up nothing used.
    expect(rows).toHaveLength(1);
    expect(
      within(rows[0]).getByRole('img', { name: 'Main photo' })
    ).toHaveAttribute('src', 'blob:intake-pin_secret-img_p1');
    expect(
      within(rows[0]).getByText(
        "Main photo · From the product page · Used in 2 ads you're downloading"
      )
    ).toBeInTheDocument();
    // The tick is the declaration itself, written out.
    const box = within(rows[0]).getByRole('checkbox', {
      name: new RegExp(DECLARATION.replace(/[.,]/g, '.')),
    });
    expect(
      within(rows[0]).getByText(
        "Once checked, the ads made with it don't need redoing."
      )
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('region', { name: /Can't be downloaded/ })
    ).toBeNull();
    expect(confirmButton()).toHaveTextContent('Confirm and download 2 images');
    expect(confirmButton()).toBeDisabled();

    fireEvent.click(box);
    expect(confirmButton()).toBeEnabled();
    mockGateway.verifyMarketingIdeas.mockResolvedValue(ok(checked()));
    mockGateway.downloadMarketingIdeas.mockResolvedValue(
      ok(
        {
          record_id: 'mie_1',
          downloads: ['av_1', 'av_2'].map((id) => ({
            asset_version_id: id,
            filename: `ad-${id}.png`,
            download_grant: { url: `https://storage.example/${id}` },
          })),
        },
        201
      )
    );
    fireEvent.click(confirmButton());

    expect(
      await screen.findByRole('heading', { name: 'Downloaded 2 images' })
    ).toBeInTheDocument();
    expect(mockGateway.verifyMarketingIdeas).toHaveBeenCalledTimes(1);
    expect(mockGateway.verifyMarketingIdeas).toHaveBeenCalledWith(
      expect.objectContaining({
        projectId: 'mp_secret',
        expectedRevision: 3,
        body: {
          fact_ids: [],
          appearance_confirmed: [],
          accepted_findings: [],
          photo_ids: ['p1'],
        },
      })
    );
    expect(mockGateway.downloadMarketingIdeas).toHaveBeenCalledWith(
      expect.objectContaining({ body: { asset_version_ids: ['av_1', 'av_2'] } })
    );
    // Nothing was made again.
    expect(mockGateway.prepareMarketingIdeasFeedback).not.toHaveBeenCalled();
    expect(mockGateway.createGenerationPreview).not.toHaveBeenCalled();
    expect(mockGateway.submitGenerationRun).not.toHaveBeenCalled();
    expect(mockSaveBlob).toHaveBeenCalledTimes(2);
    expect(container.textContent).not.toMatch(/mp_secret|av_\d|\bp\d\b|img_/);
  });

  it('asks before rejecting a photo, then the ads made with it need redoing', async () => {
    await openSheet();
    const row = within(looks()).getByTestId('check-photo');
    fireEvent.click(
      within(row).getByRole('button', {
        name: "Not this product, or can't be used",
      })
    );
    const question = within(row).getByRole('group', {
      name: 'Reject this photo?',
    });
    expect(question).toHaveTextContent(
      "3 ads were made with it. Once you reject it, they need redoing and can't be downloaded."
    );
    expect(question).toHaveTextContent(
      '2 of them were in this download and leave it.'
    );
    expect(question).toHaveTextContent(
      'Nothing new is made with it. An ad made again uses the photos you can still use.'
    );
    // Asking sends nothing; cancelling leaves the photo as it was.
    expect(mockGateway.verifyMarketingIdeas).not.toHaveBeenCalled();
    fireEvent.click(within(question).getByRole('button', { name: 'Cancel' }));
    expect(within(row).queryByRole('group')).toBeNull();

    fireEvent.click(
      within(row).getByRole('button', {
        name: "Not this product, or can't be used",
      })
    );
    mockGateway.verifyMarketingIdeas.mockResolvedValue(ok(rejected()));
    fireEvent.click(
      within(row).getByRole('button', { name: 'Reject this photo' })
    );

    const later = await screen.findByRole('region', {
      name: "Can't be downloaded this time (2)",
    });
    expect(mockGateway.verifyMarketingIdeas).toHaveBeenCalledWith(
      expect.objectContaining({
        expectedRevision: 3,
        body: {
          fact_ids: [],
          appearance_confirmed: [],
          accepted_findings: [],
          photo_ids: [],
          rejected_photo_ids: ['p1'],
        },
      })
    );
    expect(
      within(later).getAllByText(
        "A product photo it used was rejected or can't be used any more. Make it again: the new image won't use that photo."
      )
    ).toHaveLength(2);
    expect(within(looks()).queryByTestId('check-photo')).toBeNull();
    expect(confirmButton()).toBeDisabled();
    expect(mockGateway.downloadMarketingIdeas).not.toHaveBeenCalled();
  });

  it('someone changed it first: says so, shows the ideas as they are now, and offers a refresh', async () => {
    await openSheet();
    const row = within(looks()).getByTestId('check-photo');
    fireEvent.click(
      within(row).getByRole('button', {
        name: "Not this product, or can't be used",
      })
    );
    mockGateway.verifyMarketingIdeas.mockResolvedValue(
      refused('stale_marketing_revision')
    );
    fireEvent.click(
      within(row).getByRole('button', { name: 'Reject this photo' })
    );
    const alert = await within(row).findByRole('alert');
    expect(alert).toHaveTextContent(
      'These ideas changed in the meantime, maybe because someone else checked something. Refresh to see them as they are now, then try again.'
    );
    await waitFor(() =>
      expect(mockGateway.getMarketingIdeas).toHaveBeenCalledTimes(2)
    );
    fireEvent.click(within(alert).getByRole('button', { name: 'Refresh' }));
    await waitFor(() =>
      expect(mockGateway.getMarketingIdeas).toHaveBeenCalledTimes(3)
    );
  });

  it('no answer: says so, and rejecting again repeats the same request', async () => {
    await openSheet();
    const row = within(looks()).getByTestId('check-photo');
    fireEvent.click(
      within(row).getByRole('button', {
        name: "Not this product, or can't be used",
      })
    );
    mockGateway.verifyMarketingIdeas.mockRejectedValueOnce(
      new Error('network')
    );
    fireEvent.click(
      within(row).getByRole('button', { name: 'Reject this photo' })
    );
    expect(await within(row).findByRole('alert')).toHaveTextContent(
      "No answer came back, so it's unclear whether this went through. Trying again repeats the same request; it won't happen twice."
    );
    mockGateway.verifyMarketingIdeas.mockResolvedValue(ok(rejected()));
    fireEvent.click(
      within(row).getByRole('button', { name: 'Reject this photo' })
    );
    await screen.findByRole('region', {
      name: "Can't be downloaded this time (2)",
    });
    const [first, second] = mockGateway.verifyMarketingIdeas.mock.calls;
    expect(second[0].actionInstanceId).toBe(first[0].actionInstanceId);
    expect(second[0].body).toEqual(first[0].body);
  });
});

describe('an ad whose photo was rejected (#229)', () => {
  it('the ideas page and the edit screen say why it needs redoing and what to do', async () => {
    mockGateway.getMarketingIdeas.mockResolvedValue(ok(rejected()));
    render(
      <Fresh>
        <IdeasPage product={product()} />
      </Fresh>
    );
    const cards = await screen.findAllByTestId('ad-card');
    const card = cards.find((item) =>
      within(item).queryByRole('img', { name: 'Plug in. Go to bed.' })
    )!;
    expect(within(card).getByText('Needs redoing')).toBeInTheDocument();
    expect(
      within(card).getByText(
        "A product photo it used was rejected or can't be used any more. Use “Edit” to make it again without that photo."
      )
    ).toBeInTheDocument();

    fireEvent.click(within(card).getByRole('button', { name: 'Edit' }));
    const edit = await screen.findByTestId('edit-ad');
    expect(
      within(edit).getByText(
        "A product photo this ad used was rejected or can't be used any more, so it can't be downloaded. Redo it with a note, or make it once more as it is: the new image uses the photos you can still use, or none if there are none."
      )
    ).toBeInTheDocument();
    expect(
      within(edit).getByText(
        "A product photo it used was rejected or can't be used any more. Make it again: the new image won't use that photo."
      )
    ).toBeInTheDocument();
  });
});

describe('one photo, one state on both screens (#229)', () => {
  it('a photo checked on the product profile is no longer asked before downloading', async () => {
    mockGateway.getMarketingIdeas.mockResolvedValue(ok(photoDetail()));
    render(
      <Fresh>
        <ProductProfile product={product()} />
        <IdeasPage product={product()} />
      </Fresh>
    );
    const photos = await screen.findByRole('region', {
      name: 'Product photos',
    });
    const main = within(photos).getAllByTestId('profile-photo')[0];
    expect(within(main).getByText('Not checked yet')).toBeInTheDocument();
    fireEvent.click(
      within(main).getByRole('button', { name: 'Check this photo' })
    );
    mockGateway.verifyMarketingIdeas.mockResolvedValue(ok(checked()));
    fireEvent.click(
      within(main).getByRole('button', { name: 'Yes, check it' })
    );
    await waitFor(() =>
      expect(within(main).getByText('Checked')).toBeInTheDocument()
    );

    fireEvent.click(
      screen.getByRole('button', { name: 'Download images only' })
    );
    await screen.findByRole('heading', {
      level: 1,
      name: 'Check once before downloading',
    });
    // The same answer: nothing left to check, both kept ads download now.
    expect(within(looks()).queryByTestId('check-photo')).toBeNull();
    expect(
      within(looks()).getByText(
        'No product photo or look to check. 2 used a checked product photo.'
      )
    ).toBeInTheDocument();
    expect(confirmButton()).toBeEnabled();
    expect(mockGateway.getMarketingIdeas).toHaveBeenCalledTimes(1);
  });
});
