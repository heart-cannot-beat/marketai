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
 * The user's own product photo (#233, design §5.2): on the product profile
 * the user says what the photo shows and picks the file. It is uploaded like
 * any reference, goes through the reference upload gate, and is added to the
 * product, listed unchecked with the other photos. A rejected image says why
 * in the gate's own words and nothing is added. Checking it afterwards is the
 * same declaration as for a photo the reading found.
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
function refused(code: string, params?: Record<string, unknown>) {
  return {
    kind: 'problem' as const,
    status: 409,
    problem: {
      type: 'about:blank',
      title: code,
      status: 409,
      detail: code,
      instance: '',
      code,
      trace_id: 't',
      retryable: false,
      field_errors: [],
      ...(params ? { params } : {}),
    },
    meta: {},
  };
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

/** The user's own photo, as the server lists it once added. */
const own = photo('ph_own', {
  source: 'added',
  source_url: null,
  intake_id: null,
  image_id: null,
});

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

const adding = () => screen.getByRole('group', { name: 'Add a product photo' });

function choose(name = 'acme-charge-home.png') {
  const file = new File([new Uint8Array([137, 80, 78, 71])], name, {
    type: 'image/png',
  });
  fireEvent.change(screen.getByTestId('profile-photo-add-file'), {
    target: { files: [file] },
  });
  return file;
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

describe('adding the user’s own product photo (#233)', () => {
  it('a product without photos: uploads one through the gate, adds it, and lists it unchecked', async () => {
    const { container } = await renderProfile();
    expect(adding()).toHaveTextContent(
      'Use your own photo of the product so the product in the images looks more like it.'
    );
    // No photo of the whole product yet, so that is what it is taken for.
    expect(
      within(adding()).getByRole('radio', { name: 'The whole product' })
    ).toBeChecked();
    uploadPasses();
    mockGateway.addMarketingIdeasPhoto.mockResolvedValue(
      created({ ...noPhotos(), photos: [own] })
    );
    choose();

    await waitFor(() =>
      expect(mockGateway.addMarketingIdeasPhoto).toHaveBeenCalled()
    );
    // Uploaded as a reference, for these ideas, then gated with no rights declared.
    expect(mockStartUpload).toHaveBeenCalledWith(
      'reference_asset',
      expect.objectContaining({
        ownerKind: 'marketing_project',
        ownerId: 'mp_secret',
      })
    );
    const gate = mockGateway.createLibraryReferenceObject.mock.calls[0][0];
    expect(gate.body).toEqual({
      upload_intent_id: 'ui_9',
      filename: 'acme-charge-home.png',
      declared_mime: 'image/png',
    });
    expect(mockGateway.addMarketingIdeasPhoto).toHaveBeenCalledWith(
      expect.objectContaining({
        projectId: 'mp_secret',
        body: { reference_object_id: 'refobj_9', role: 'main' },
      })
    );
    // Listed with the product's photos, unchecked, shown from its released copy.
    const listed = await screen.findByTestId('profile-photo');
    expect(
      within(listed).getByText('Photo 1 · Main photo')
    ).toBeInTheDocument();
    expect(within(listed).getByText('Added by you')).toBeInTheDocument();
    expect(within(listed).getByText('Not checked yet')).toBeInTheDocument();
    expect(
      await within(listed).findByRole('img', { name: 'Photo 1 · Main photo' })
    ).toHaveAttribute('src', 'blob:own-photo');
    expect(
      mockGateway.createMarketingIdeasPhotoDownloadIntent
    ).toHaveBeenCalledWith(
      expect.objectContaining({ projectId: 'mp_secret', photoId: 'ph_own' })
    );
    expect(adding()).toHaveTextContent(
      'Added, not checked yet. Images made with it can be downloaded once you check it.'
    );
    expect(container.textContent).not.toMatch(/refobj_|ui_9|mp_secret|ph_own/);
  });

  it('checks the added photo with the same declaration as any photo', async () => {
    await renderProfile({ ...noPhotos(), photos: [own] });
    mockGateway.verifyMarketingIdeas.mockResolvedValue(
      ok({
        ...noPhotos(),
        revision: 4,
        photos: [{ ...own, status: 'verified', reference_version_id: 'rv_9' }],
      })
    );
    const listed = screen.getByTestId('profile-photo');
    fireEvent.click(
      within(listed).getByRole('button', { name: 'Check this photo' })
    );
    expect(
      within(listed).getByRole('group', { name: 'Check this photo' })
    ).toHaveTextContent(
      'This is a real photo of Acme Charge Home, and I have the right to use it in ads.'
    );
    fireEvent.click(
      within(listed).getByRole('button', { name: 'Yes, check it' })
    );
    await within(listed).findByText('Checked');
    expect(mockGateway.verifyMarketingIdeas.mock.calls[0][0].body).toEqual({
      fact_ids: [],
      appearance_confirmed: [],
      accepted_findings: [],
      photo_ids: ['ph_own'],
      rejected_photo_ids: [],
    });
  });

  it('an image the gate rejects says why, and nothing is added', async () => {
    await renderProfile();
    uploadPasses();
    mockGateway.pollOperation.mockImplementation(
      async ({ operationId }: { operationId: string }) =>
        operationId === 'op_gate'
          ? operation('failed', 'refobj_bad')
          : operation('succeeded')
    );
    mockGateway.getLibraryReferenceObject.mockResolvedValue(
      ok({
        object_id: 'refobj_bad',
        state: 'rejected',
        failure_code: 'content_safety_unavailable',
      })
    );
    choose('not-a-photo.png');
    const alert = await within(adding()).findByRole('alert');
    expect(alert).toHaveTextContent(
      "This image didn't pass the upload checks:"
    );
    expect(mockGateway.getLibraryReferenceObject).toHaveBeenCalledWith(
      expect.objectContaining({ objectId: 'refobj_bad' })
    );
    expect(mockGateway.addMarketingIdeasPhoto).not.toHaveBeenCalled();
    expect(screen.queryByTestId('profile-photo')).toBeNull();
    expect(
      within(alert).getByRole('button', { name: 'Choose another' })
    ).toBeInTheDocument();
  });

  it('a photo the product already has is refused with its own words', async () => {
    await renderProfile({ ...noPhotos(), photos: [own] });
    uploadPasses();
    mockGateway.addMarketingIdeasPhoto.mockResolvedValue(
      refused('photo_already_listed', { photo_id: 'ph_own' })
    );
    choose();
    const alert = await within(adding()).findByRole('alert');
    expect(alert).toHaveTextContent(
      "The photo wasn't added: This photo is already one of the product's photos."
    );
  });

  it('the upload not finishing says so in plain words', async () => {
    await renderProfile();
    mockStartUpload.mockResolvedValue({
      kind: 'transfer_error',
      error: { kind: 'network', message: 'offline' },
    });
    choose();
    expect(await within(adding()).findByRole('alert')).toHaveTextContent(
      "The photo didn't finish uploading. Try again."
    );
    expect(mockGateway.createLibraryReferenceObject).not.toHaveBeenCalled();
  });

  it('with a photo of the whole product already, a new one is taken for a close-up', async () => {
    await renderProfile({ ...noPhotos(), photos: [photo('p1')] });
    expect(
      within(adding()).getByRole('radio', { name: 'A close-up of the product' })
    ).toBeChecked();
    uploadPasses();
    mockGateway.addMarketingIdeasPhoto.mockResolvedValue(
      created({
        ...noPhotos(),
        photos: [photo('p1'), { ...own, role: 'detail' }],
      })
    );
    fireEvent.click(
      within(adding()).getByRole('radio', {
        name: 'Another angle of the product',
      })
    );
    choose();
    await waitFor(() =>
      expect(mockGateway.addMarketingIdeasPhoto).toHaveBeenCalledWith(
        expect.objectContaining({
          body: { reference_object_id: 'refobj_9', role: 'angle' },
        })
      )
    );
  });
});
