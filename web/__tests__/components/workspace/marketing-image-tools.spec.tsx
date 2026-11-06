/** @jest-environment jsdom */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { SWRConfig } from 'swr';
import { MarketingImageTools } from '@/components/workspace/MarketingImageTools';
import messages from '../../../locales/en/common.json';

jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      ((messages as Record<string, string>)[key] || key).replace(
        /\{\{(\w+)\}\}/g,
        (_, k) => String(options?.[k] ?? '')
      ),
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws', slug: 'alpha' } }),
}));
const gateway = {
  createGenerationAssetVersionDownloadIntent: jest.fn(),
  getGenerationCapabilities: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
const blobClient = {
  downloadBlob: jest.fn().mockResolvedValue(new Blob(['source'])),
};
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => blobClient,
}));

beforeEach(() => {
  jest.clearAllMocks();
  URL.createObjectURL = jest.fn(() => 'blob:private-source');
  URL.revokeObjectURL = jest.fn();
  gateway.createGenerationAssetVersionDownloadIntent.mockResolvedValue({
    kind: 'created',
    data: { download_grant: { url: 'https://private-storage.test/signed' } },
  });
  gateway.getGenerationCapabilities.mockResolvedValue({
    kind: 'success',
    data: {
      provider_options: [
        {
          id: 'gemini',
          label: 'Gemini',
          default_model: 'gemini-image',
          available: true,
          execution_mode: 'byok',
          models: [
            {
              id: 'gemini-image',
              available: true,
              image_sizes: ['1K', '2K'],
              image_tools: ['upscale'],
            },
          ],
        },
        {
          id: 'openai',
          label: 'OpenAI',
          default_model: 'gpt-image-2',
          available: true,
          execution_mode: 'byok',
          models: [
            {
              id: 'gpt-image-2',
              available: true,
              image_sizes: ['1K', '2K'],
              image_tools: ['upscale', 'erase'],
            },
            {
              id: 'gpt-image-1.5',
              available: true,
              image_sizes: ['1K', '2K'],
              image_tools: ['upscale', 'remove_background', 'erase'],
            },
          ],
        },
      ],
    },
  });
});

async function setup(width = 80, height = 60) {
  const onPrepare = jest.fn();
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <MarketingImageTools
        assetVersionId="source-version"
        mode="byok"
        busy={false}
        onPrepare={onPrepare}
      />
    </SWRConfig>
  );
  const details = screen
    .getByText('Upscale, remove background or erase')
    .closest('details')!;
  details.open = true;
  fireEvent(details, new Event('toggle'));
  const image = await screen.findByAltText('Original image');
  Object.defineProperty(image, 'naturalWidth', { value: width });
  Object.defineProperty(image, 'naturalHeight', { value: height });
  fireEvent.load(image);
  await waitFor(() =>
    expect(screen.getByLabelText('Image provider')).toHaveValue('gemini')
  );
  return onPrepare;
}

