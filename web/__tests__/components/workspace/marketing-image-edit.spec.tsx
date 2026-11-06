/** @jest-environment jsdom */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import { MarketingImageEdit } from '@/components/workspace/MarketingImageEdit';
import messages from '../../../locales/en/common.json';

jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, o?: Record<string, unknown>) =>
      ((messages as Record<string, string>)[key] || key).replace(
        /\{\{(\w+)\}\}/g,
        (_, k) => String(o?.[k] ?? '')
      ),
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws', slug: 'alpha' } }),
}));
const gateway = { createGenerationAssetVersionDownloadIntent: jest.fn() };
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
const blobClient = {
  downloadBlob: jest.fn().mockResolvedValue(new Blob(['image'])),
};
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => blobClient,
}));
beforeEach(() => {
  jest.clearAllMocks();
  URL.createObjectURL = jest.fn(() => 'blob:test-original');
  URL.revokeObjectURL = jest.fn();
  gateway.createGenerationAssetVersionDownloadIntent.mockResolvedValue({
    kind: 'created',
    data: { download_grant: { url: 'https://storage.test/signed.png' } },
  });
});

test('crop selection and clockwise rotation submit the exact source, without approving it', async () => {
  const onCreate = jest.fn();
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <MarketingImageEdit
        assetVersionId="source-v1"
        busy={false}
        edits={[]}
        onCreate={onCreate}
      />
    </SWRConfig>
  );
  const details = screen.getByText('Crop and rotate').closest('details')!;
  details.open = true;
  fireEvent(details, new Event('toggle'));
  await waitFor(() =>
    expect(
      gateway.createGenerationAssetVersionDownloadIntent
    ).toHaveBeenCalled()
  );
  expect(await screen.findByAltText('Original image')).toHaveAttribute(
    'src',
    'blob:test-original'
  );
  fireEvent.click(screen.getByLabelText('Crop image'));
  fireEvent.change(screen.getByLabelText('Width (%)'), {
    target: { value: '50' },
  });
  fireEvent.change(screen.getByLabelText('Clockwise rotation'), {
    target: { value: '90' },
  });
  fireEvent.click(
    screen.getByRole('button', { name: 'Create edited version' })
  );
  expect(onCreate).toHaveBeenCalledWith({
    asset_version_id: 'source-v1',
    rotation: 90,
    crop: { x: 0, y: 0, width: 0.5, height: 1 },
  });
});

test('out-of-bounds crop is blocked before creating an edit', () => {
  const onCreate = jest.fn();
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <MarketingImageEdit
        assetVersionId="source-v1"
        busy={false}
        edits={[]}
        onCreate={onCreate}
      />
    </SWRConfig>
  );
  const details = screen.getByText('Crop and rotate').closest('details')!;
  details.open = true;
  fireEvent(details, new Event('toggle'));
  fireEvent.click(screen.getByLabelText('Crop image'));
  fireEvent.change(screen.getByLabelText('From left (%)'), {
    target: { value: '90' },
  });
  expect(
    screen.getByRole('button', { name: 'Create edited version' })
  ).toBeDisabled();
  expect(onCreate).not.toHaveBeenCalled();
});
