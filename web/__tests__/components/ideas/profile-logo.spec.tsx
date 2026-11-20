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
import { ad, ideasDetail, product } from './fixtures';

/**
 * The brand's logo on the product profile (#233, design §5.7): uploaded
 * through the same gate as a photo, listed with the brand and never among
 * the product photos. Checked -- this brand's logo, usable in ads -- it can
 * be chosen in the brand baseline below; rejecting it asks first and says
 * that a baseline already saved with it keeps it until changed there.
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
    current: { slug: 'acme', workspaceId: 'ws_1', role: 'MEMBER' },
  }),
}));
const mockGateway = {
  getMarketingIdeas: jest.fn(),
  verifyMarketingIdeas: jest.fn(),
  pollOperation: jest.fn(),
  createLibraryReferenceObject: jest.fn(),
  getLibraryReferenceObject: jest.fn(),
  addMarketingIdeasPhoto: jest.fn(),
  createMarketingIdeasPhotoDownloadIntent: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
const mockStartUpload = jest.fn();
jest.mock('@/hooks/domain/useUpload', () => ({
  useUpload: (purpose: string) => ({
    startUpload: (params: unknown) => mockStartUpload(purpose, params),
    state: 'idle',
    hashProgress: 0,
  }),
}));
const mockDownloadBlob = jest.fn();
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => ({ downloadBlob: mockDownloadBlob }),
  useFileDigest: () => jest.fn(),
}));
const mockReads: Record<string, unknown> = {};
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
    refresh: jest.fn().mockResolvedValue(undefined),
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
function created<T>(data: T) {
  return { kind: 'created' as const, status: 201, data, meta: {} };
}
function operation(state: string, resourceId: string | null = null) {
  return ok({
    operation_id: 'op',
    command_type: 'x',
    state,
    result: resourceId
      ? { resource_type: 'reference_object', resource_id: resourceId }
      : null,
    created_at: '',
    updated_at: '',
  });
}

/** Ads made without a product photo; the reading found none. */
function noPhotos(): Detail {
  return ideasDetail({
    photos: [],
    ads: [ad('av_1', 'c1', { used_product_photo: false })],
    pending: [],
  });
}

type Logo = components['schemas']['IdeasLogo'];
const logo = (extra: Partial<Logo> = {}): Logo => ({
  photo_id: 'lg_1',
  status: 'unverified',
  width: 300,
  height: 120,
  reference_version_id: null,
  ...extra,
});
/** A logo item of the product's published version, as the brand baseline reads it. */
const logoItem = {
  item_id: 'pritem_lg',
  reference_object_id: 'refobj_lg',
  reference_version_id: 'rv_lg',
  model_variant: '',
  role: 'logo',
  identity_status: 'unverified',
  note: '',
  added_by: 'user_1',
  added_at: '2026-11-18',
  available: true,
};

