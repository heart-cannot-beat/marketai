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
import { ProductProfile } from '@/components/ideas/ProductProfile';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { ad, ideasDetail, photo, product } from './fixtures';

/**
 * The photos of the product profile (#199, #229): the photos the product's
 * reading found, each with its checking. A photo is checked with the
 * declaration written out, rejected after being told which ads need
 * redoing; a rejected one can be checked after all, one that can't be used
 * any more says why. Ideas no reading opened show the product's published
 * photos, read only.
 */
type Detail = components['schemas']['IdeasProjectDetail'];

jest.mock('next/router', () => ({
  useRouter: () => ({
    asPath: '/w/acme/p/prod_home/profile',
    query: { productId: 'prod_home' },
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'ws_1', role: 'OWNER' },
  }),
}));
const mockGateway = {
  getMarketingIdeas: jest.fn(),
  editMarketingIdeasUnderstanding: jest.fn(),
  verifyMarketingIdeas: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
const mockReads: Record<string, unknown> = {};
const mockRefreshRead = jest.fn().mockResolvedValue(undefined);
// The photo area's own upload (#233) needs the browser's storage client; nothing here uploads.
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => ({ downloadBlob: jest.fn() }),
  useFileDigest: () => jest.fn(),
}));
jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (
    operation: string,
    _params: unknown,
    options?: { enabled?: boolean }
  ) => ({
    data: options?.enabled === false ? undefined : mockReads[operation],
    problem: undefined,
    isLoading: false,
    isStale: false,
    lastSuccessfulAt: null,
    refresh: mockRefreshRead,
  }),
}));
jest.mock('@/hooks/library/useReferenceImageUrl', () => ({
  useReferenceImageUrl: (id: string) => ({
    blobUrl: `blob:photo-${id}`,
    isLoading: false,
    problem: null,
  }),
}));
jest.mock('@/hooks/products/useIntakeImageUrl', () => ({
  useIntakeImageUrl: (intakeId: string, imageId: string) => ({
    blobUrl: `blob:intake-${intakeId}-${imageId}`,
    isLoading: false,
    failed: false,
  }),
}));

function ok<T>(data: T) {
  return { kind: 'ok' as const, status: 200, data, meta: {} };
}
function refused(code: string, status = 409) {
  return {
    kind: 'problem' as const,
    status,
    problem: {
      type: 'about:blank',
      title: code,
      status,
      detail: code,
      instance: '',
      code,
      trace_id: 't',
      retryable: false,
      field_errors: [],
    },
    meta: {},
  };
}

/** Four photos: unchecked (two ads), checked (one ad), rejected, no longer usable. */
function photoDetail(): Detail {
  return ideasDetail({
    photos: [
      photo('p1', { asset_version_ids: ['av_1', 'av_2'] }),
      photo('p2', {
        role: 'angle',
        status: 'verified',
        reference_version_id: 'rv_2',
        asset_version_ids: ['av_3'],
      }),
      photo('p3', { role: 'detail', status: 'rejected' }),
      photo('p4', { role: 'detail', status: 'unavailable', source: 'upload' }),
    ],
    ads: [
      ad('av_1', 'c1', { kept: true, photo_ids: ['p1'] }),
      ad('av_2', 'c2', { photo_ids: ['p1'] }),
      ad('av_3', 'c3', { photo_ids: ['p2'] }),
    ],
    pending: [],
  });
}

