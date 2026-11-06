/** @jest-environment jsdom */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { SWRConfig } from 'swr';
import { MarketingVersionHistory } from '@/components/workspace/MarketingVersionHistory';
import { useMarketingImageSource } from '@/components/workspace/useMarketingImageSource';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import messages from '../../../locales/en/common.json';

jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      ((messages as Record<string, string>)[key] || key).replace(
        /\{\{(\w+)\}\}/g,
        (_, k) => String(options?.[k] ?? '')
      ),
    i18n: { language: 'en' },
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws', slug: 'alpha' } }),
}));
const gateway = { createGenerationAssetVersionDownloadIntent: jest.fn() };
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
const blobs = { downloadBlob: jest.fn() };
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => blobs,
}));
type Asset = components['schemas']['WorkflowAsset'];
const source = {
  asset_version_id: 'original',
  asset_id: 'asset',
  parent_version_id: null,
  created_at: '2026-10-20T01:00:00Z',
  edit_operation: 'generated',
  ready: true,
  quality: { quality: 'approved' },
} as Asset;
const child = {
  ...source,
  asset_version_id: 'child',
  parent_version_id: 'original',
  created_at: '2026-10-21T01:00:00Z',
  edit_operation: 'erase',
  ready: false,
  quality: null,
} as Asset;
beforeEach(() => {
  jest.clearAllMocks();
  URL.createObjectURL = jest
    .fn()
    .mockReturnValueOnce('blob:first')
    .mockReturnValue('blob:next');
  URL.revokeObjectURL = jest.fn();
  gateway.createGenerationAssetVersionDownloadIntent.mockImplementation(
    async ({ versionId }) => ({
      kind: 'created',
      data: { download_grant: { url: 'https://private.test/' + versionId } },
    })
  );
  blobs.downloadBlob.mockResolvedValue(new Blob(['fixture']));
});
function show(editable = true, assets = [child, source]) {
  const onEdit = jest.fn();
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <MarketingVersionHistory
        assets={assets}
        busy={false}
        editable={editable}
        onEdit={onEdit}
      />
    </SWRConfig>
  );
  const details = screen
    .getByText('Version history and comparison')
    .closest('details')!;
  details.open = true;
  fireEvent(details, new Event('toggle'));
  return onEdit;
}
test('history follows parent links and exact review state, and can edit an older source', async () => {
  const onEdit = show();
  const rows = within(screen.getByRole('list')).getAllByRole('listitem');
  expect(rows[0]).toHaveTextContent('Version 1 · Original generation');
  expect(rows[0]).toHaveTextContent('Approved for use');
  expect(rows[1]).toHaveTextContent('Version 2 · Local erase');
  expect(rows[1]).toHaveTextContent('Source: Version 1');
  expect(rows[1]).toHaveTextContent('Awaiting human review');
  fireEvent.click(
    within(rows[0]).getByRole('button', {
      name: 'Continue editing this version',
    })
  );
  expect(onEdit).toHaveBeenCalledWith('original');
  const result = await screen.findByAltText('Comparison result');
  expect(result.getAttribute('src')).toMatch(/^blob:/);
  expect(screen.getByLabelText('Source version (left)')).toHaveValue(
    'original'
  );
  expect(screen.getByLabelText('Result version (right)')).toHaveValue('child');
  fireEvent.change(screen.getByRole('slider', { name: 'Show source: 50%' }), {
    target: { value: '25' },
  });
  expect(screen.getByAltText('Comparison source').parentElement).toHaveStyle({
    clipPath: 'inset(0 75% 0 0)',
  });
  fireEvent.change(
    screen.getByRole('slider', { name: 'Comparison zoom: 100%' }),
    { target: { value: '4' } }
  );
  expect(result.parentElement).toHaveStyle({ width: '400%' });
});
test('frozen history allows inspection but offers no edit action', async () => {
  show(false);
  await screen.findByAltText('Comparison result');
  expect(
    screen.queryByRole('button', { name: 'Continue editing this version' })
  ).toBeNull();
});

test('default comparison uses the newest result and never substitutes an unrelated missing parent', async () => {
  const unrelated = {
    ...source,
    asset_version_id: 'other-image',
    asset_id: 'other-asset',
    created_at: '2026-10-20T12:00:00Z',
  };
  show(true, [
    unrelated,
    source,
    { ...child, parent_version_id: 'outside-round' },
  ]);
  await screen.findByAltText('Comparison result');
  expect(screen.getByLabelText('Result version (right)')).toHaveValue('child');
  expect(screen.getByLabelText('Source version (left)')).toHaveValue('child');
  expect(
    screen.getByText('Source: an earlier version outside this round')
  ).toBeVisible();
  // Versions are told apart by number and picture; ids stay in technical details.
  expect(screen.queryByText('Source: outside-round')).not.toBeInTheDocument();
  expect(
    gateway.createGenerationAssetVersionDownloadIntent
  ).not.toHaveBeenCalledWith(
    expect.objectContaining({ versionId: 'outside-round' })
  );
});
test('failed private download offers a retry and never exposes the grant URL', async () => {
  blobs.downloadBlob.mockRejectedValue(new Error('private failure'));
  show();
  expect(await screen.findByText('Reload original image')).toBeVisible();
  expect(screen.queryByAltText('Comparison result')).toBeNull();
  expect(document.body.innerHTML).not.toContain('https://private.test/');
});
test('changing source identity and closing revoke blobs without showing the previous source', async () => {
  function Source({ id, open }: { id: string; open: boolean }) {
    const { imageUrl } = useMarketingImageSource(id, open);
    return <output>{imageUrl || 'loading'}</output>;
  }
  const cache = new Map();
  const tree = (id: string, open = true) => (
    <SWRConfig value={{ provider: () => cache }}>
      <Source id={id} open={open} />
    </SWRConfig>
  );
  const view = render(tree('first'));
  await screen.findByText('blob:first');
  blobs.downloadBlob.mockImplementation(() => new Promise(() => undefined));
  view.rerender(tree('second'));
  expect(screen.queryByText('blob:first')).toBeNull();
  await waitFor(() =>
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:first')
  );
  view.rerender(tree('second', false));
  expect(screen.getByText('loading')).toBeInTheDocument();
});
