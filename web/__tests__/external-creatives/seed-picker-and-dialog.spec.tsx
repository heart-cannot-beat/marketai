/**
 * @jest-environment jsdom
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import {
  SeedAssetPicker,
  mergeSeeds,
} from '@/components/test-plans/SeedAssetPicker';
import { ExternalCreativeDialog } from '@/components/external-creatives/ExternalCreativeDialog';
import { creative } from './fixtures';

const mockGateway = {
  listGenerationAssets: jest.fn(),
  listExternalCreatives: jest.fn(),
  createExternalCreatives: jest.fn(),
  getExternalCreative: jest.fn(),
  confirmExternalCreative: jest.fn(),
  pollOperation: jest.fn(),
};
const mockStartUpload = jest.fn();

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'MEMBER' } }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/hooks/domain/useUpload', () => ({
  useUpload: (purpose: string) => ({
    startUpload: (params: unknown) => mockStartUpload(purpose, params),
    state: 'idle',
    hashProgress: 0,
  }),
}));
jest.mock('@/hooks/products/useProductDirectory', () => ({
  useProductDirectory: () => ({
    products: [{ product_id: 'prod_1', name: 'VoltNest 壁挂充电桩' }],
    isLoading: false,
    problem: undefined,
  }),
}));
jest.mock('@/hooks/assets/useAssetImageUrl', () => ({
  useAssetImageUrl: (id: string | undefined) => ({
    blobUrl: id ? `blob:${id}` : null,
    isLoading: false,
    problem: undefined,
    source: 'thumbnail',
  }),
}));

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  Object.values(mockGateway).forEach((fn) => fn.mockReset());
  mockStartUpload.mockReset();
});

describe('SeedAssetPicker with external creatives', () => {
  it('lists confirmed external creatives next to Approved assets, marked External', async () => {
    mockGateway.listGenerationAssets.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: {
        items: [
          { id: 'av_gen', origin_kind: 'generation' },
          { id: 'av_ext_1', origin_kind: 'upload' },
        ],
      },
      meta: {},
    });
    mockGateway.listExternalCreatives.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: {
        items: [
          creative({ status: 'confirmed' }),
          creative({
            external_creative_id: 'exc_2',
            asset_version_id: 'av_ext_2',
            status: 'confirmed',
          }),
        ],
      },
      meta: {},
    });
    const onChange = jest.fn();
    render(
      <SeedAssetPicker productId="prod_1" value={[]} onChange={onChange} />
    );
    await flush();
    expect(mockGateway.listExternalCreatives).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      productId: 'prod_1',
      status: 'confirmed',
    });
    expect(screen.getAllByRole('checkbox')).toHaveLength(3);
    expect(screen.getAllByText('External')).toHaveLength(2);
    fireEvent.click(screen.getByLabelText('Use this asset 3'));
    expect(onChange).toHaveBeenCalledWith(['av_ext_2']);
  });

  it('still lists Approved assets when the external list fails', async () => {
    mockGateway.listGenerationAssets.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: { items: [{ id: 'av_gen' }] },
      meta: {},
    });
    mockGateway.listExternalCreatives.mockRejectedValue(new Error('offline'));
    render(
      <SeedAssetPicker productId="prod_1" value={[]} onChange={jest.fn()} />
    );
    await flush();
    expect(screen.getAllByRole('checkbox')).toHaveLength(1);
  });

  it('merges without duplicates', () => {
    expect(
      mergeSeeds(
        [{ id: 'a' }],
        [{ asset_version_id: 'a' }, { asset_version_id: 'b' }, {}]
      ).map((item) => item.id)
    ).toEqual(['a', 'b']);
  });
});

describe('ExternalCreativeDialog', () => {
  it('uploads with the rights declaration, then reviews and confirms the tags', async () => {
    mockStartUpload.mockResolvedValue({
      kind: 'accepted',
      uploadIntentId: 'ui_1',
      operationRecordId: 'op_up_1',
    });
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: { state: 'succeeded' },
      meta: {},
    });
    mockGateway.createExternalCreatives.mockResolvedValue({
      kind: 'accepted',
      status: 202,
      data: { items: [creative()] },
      meta: {},
    });
    mockGateway.confirmExternalCreative.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: creative({
        status: 'confirmed',
        confirmation: {
          revision: 1,
          tags: [],
          attributable: false,
          experiment_ready: true,
          concept_count: 1,
        },
      }),
      meta: {},
    });
    const onChanged = jest.fn();
    render(
      <ExternalCreativeDialog
        open
        onClose={jest.fn()}
        onChanged={onChanged}
        defaultProductId="prod_1"
      />
    );
    const submit = screen.getByRole('button', { name: 'Upload and tag' });
    expect(submit).toBeDisabled();

    const file = new File(['png'], 'lovart-01.png', { type: 'image/png' });
    fireEvent.change(screen.getByTestId('external-files'), {
      target: { files: [file] },
    });
    expect(screen.getByText('1 selected')).toBeInTheDocument();
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Who made these images'), {
      target: { value: 'designer' },
    });
    fireEvent.click(
      screen.getByRole('checkbox', {
        name: 'I confirm we have the right to use these images in ads',
      })
    );
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    await flush();
    await flush();

    expect(mockStartUpload).toHaveBeenCalledWith(
      'external_creative',
      expect.objectContaining({ file, ownerKind: 'external_creative' })
    );
    expect(mockGateway.createExternalCreatives.mock.calls[0][0].body).toEqual({
      product_id: 'prod_1',
      upload_intent_ids: ['ui_1'],
      rights: { made_by: 'designer', permission_confirmed: true, note: null },
    });
    expect(screen.getByText('Confirm external asset tags')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm tags' }));
    await flush();
    expect(mockGateway.confirmExternalCreative).toHaveBeenCalledWith(
      expect.objectContaining({ externalCreativeId: 'exc_1' })
    );
    expect(screen.getByText('Confirmed')).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalled();
  });

  it('refuses unsupported files before any upload', () => {
    render(<ExternalCreativeDialog open onClose={jest.fn()} />);
    fireEvent.change(screen.getByTestId('external-files'), {
      target: { files: [new File(['gif'], 'loop.gif', { type: 'image/gif' })] },
    });
    expect(screen.getByRole('alert')).toHaveTextContent(
      'loop.gif is not a PNG, JPEG or WebP'
    );
    expect(mockStartUpload).not.toHaveBeenCalled();
  });

  it('explains a refused admission', async () => {
    mockStartUpload.mockResolvedValue({
      kind: 'accepted',
      uploadIntentId: 'ui_1',
      operationRecordId: 'op_up_1',
    });
    mockGateway.pollOperation.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: { state: 'succeeded' },
      meta: {},
    });
    mockGateway.createExternalCreatives.mockResolvedValue({
      kind: 'problem',
      status: 409,
      problem: { code: 'guardrail_exceeded', detail: 'over' },
    });
    render(
      <ExternalCreativeDialog
        open
        onClose={jest.fn()}
        defaultProductId="prod_1"
      />
    );
    fireEvent.change(screen.getByTestId('external-files'), {
      target: { files: [new File(['png'], 'a.png', { type: 'image/png' })] },
    });
    fireEvent.click(
      screen.getByRole('checkbox', {
        name: 'I confirm we have the right to use these images in ads',
      })
    );
    fireEvent.click(screen.getByRole('button', { name: 'Upload and tag' }));
    await flush();
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent(
      "AI tagging would exceed the workspace's spend guardrail"
    );
  });
});