test('prepares a quoted upscale draft with source dimensions and no generation call', async () => {
  const onPrepare = await setup();
  expect(screen.getByText('Output: 160 × 120 px')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Prepare edit draft' }));
  expect(onPrepare).toHaveBeenCalledWith({
    asset_version_id: 'source-version',
    note: '',
    provider: 'gemini',
    model: 'gemini-image',
    image_tool: {
      algorithm: 'supplier-image-tools-v1',
      kind: 'upscale',
      source_width: 80,
      source_height: 60,
      scale: 2,
    },
  });
  expect(
    gateway.createGenerationAssetVersionDownloadIntent
  ).toHaveBeenCalledWith(
    expect.objectContaining({
      versionId: 'source-version',
      workspaceSlug: 'alpha',
    })
  );
});

test('background removal offers the stable transparent model and private original preview', async () => {
  const onPrepare = await setup();
  fireEvent.change(screen.getByLabelText('Edit action'), {
    target: { value: 'remove_background' },
  });
  expect(screen.getByLabelText('Image provider')).toHaveValue('openai');
  expect(screen.getByLabelText('Model version')).toHaveValue('gpt-image-1.5');
  expect(
    within(screen.getByLabelText('Image provider')).queryByText('Gemini')
  ).toBeNull();
  expect(screen.getByAltText('Original image')).toHaveAttribute(
    'src',
    'blob:private-source'
  );
  fireEvent.click(screen.getByRole('button', { name: 'Prepare edit draft' }));
  expect(onPrepare).toHaveBeenCalledWith(
    expect.objectContaining({
      provider: 'openai',
      model: 'gpt-image-1.5',
      image_tool: {
        algorithm: 'supplier-image-tools-v1',
        kind: 'remove_background',
        source_width: 80,
        source_height: 60,
      },
    })
  );
});

test('erase freezes the displayed rectangle and blocks out-of-bounds selection', async () => {
  const onPrepare = await setup();
  fireEvent.change(screen.getByLabelText('Edit action'), {
    target: { value: 'erase' },
  });
  fireEvent.change(screen.getByLabelText('From left (%)'), {
    target: { value: '25' },
  });
  expect(screen.getByLabelText('Area to erase')).toHaveStyle({
    left: '25%',
    width: '25%',
  });
  fireEvent.click(screen.getByRole('button', { name: 'Prepare edit draft' }));
  expect(onPrepare).toHaveBeenCalledWith(
    expect.objectContaining({
      image_tool: {
        algorithm: 'supplier-image-tools-v1',
        kind: 'erase',
        source_width: 80,
        source_height: 60,
        region: { x: 0.25, y: 0, width: 0.25, height: 0.25 },
      },
    })
  );
  onPrepare.mockClear();
  fireEvent.change(screen.getByLabelText('From left (%)'), {
    target: { value: '90' },
  });
  expect(
    screen.getByRole('button', { name: 'Prepare edit draft' })
  ).toBeDisabled();
  expect(onPrepare).not.toHaveBeenCalled();
});

test('oversized enlargement is blocked before preparing a draft', async () => {
  const onPrepare = await setup(4000, 3000);
  expect(
    screen.getByRole('button', { name: 'Prepare edit draft' })
  ).toBeDisabled();
  expect(screen.getByRole('alert')).toHaveTextContent(
    'target image is too large'
  );
  expect(onPrepare).not.toHaveBeenCalled();
});

test('brush strokes are committed on pointer-up, undoable and frozen in the draft', async () => {
  window.PointerEvent = MouseEvent as typeof PointerEvent;
  const onPrepare = await setup();
  fireEvent.change(screen.getByLabelText('Edit action'), {
    target: { value: 'erase' },
  });
  fireEvent.change(screen.getByLabelText('Selection tool'), {
    target: { value: 'brush' },
  });
  const image = screen.getByAltText('Original image');
  Object.defineProperty(image, 'naturalWidth', { value: 80 });
  Object.defineProperty(image, 'naturalHeight', { value: 60 });
  fireEvent.load(image);
  const canvas = screen.getByRole('group', { name: 'Brush' });
  canvas.getBoundingClientRect = () => ({
    x: 10,
    y: 20,
    left: 10,
    top: 20,
    width: 400,
    height: 300,
    right: 410,
    bottom: 320,
    toJSON: () => ({}),
  });
  const prepare = screen.getByRole('button', { name: 'Prepare edit draft' });
  expect(prepare).toBeDisabled();
  fireEvent.pointerDown(canvas, { clientX: 110, clientY: 95, button: 0 });
  fireEvent.pointerMove(canvas, { clientX: 210, clientY: 170 });
  expect(prepare).toBeDisabled();
  expect(onPrepare).not.toHaveBeenCalled();
  fireEvent.pointerUp(canvas, { clientX: 210, clientY: 170 });
  expect(prepare).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: 'Undo stroke' }));
  expect(prepare).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Redo stroke' }));
  fireEvent.click(prepare);
  expect(onPrepare).toHaveBeenCalledWith(
    expect.objectContaining({
      asset_version_id: 'source-version',
      image_tool: {
        algorithm: 'supplier-image-tools-v1',
        kind: 'erase',
        source_width: 80,
        source_height: 60,
        strokes: [
          {
            width: 0.05,
            points: [
              { x: 0.25, y: 0.25 },
              { x: 0.5, y: 0.5 },
            ],
          },
        ],
      },
    })
  );
  onPrepare.mockClear();
  fireEvent.pointerDown(canvas, { clientX: 310, clientY: 245, button: 0 });
  fireEvent.pointerCancel(canvas);
  expect(screen.getByText('1 strokes selected')).toBeVisible();
  expect(onPrepare).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Clear strokes' }));
  expect(prepare).toBeDisabled();
});