function Fresh({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

async function renderProfile(detail: Detail = noPhotos()) {
  mockGateway.getMarketingIdeas.mockResolvedValue(ok(detail));
  const view = render(
    <Fresh>
      <ProductProfile product={product()} />
    </Fresh>
  );
  await screen.findByRole('region', { name: 'Product photos' });
  return view;
}

/** The upload and the gate both pass; the gate released `refobj_9`. */
function uploadPasses() {
  mockStartUpload.mockResolvedValue({
    kind: 'accepted',
    uploadIntentId: 'ui_9',
    operationRecordId: 'op_upload',
  });
  mockGateway.createLibraryReferenceObject.mockResolvedValue({
    kind: 'accepted',
    status: 202,
    data: {
      operation_id: 'op_gate',
      state: 'queued',
      command_type: 'reference_gate',
    },
    meta: {},
  });
  mockGateway.pollOperation.mockImplementation(
    async ({ operationId }: { operationId: string }) =>
      operationId === 'op_gate'
        ? operation('succeeded', 'refobj_9')
        : operation('succeeded')
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  for (const key of Object.keys(mockReads)) delete mockReads[key];
  mockReads.list_products = {
    items: [{ product_id: 'prod_home', entry_id: 'le_1', ready: false }],
  };
  mockGateway.createMarketingIdeasPhotoDownloadIntent.mockResolvedValue(
    created({
      photo_id: 'ph_own',
      download_url: 'https://storage.example/ph_own',
      expires_at: '2026-11-18T12:00:00Z',
      mime_type: 'image/jpeg',
      byte_size: 10,
      width: 480,
      height: 480,
    })
  );
  mockDownloadBlob.mockResolvedValue(new Blob(['x']));
  Object.assign(URL, {
    createObjectURL: jest.fn(() => 'blob:own-photo'),
    revokeObjectURL: jest.fn(),
  });
});

const brand = () => screen.getByRole('region', { name: 'Brand' });
const uploading = () =>
  within(brand()).getByRole('group', { name: 'Upload a logo' });

describe('the brand’s logo (#233)', () => {
  it('uploads a logo through the gate and lists it with the brand, never as a photo', async () => {
    const { container } = await renderProfile();
    // No choice of what it shows: it is the logo.
    expect(within(uploading()).queryByRole('radio')).toBeNull();
    uploadPasses();
    mockGateway.addMarketingIdeasPhoto.mockResolvedValue(
      created({ ...noPhotos(), logos: [logo()] })
    );
    fireEvent.change(screen.getByTestId('profile-logo-add-file'), {
      target: {
        files: [
          new File([new Uint8Array([137, 80])], 'acme-logo.png', {
            type: 'image/png',
          }),
        ],
      },
    });
    await waitFor(() =>
      expect(mockGateway.addMarketingIdeasPhoto).toHaveBeenCalledWith(
        expect.objectContaining({
          body: { reference_object_id: 'refobj_9', role: 'logo' },
        })
      )
    );
    const listed = await within(brand()).findByTestId('profile-logo');
    expect(within(listed).getByText('Logo 1')).toBeInTheDocument();
    expect(within(listed).getByText('Not checked yet')).toBeInTheDocument();
    expect(
      await within(listed).findByRole('img', { name: 'Logo 1' })
    ).toHaveAttribute('src', 'blob:own-photo');
    // The photo area still has no photo.
    expect(screen.queryByTestId('profile-photo')).toBeNull();
    expect(uploading()).toHaveTextContent(
      'Added, not checked yet. Once you check it, you can choose it in the brand baseline.'
    );
    expect(container.textContent).not.toMatch(/refobj_|lg_1|mp_secret/);
  });

  it('checks the logo with its declaration, and the brand baseline can then choose it', async () => {
    mockReads.get_product_brand_kit = {
      product_id: 'prod_home',
      revision: 0,
      current: null,
    };
    await renderProfile({ ...noPhotos(), logos: [logo()] });
    mockGateway.verifyMarketingIdeas.mockResolvedValue(
      ok({
        ...noPhotos(),
        revision: 4,
        logos: [logo({ status: 'verified', reference_version_id: 'rv_lg' })],
      })
    );
    const listed = within(brand()).getByTestId('profile-logo');
    fireEvent.click(
      within(listed).getByRole('button', { name: 'Check this logo' })
    );
    expect(
      within(listed).getByRole('group', { name: 'Check this logo' })
    ).toHaveTextContent(
      'This is this brand’s logo, and I have the right to use it in ads.'.replace(
        '’',
        "'"
      )
    );
    // The product's published version has it as a logo once checked: the baseline offers it.
    mockReads.list_products = {
      items: [{ product_id: 'prod_home', entry_id: 'le_1', ready: true }],
    };
    mockReads.get_product_references = { items: [logoItem] };
    fireEvent.click(
      within(listed).getByRole('button', { name: 'Yes, check it' })
    );
    await within(listed).findByText('Checked');
    expect(mockGateway.verifyMarketingIdeas.mock.calls[0][0].body).toEqual({
      fact_ids: [],
      appearance_confirmed: [],
      accepted_findings: [],
      photo_ids: ['lg_1'],
      rejected_photo_ids: [],
    });
    const baseline = within(brand()).getByText('Brand baseline');
    fireEvent.click(baseline);
    const details = baseline.closest('details')!;
    details.open = true;
    fireEvent(details, new Event('toggle'));
    const choice = await within(brand()).findByRole('combobox', {
      name: 'Logo',
    });
    expect(
      within(choice).getByRole('option', { name: 'Logo image 1' })
    ).toBeInTheDocument();
  });

  it('a rejected logo the saved brand baseline still uses: new images keep it (#204)', async () => {
    mockReads.get_product_brand_kit = {
      product_id: 'prod_home',
      revision: 2,
      current: {
        id: 'bk_1',
        product_id: 'prod_home',
        version: 2,
        content_hash: 'h',
        content: {
          logo: {
            item_id: 'pritem_lg',
            placement: 'top_left',
            reference_version_id: 'rv_lg',
            reference_object_id: 'refobj_lg',
            content_sha256: 'a'.repeat(64),
          },
        },
        created_at: '2026-11-18T08:00:00+00:00',
        logo_available: true,
      },
    };
    // As the server answers: a rejected logo no longer names its library version.
    await renderProfile({
      ...noPhotos(),
      logos: [logo({ status: 'rejected', reference_version_id: null })],
    });
    expect(
      within(brand()).getByTestId('profile-logo-still-in-kit')
    ).toHaveTextContent(
      'The saved brand baseline still uses a logo you rejected or that can no longer be used: new images keep carrying it until the Owner changes the brand baseline below and saves it.'
    );
  });

  it('says nothing of a rejected logo when the brand baseline uses a checked one (#204)', async () => {
    mockReads.get_product_brand_kit = {
      product_id: 'prod_home',
      revision: 2,
      current: {
        id: 'bk_1',
        product_id: 'prod_home',
        version: 2,
        content_hash: 'h',
        content: {
          logo: {
            item_id: 'pritem_ok',
            placement: 'top_left',
            reference_version_id: 'rv_ok',
            reference_object_id: 'refobj_ok',
            content_sha256: 'b'.repeat(64),
          },
        },
        created_at: '2026-11-18T08:00:00+00:00',
        logo_available: true,
      },
    };
    await renderProfile({
      ...noPhotos(),
      logos: [
        logo({
          photo_id: 'lg_1',
          status: 'rejected',
          reference_version_id: null,
        }),
        logo({
          photo_id: 'lg_2',
          status: 'verified',
          reference_version_id: 'rv_ok',
        }),
      ],
    });
    expect(
      within(brand()).queryByTestId('profile-logo-still-in-kit')
    ).toBeNull();
  });

  it('says nothing more of a rejected logo the brand baseline does not use (#204)', async () => {
    mockReads.get_product_brand_kit = {
      product_id: 'prod_home',
      revision: 1,
      current: null,
    };
    await renderProfile({
      ...noPhotos(),
      logos: [logo({ status: 'rejected', reference_version_id: 'rv_lg' })],
    });
    expect(
      within(brand()).queryByTestId('profile-logo-still-in-kit')
    ).toBeNull();
  });

  it('rejecting a logo asks first, saying a saved baseline keeps it until changed', async () => {
    await renderProfile({
      ...noPhotos(),
      logos: [logo({ status: 'verified', reference_version_id: 'rv_lg' })],
    });
    mockGateway.verifyMarketingIdeas.mockResolvedValue(
      ok({ ...noPhotos(), revision: 4, logos: [logo({ status: 'rejected' })] })
    );
    const listed = within(brand()).getByTestId('profile-logo');
    fireEvent.click(
      within(listed).getByRole('button', {
        name: "Not this brand's, or can't be used",
      })
    );
    expect(mockGateway.verifyMarketingIdeas).not.toHaveBeenCalled();
    expect(
      within(listed).getByRole('group', {
        name: "Not this brand's, or can't be used",
      })
    ).toHaveTextContent(
      "Once rejected, the brand baseline can't choose it. If a saved brand baseline already uses it, change it there."
    );
    fireEvent.click(
      within(listed).getByRole('button', { name: 'Yes, reject it' })
    );
    await within(listed).findByText('Rejected');
    expect(
      mockGateway.verifyMarketingIdeas.mock.calls[0][0].body.rejected_photo_ids
    ).toEqual(['lg_1']);
  });
});
