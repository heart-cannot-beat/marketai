/** @jest-environment jsdom */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { ProductReferenceSettings } from '@/components/products/ProductReferenceSettings';

const mockRefresh = jest.fn(async () => undefined);
const mockChanged = jest.fn();
const mockGateway = {
  addProductReference: jest.fn(),
  updateProductReference: jest.fn(),
  deleteProductReference: jest.fn(),
};
let mockData: any;
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme' } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: () => ({ data: mockData, refresh: mockRefresh }),
}));
jest.mock('@/hooks/library/usePublishedReferenceAssets', () => ({
  usePublishedReferenceAssets: () => ({
    items: [{ versionId: 'ref_new', displayName: 'Studio photograph' }],
    refresh: mockRefresh,
  }),
}));
jest.mock('@/components/library/references/ReferenceThumbnail', () => ({
  ReferenceThumbnail: ({ alt }: { alt: string }) => <span>{alt}</span>,
}));
jest.mock('@/components/library/references/ReferenceUploadZone', () => ({
  ReferenceUploadZone: ({ onComplete, contextEntryId }: any) => (
    <button onClick={() => onComplete('object_uploaded')}>
      {contextEntryId}
    </button>
  ),
}));

const product = {
  product_id: 'prod_1',
  entry_id: 'lib_product',
  name: 'Charger',
  ready: true,
  variant_label: null,
};
const item = {
  item_id: 'item_1',
  reference_object_id: 'object_1',
  reference_version_id: 'ref_1',
  model_variant: 'A',
  role: 'primary',
  identity_status: 'unverified',
  note: 'Manufacturer image',
  available: true,
};

beforeEach(() => {
  jest.clearAllMocks();
  mockData = {
    revision: 7,
    version_id: 'v7',
    model_variants: ['A', 'B'],
    items: [item],
    has_unpublished_changes: false,
  };
  for (const method of Object.values(mockGateway))
    method.mockResolvedValue({ kind: 'ok', data: mockData });
});

function open() {
  const view = render(
    <ProductReferenceSettings product={product} onChanged={mockChanged} />
  );
  const details = view.container.querySelector('details')!;
  details.open = true;
  fireEvent(details, new Event('toggle'));
  return view;
}

it('keeps model groups separate and saves explicit verification with the current revision', async () => {
  open();
  expect(screen.getByRole('region', { name: 'B' })).toHaveTextContent(
    'No reference images'
  );
  const modelA = screen.getByRole('region', { name: 'A' });
  fireEvent.change(within(modelA).getByLabelText('Physical product identity'), {
    target: { value: 'verified_physical' },
  });
  await waitFor(() =>
    expect(mockGateway.updateProductReference).toHaveBeenCalledWith(
      expect.objectContaining({
        productId: 'prod_1',
        itemId: 'item_1',
        ifMatch: 7,
        body: { identity_status: 'verified_physical' },
      })
    )
  );
  await waitFor(() => expect(mockChanged).toHaveBeenCalled());
});

it('reuses a published version in the chosen model, and uploads bind the same promoted object', async () => {
  open();
  fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'B' } });
  fireEvent.change(screen.getByLabelText('Reuse an existing Library image'), {
    target: { value: 'ref_new' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Add selected image' }));
  await waitFor(() =>
    expect(mockGateway.addProductReference).toHaveBeenCalledWith(
      expect.objectContaining({
        ifMatch: 7,
        body: expect.objectContaining({
          reference_version_id: 'ref_new',
          model_variant: 'B',
          identity_status: 'unverified',
        }),
      })
    )
  );
  await waitFor(() =>
    expect(
      screen.getByRole('button', { name: 'Upload, drop or paste an image' })
    ).toBeEnabled()
  );
  fireEvent.click(
    screen.getByRole('button', { name: 'Upload, drop or paste an image' })
  );
  fireEvent.click(screen.getByRole('button', { name: 'lib_product' }));
  await waitFor(() =>
    expect(mockGateway.addProductReference).toHaveBeenLastCalledWith(
      expect.objectContaining({
        body: expect.objectContaining({
          reference_object_id: 'object_uploaded',
          model_variant: 'B',
        }),
      })
    )
  );
});

it('offers the Library draft resolution path without overwriting unpublished product changes', () => {
  mockData.has_unpublished_changes = true;
  open();
  expect(screen.getByRole('status')).toHaveTextContent(
    'unpublished Library changes'
  );
  expect(
    screen.getByRole('link', {
      name: 'Open Creative Library and version history',
    })
  ).toHaveAttribute('href', '/w/acme/library?cat=product');
  expect(screen.getByLabelText('Physical product identity')).toBeDisabled();
  expect(mockGateway.updateProductReference).not.toHaveBeenCalled();
});

it('requires a named model for physical photos while allowing product-wide logos', () => {
  open();
  expect(
    screen.getByText(/Choose the exact model before adding/)
  ).toBeInTheDocument();
  expect(
    screen.queryByRole('button', { name: 'Upload, drop or paste an image' })
  ).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Use of the new image'), {
    target: { value: 'logo' },
  });
  expect(
    screen.getByRole('button', { name: 'Upload, drop or paste an image' })
  ).toBeEnabled();
});

it('shows a readable stable-code failure and retries with the refreshed revision', async () => {
  mockGateway.deleteProductReference.mockResolvedValue({
    kind: 'problem',
    problem: { code: 'revision_conflict' },
  });
  open();
  fireEvent.click(
    screen.getByRole('button', { name: 'Remove from this product' })
  );
  await waitFor(() =>
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Reference settings changed'
    )
  );
  expect(mockRefresh).toHaveBeenCalled();
});

it('asks for a published product card when the product card is not active', async () => {
  mockGateway.deleteProductReference.mockResolvedValue({
    kind: 'problem',
    problem: { code: 'product_reference_profile_required' },
  });
  open();
  fireEvent.click(
    screen.getByRole('button', { name: 'Remove from this product' })
  );
  await waitFor(() =>
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Publish an active product card in Creative Library before changing reference images.'
    )
  );
});

it('falls back to the default sentence for a conflict without its own copy', async () => {
  mockGateway.deleteProductReference.mockResolvedValue({
    kind: 'problem',
    problem: { code: 'library_conflict' },
  });
  open();
  fireEvent.click(
    screen.getByRole('button', { name: 'Remove from this product' })
  );
  await waitFor(() =>
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Could not save the reference image. Refresh and try again.'
    )
  );
  expect(screen.getByRole('alert')).not.toHaveTextContent('library_conflict');
});
