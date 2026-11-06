/** @jest-environment jsdom */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import {
  ProductBrandKitSettings,
  brandKitBody,
  brandKitForm,
} from '@/components/products/ProductBrandKitSettings';

const mockRefresh = jest.fn(async () => undefined);
const mockGateway = { saveProductBrandKit: jest.fn() };
let mockRole = 'OWNER';
let mockKit: any;
const mockReferences = {
  items: [
    {
      item_id: 'item_logo',
      reference_version_id: 'ref_logo',
      role: 'logo',
      note: 'Primary wordmark',
      available: true,
    },
    {
      item_id: 'item_photo',
      reference_version_id: 'ref_photo',
      role: 'primary',
      note: 'Front photo',
      available: true,
    },
  ],
};
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: mockRole } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (operation: string) => ({
    data: operation === 'get_product_brand_kit' ? mockKit : mockReferences,
    refresh: mockRefresh,
  }),
}));
jest.mock('@/components/library/references/ReferenceThumbnail', () => ({
  ReferenceThumbnail: ({ alt }: { alt: string }) => <span>{alt}</span>,
}));

const product = {
  product_id: 'prod_1',
  entry_id: 'lib_product',
  name: 'Acme Charge adapter',
  ready: true,
  variant_label: null,
} as any;
const version = {
  id: 'bk_1',
  product_id: 'prod_1',
  version: 3,
  content_hash: 'a'.repeat(64),
  created_at: '2026-10-24T00:00:00Z',
  logo_available: true,
  content: {
    logo: {
      item_id: 'item_logo',
      placement: 'top_right',
      reference_version_id: 'ref_logo',
      reference_object_id: 'obj_logo',
      content_sha256: 'b'.repeat(64),
    },
    fonts: { primary: 'Inter', secondary: '' },
    palette: ['#0A3D62', '#F5F6FA'],
    readability_rules: {
      min_text_height_ratio: 0.04,
      min_contrast_ratio: 4.5,
      safe_area_ratio: 0.06,
    },
    forbidden: ['Unverified UL certification'],
  },
};

function open() {
  const view = render(<ProductBrandKitSettings product={product} />);
  const details = view.container.querySelector('details')!;
  details.open = true;
  fireEvent(details, new Event('toggle'));
  return view;
}

beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'OWNER';
  mockKit = { product_id: 'prod_1', revision: 3, current: version };
  mockGateway.saveProductBrandKit.mockResolvedValue({
    kind: 'ok',
    data: { product_id: 'prod_1', revision: 4, current: version },
  });
});

it('round-trips the stored version into an exact save body', () => {
  expect(brandKitBody(brandKitForm(version.content as any))).toEqual({
    logo: { item_id: 'item_logo', placement: 'top_right' },
    fonts: { primary: 'Inter', secondary: '' },
    palette: ['#0A3D62', '#F5F6FA'],
    readability_rules: {
      min_text_height_ratio: 0.04,
      min_contrast_ratio: 4.5,
      safe_area_ratio: 0.06,
    },
    forbidden: ['Unverified UL certification'],
  });
});

it('lets the Owner save a new version against the current revision, offering only logo items', async () => {
  open();
  expect(screen.getByText('Current version: 3')).toBeInTheDocument();
  const logo = screen.getByLabelText('Logo') as HTMLSelectElement;
  expect(Array.from(logo.options).map((option) => option.value)).toEqual([
    '',
    'item_logo',
  ]);
  fireEvent.click(screen.getByRole('button', { name: 'Add a color' }));
  fireEvent.change(
    screen.getByLabelText('Forbidden content or claims (one per line)'),
    { target: { value: 'Unverified UL certification\nCompetitor logos\n' } }
  );
  fireEvent.click(
    screen.getByRole('button', { name: 'Save as a new version' })
  );
  await waitFor(() =>
    expect(mockGateway.saveProductBrandKit).toHaveBeenCalledTimes(1)
  );
  const call = mockGateway.saveProductBrandKit.mock.calls[0][0];
  expect(call).toMatchObject({
    workspaceSlug: 'acme',
    productId: 'prod_1',
    ifMatch: 3,
  });
  expect(call.body.palette).toEqual(['#0A3D62', '#F5F6FA', '#FFFFFF']);
  expect(call.body.forbidden).toEqual([
    'Unverified UL certification',
    'Competitor logos',
  ]);
  expect(await screen.findByText('Saved version 4.')).toBeInTheDocument();
});

it('reloads after a concurrent change and never lets a member save', async () => {
  mockGateway.saveProductBrandKit.mockResolvedValue({
    kind: 'problem',
    problem: { code: 'stale_brand_kit_revision' },
  });
  open();
  fireEvent.click(
    screen.getByRole('button', { name: 'Save as a new version' })
  );
  expect(
    await screen.findByText(
      'The brand baseline changed elsewhere and has been reloaded. Review it and save again.'
    )
  ).toBeInTheDocument();
  expect(mockRefresh).toHaveBeenCalled();
});

it('shows the baseline read-only to members', () => {
  mockRole = 'MEMBER';
  open();
  expect(
    screen.getByText('Only the Workspace Owner can change the brand baseline.')
  ).toBeInTheDocument();
  expect(
    screen.getByRole('button', { name: 'Save as a new version' })
  ).toBeDisabled();
});

it('warns when the saved logo lost its gates', () => {
  mockKit = {
    product_id: 'prod_1',
    revision: 3,
    current: { ...version, logo_available: false },
  };
  open();
  expect(
    screen.getByText(
      /The saved logo no longer passes its file or rights checks/
    )
  ).toBeInTheDocument();
});
