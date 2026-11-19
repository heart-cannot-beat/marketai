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
 * Renewing a checked photo whose rights ran out (#233). A checked photo says
 * until when its check runs; one whose check ran out says when, and renewing
 * it is the same declaration made again, after saying that the ads made with
 * it come back with no redoing. A photo withdrawn in the library says so and
 * offers nothing. A logo renews the same way.
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

function Fresh({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

async function renderProfile(detail: Detail) {
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

const UNTIL = '2027-11-18';
const RAN_OUT = '2026-11-17';
const day = (iso: string) =>
  new Date(`${iso}T00:00:00Z`).toLocaleDateString('en', { timeZone: 'UTC' });

function renewalDetail(): Detail {
  return ideasDetail({
    photos: [
      photo('p1', {
        status: 'verified',
        reference_version_id: 'rv_1',
        valid_to: UNTIL,
      }),
      photo('p2', {
        role: 'detail',
        status: 'unavailable',
        unavailable_reason: 'expired',
        valid_to: RAN_OUT,
        asset_version_ids: ['av_1', 'av_2'],
      }),
      photo('p3', {
        role: 'angle',
        status: 'unavailable',
        unavailable_reason: 'withdrawn',
        valid_to: UNTIL,
      }),
    ],
    logos: [
      {
        photo_id: 'lg_1',
        status: 'unavailable',
        unavailable_reason: 'expired',
        valid_to: RAN_OUT,
        width: 300,
        height: 120,
        reference_version_id: null,
      },
    ],
    ads: [
      ad('av_1', 'c1', { photo_ids: ['p2'] }),
      ad('av_2', 'c2', { photo_ids: ['p2'] }),
    ],
    pending: [],
  });
}

describe('renewing a photo check (#233)', () => {
  it('a checked photo says until when; one that ran out says when and offers renewing', async () => {
    await renderProfile(renewalDetail());
    const checked = card('Photo 1 · Main photo');
    expect(checked).toHaveTextContent(`Checked until ${day(UNTIL)}`);
    const expired = card('Photo 2 · Close-up');
    expect(expired).toHaveTextContent(
      `Its check ran out on ${day(RAN_OUT)}, so it can't be used now and the ads made with it need redoing.`
    );
    expect(
      within(expired).getByRole('button', { name: 'Renew this photo' })
    ).toBeInTheDocument();
    expect(photos()).not.toHaveTextContent(/can't be renewed yet/);
  });

  it('renews with the declaration made again, saying the ads come back with no redoing', async () => {
    const detail = renewalDetail();
    await renderProfile(detail);
    mockGateway.verifyMarketingIdeas.mockResolvedValue(
      ok({
        ...detail,
        revision: detail.revision + 1,
        photos: detail.photos!.map((item) =>
          item.photo_id === 'p2'
            ? {
                ...item,
                status: 'verified' as const,
                unavailable_reason: null,
                valid_to: UNTIL,
                reference_version_id: 'rv_2b',
              }
            : item
        ),
      })
    );
    const expired = card('Photo 2 · Close-up');
    fireEvent.click(
      within(expired).getByRole('button', { name: 'Renew this photo' })
    );
    const question = within(expired).getByRole('group', {
      name: 'Renew this photo',
    });
    expect(question).toHaveTextContent(
      `The last check ran out on ${day(RAN_OUT)}. Renewing is making the declaration below again; the new year starts today.`
    );
    expect(question).toHaveTextContent(
      'This is a real photo of Acme Charge Home, and I have the right to use it in ads.'
    );
    expect(question).toHaveTextContent(
      'The 2 ads made with it come back as they were, with no redoing.'
    );
    expect(mockGateway.verifyMarketingIdeas).not.toHaveBeenCalled();
    fireEvent.click(
      within(question).getByRole('button', { name: 'Yes, renew it' })
    );
    await waitFor(() =>
      expect(card('Photo 2 · Close-up')).toHaveTextContent(
        `Checked until ${day(UNTIL)}`
      )
    );
    expect(mockGateway.verifyMarketingIdeas.mock.calls[0][0].body).toEqual({
      fact_ids: [],
      appearance_confirmed: [],
      accepted_findings: [],
      photo_ids: ['p2'],
      rejected_photo_ids: [],
    });
  });

  it('a photo withdrawn in the library says so and offers nothing', async () => {
    await renderProfile(renewalDetail());
    const withdrawn = card('Photo 3 · Another angle');
    expect(withdrawn).toHaveTextContent(
      "It was withdrawn or archived in the library, so it can't be used or renewed; the ads made with it need redoing."
    );
    expect(within(withdrawn).queryByRole('button')).toBeNull();
  });

  it('a logo whose check ran out renews the same way', async () => {
    await renderProfile(renewalDetail());
    const brand = screen.getByRole('region', { name: 'Brand' });
    const logo = within(brand).getByTestId('profile-logo');
    expect(logo).toHaveTextContent(
      `Its check ran out on ${day(RAN_OUT)}, so the brand baseline can't use it; renew it to choose it again.`
    );
    fireEvent.click(
      within(logo).getByRole('button', { name: 'Renew this logo' })
    );
    expect(
      within(logo).getByRole('group', { name: 'Renew this logo' })
    ).toHaveTextContent(
      "This is this brand's logo, and I have the right to use it in ads."
    );
  });
});