/** The project after a photo decision: the server's answer. */
function decided(
  detail: Detail,
  photoId: string,
  status: 'verified' | 'rejected'
): Detail {
  return {
    ...detail,
    revision: detail.revision + 1,
    photos: detail.photos!.map((item) =>
      item.photo_id === photoId ? { ...item, status } : item
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

async function renderProfile(detail: Detail = photoDetail()) {
  mockGateway.getMarketingIdeas.mockResolvedValue(ok(detail));
  const view = render(
    <Fresh>
      <ProductProfile product={product()} />
    </Fresh>
  );
  await screen.findByRole('region', { name: 'Product photos' });
  return view;
}

const photos = () => screen.getByRole('region', { name: 'Product photos' });
const card = (name: string) =>
  within(photos())
    .getAllByTestId('profile-photo')
    .find((item) => within(item).queryByText(name))!;

beforeEach(() => {
  jest.clearAllMocks();
  for (const key of Object.keys(mockReads)) delete mockReads[key];
  mockReads.list_products = {
    items: [{ product_id: 'prod_home', entry_id: 'le_1', ready: false }],
  };
});

describe('product profile photos (#229)', () => {
  it('lists the photos the reading found, each with its checking and its ads', async () => {
    const { container } = await renderProfile();
    expect(photos()).toHaveTextContent(
      "Checking a photo means you confirm it's a real photo of this product and that you have the right to use it in ads."
    );
    expect(photos()).toHaveTextContent(
      "A check lasts one year. After that the photo can't be used any more and the ads made with it need redoing; a check can't be renewed yet."
    );
    expect(within(photos()).getAllByTestId('profile-photo')).toHaveLength(4);

    const first = card('Photo 1 · Main photo');
    expect(
      within(first).getByRole('img', { name: 'Photo 1 · Main photo' })
    ).toHaveAttribute('src', 'blob:intake-pin_secret-img_p1');
    expect(within(first).getByText('Not checked yet')).toBeInTheDocument();
    expect(
      within(first).getByText('From the product page')
    ).toBeInTheDocument();
    expect(within(first).getByText('Used in 2 ads')).toBeInTheDocument();
    expect(
      within(first).getByText(
        'Ads made with it stay drafts until you check it.'
      )
    ).toBeInTheDocument();
    expect(
      within(first).getByRole('button', { name: 'Check this photo' })
    ).toBeInTheDocument();

    const second = card('Photo 2 · Another angle');
    expect(within(second).getByText('Checked')).toBeInTheDocument();
    expect(within(second).getByText('Used in 1 ad')).toBeInTheDocument();
    expect(
      within(second).queryByRole('button', { name: 'Check this photo' })
    ).toBeNull();

    const third = card('Photo 3 · Close-up');
    expect(within(third).getByText('Rejected')).toBeInTheDocument();
    expect(within(third).getByText('No ad uses it yet.')).toBeInTheDocument();
    expect(
      within(third).getByRole('button', {
        name: 'It is this product: check it',
      })
    ).toBeInTheDocument();
    expect(
      within(third).queryByRole('button', {
        name: "Not this product, or can't be used",
      })
    ).toBeNull();

    const fourth = card('Photo 4 · Close-up');
    expect(
      within(fourth).getByText("Can't be used any more")
    ).toBeInTheDocument();
    expect(
      within(fourth).getByText('From the pictures you uploaded')
    ).toBeInTheDocument();
    expect(
      within(fourth).getByText(
        "It was checked, but can't be used any more: the check ran out, or the photo was withdrawn or archived in the library. Nothing new is made with it, and the ads made with it need redoing."
      )
    ).toBeInTheDocument();
    expect(within(fourth).queryByRole('button')).toBeNull();

    // The notes that checking and uploading are not open yet are gone (#233):
    // the user's own photo is added right here.
    expect(photos()).not.toHaveTextContent(/isn't available/i);
    expect(
      within(photos()).getByRole('group', { name: 'Add a product photo' })
    ).toBeInTheDocument();
    expect(container.textContent).not.toMatch(
      /mp_secret|av_\d|\bp\d\b|img_|pin_secret|rv_\d/
    );
  });

  it('checks a photo with the declaration written out, sending that photo alone', async () => {
    const detail = photoDetail();
    await renderProfile(detail);
    const first = card('Photo 1 · Main photo');
    fireEvent.click(
      within(first).getByRole('button', { name: 'Check this photo' })
    );
    const question = within(first).getByRole('group', {
      name: 'Check this photo',
    });
    expect(question).toHaveTextContent(
      'This is a real photo of Acme Charge Home, and I have the right to use it in ads.'
    );
    expect(question).toHaveTextContent(
      "2 ads made with it stop waiting for this photo. They don't need redoing."
    );
    expect(mockGateway.verifyMarketingIdeas).not.toHaveBeenCalled();

    mockGateway.verifyMarketingIdeas.mockResolvedValue(
      ok(decided(detail, 'p1', 'verified'))
    );
    fireEvent.click(
      within(question).getByRole('button', { name: 'Yes, check it' })
    );
    await waitFor(() =>
      expect(within(first).getByText('Checked')).toBeInTheDocument()
    );
    expect(mockGateway.verifyMarketingIdeas).toHaveBeenCalledTimes(1);
    expect(mockGateway.verifyMarketingIdeas).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        projectId: 'mp_secret',
        expectedRevision: 3,
        body: {
          fact_ids: [],
          appearance_confirmed: [],
          accepted_findings: [],
          photo_ids: ['p1'],
          rejected_photo_ids: [],
        },
      })
    );
    expect(within(first).queryByRole('group')).toBeNull();
    // A checked photo becomes part of the product's confirmed details.
    expect(mockRefreshRead).toHaveBeenCalled();
  });

  it('rejects a checked photo only after saying which ads need redoing', async () => {
    const detail = photoDetail();
    await renderProfile(detail);
    const second = card('Photo 2 · Another angle');
    fireEvent.click(
      within(second).getByRole('button', {
        name: "Not this product, or can't be used",
      })
    );
    const question = within(second).getByRole('group', {
      name: 'Reject this photo?',
    });
    expect(question).toHaveTextContent(
      "1 ad was made with it. Once you reject it, it needs redoing and can't be downloaded."
    );
    expect(mockGateway.verifyMarketingIdeas).not.toHaveBeenCalled();

    mockGateway.verifyMarketingIdeas.mockResolvedValue(
      ok(decided(detail, 'p2', 'rejected'))
    );
    fireEvent.click(
      within(question).getByRole('button', { name: 'Reject this photo' })
    );
    await waitFor(() =>
      expect(within(second).getByText('Rejected')).toBeInTheDocument()
    );
    expect(mockGateway.verifyMarketingIdeas).toHaveBeenCalledWith(
      expect.objectContaining({
        expectedRevision: 3,
        body: expect.objectContaining({
          photo_ids: [],
          rejected_photo_ids: ['p2'],
        }),
      })
    );
    expect(
      within(second).getByText(
        'You rejected it. Nothing new is made with it, and the ads made with it need redoing.'
      )
    ).toBeInTheDocument();
  });

  it('a rejected photo can be checked after all, with the same declaration', async () => {
    const detail = photoDetail();
    await renderProfile(detail);
    const third = card('Photo 3 · Close-up');
    fireEvent.click(
      within(third).getByRole('button', {
        name: 'It is this product: check it',
      })
    );
    const question = within(third).getByRole('group', {
      name: 'Check this photo',
    });
    expect(question).toHaveTextContent(
      'This is a real photo of Acme Charge Home, and I have the right to use it in ads.'
    );
    mockGateway.verifyMarketingIdeas.mockResolvedValue(
      ok(decided(detail, 'p3', 'verified'))
    );
    fireEvent.click(
      within(question).getByRole('button', { name: 'Yes, check it' })
    );
    await waitFor(() =>
      expect(within(third).getByText('Checked')).toBeInTheDocument()
    );
    expect(mockGateway.verifyMarketingIdeas.mock.calls[0][0].body).toEqual(
      expect.objectContaining({ photo_ids: ['p3'], rejected_photo_ids: [] })
    );
  });

  it('someone changed it first: says so and refreshes before trying again', async () => {
    await renderProfile();
    const first = card('Photo 1 · Main photo');
    fireEvent.click(
      within(first).getByRole('button', { name: 'Check this photo' })
    );
    mockGateway.verifyMarketingIdeas.mockResolvedValue(
      refused('stale_marketing_revision')
    );
    fireEvent.click(
      within(first).getByRole('button', { name: 'Yes, check it' })
    );
    const alert = await within(first).findByRole('alert');
    expect(alert).toHaveTextContent(
      'These ideas changed in the meantime, maybe because someone else checked something. Refresh to see them as they are now, then try again.'
    );
    // The question stays open, so trying again is one click once refreshed.
    expect(
      within(first).getByRole('group', { name: 'Check this photo' })
    ).toBeInTheDocument();
    expect(mockGateway.getMarketingIdeas).toHaveBeenCalledTimes(1);
    fireEvent.click(within(alert).getByRole('button', { name: 'Refresh' }));
    await waitFor(() =>
      expect(mockGateway.getMarketingIdeas).toHaveBeenCalledTimes(2)
    );
  });

  it('a photo no longer among the product’s photos: says to refresh', async () => {
    await renderProfile();
    const first = card('Photo 1 · Main photo');
    fireEvent.click(
      within(first).getByRole('button', { name: 'Check this photo' })
    );
    mockGateway.verifyMarketingIdeas.mockResolvedValue(
      refused('photo_not_found', 422)
    );
    fireEvent.click(
      within(first).getByRole('button', { name: 'Yes, check it' })
    );
    const alert = await within(first).findByRole('alert');
    expect(alert).toHaveTextContent(
      "This photo is no longer one of this product's photos. Refresh and look again."
    );
    expect(
      within(alert).getByRole('button', { name: 'Refresh' })
    ).toBeInTheDocument();
  });

  it('no answer: says so, and checking again repeats the same request', async () => {
    const detail = photoDetail();
    await renderProfile(detail);
    const first = card('Photo 1 · Main photo');
    fireEvent.click(
      within(first).getByRole('button', { name: 'Check this photo' })
    );
    mockGateway.verifyMarketingIdeas.mockRejectedValueOnce(new Error('lost'));
    fireEvent.click(
      within(first).getByRole('button', { name: 'Yes, check it' })
    );
    expect(await within(first).findByRole('alert')).toHaveTextContent(
      "No answer came back, so it's unclear whether this went through. Trying again repeats the same request; it won't happen twice."
    );
    mockGateway.verifyMarketingIdeas.mockResolvedValue(
      ok(decided(detail, 'p1', 'verified'))
    );
    fireEvent.click(
      within(first).getByRole('button', { name: 'Yes, check it' })
    );
    await waitFor(() =>
      expect(within(first).getByText('Checked')).toBeInTheDocument()
    );
    const [one, two] = mockGateway.verifyMarketingIdeas.mock.calls;
    expect(two[0].actionInstanceId).toBe(one[0].actionInstanceId);
  });

  it('ideas no reading opened: no photos of their own, and says what that means', async () => {
    await renderProfile(ideasDetail({ photos: [], pending: [] }));
    expect(photos()).toHaveTextContent(
      'No product photo was found when the product was read, so its ads were made without one. Before downloading, you confirm the product in each image looks right.'
    );
    expect(within(photos()).queryByRole('button')).toBeNull();
  });

  it('the brand says a checked photo is not a logo, and a logo can be uploaded there (#233)', async () => {
    await renderProfile();
    const brand = screen.getByRole('region', { name: 'Brand' });
    expect(brand).toHaveTextContent(
      "The brand baseline's logo is chosen from the checked logos above; product photos are never used as a logo."
    );
    expect(brand).not.toHaveTextContent(/can't be added here yet/);
    expect(
      within(brand).getByRole('group', { name: 'Upload a logo' })
    ).toBeInTheDocument();
  });
});

describe('product profile photos beside the audience (#224)', () => {
  it('checking the audience and then a photo: two separate checks, each at the revision it saw', async () => {
    const detail = photoDetail();
    await renderProfile(detail);
    const audience = screen.getByTestId('profile-audience');
    const afterAudience = {
      ...detail,
      revision: detail.revision + 1,
      audience_confirmed: true,
    };
    mockGateway.verifyMarketingIdeas.mockResolvedValueOnce(ok(afterAudience));
    fireEvent.click(
      within(audience).getByRole('button', { name: 'This is right' })
    );
    await waitFor(() =>
      expect(within(audience).getByText('Checked')).toBeInTheDocument()
    );
    // The photos are as they were.
    expect(
      within(card('Photo 1 · Main photo')).getByText('Not checked yet')
    ).toBeInTheDocument();

    mockGateway.verifyMarketingIdeas.mockResolvedValueOnce(
      ok(decided(afterAudience, 'p1', 'verified'))
    );
    const first = card('Photo 1 · Main photo');
    fireEvent.click(
      within(first).getByRole('button', { name: 'Check this photo' })
    );
    fireEvent.click(
      within(
        within(first).getByRole('group', { name: 'Check this photo' })
      ).getByRole('button', { name: 'Yes, check it' })
    );
    await waitFor(() =>
      expect(within(first).getByText('Checked')).toBeInTheDocument()
    );
    // The audience stays checked; neither check carries the other's decision.
    expect(within(audience).getByText('Checked')).toBeInTheDocument();
    const [audienceCall, photoCall] =
      mockGateway.verifyMarketingIdeas.mock.calls.map((call) => call[0]);
    expect(audienceCall).toEqual(
      expect.objectContaining({
        expectedRevision: 3,
        body: { audience_confirmed: true },
      })
    );
    expect(photoCall.expectedRevision).toBe(4);
    expect(photoCall.body).toEqual(
      expect.objectContaining({ photo_ids: ['p1'], rejected_photo_ids: [] })
    );
    expect(photoCall.body).not.toHaveProperty('audience_confirmed');
  });
});
