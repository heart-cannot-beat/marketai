/** @jest-environment jsdom */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import { MarketingEvidenceHandoff } from '@/components/workspace/MarketingEvidenceHandoff';
import {
  MarketingProductReferences,
  type ProductReferences,
} from '@/components/workspace/MarketingProductReferences';
import { useState } from 'react';
import messages from '../../../locales/en/common.json';
jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => (messages as Record<string, string>)[key] ?? key,
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws', slug: 'alpha' } }),
}));
const gateway = {
  getMarketingEvidenceHandoff: jest.fn(),
  createMarketingEvidenceHandoff: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
jest.mock('@/hooks/library/usePublishedReferenceAssets', () => ({
  usePublishedReferenceAssets: () => ({
    items: [1, 2, 3, 4].map((n) => ({
      versionId: 'ref-' + n,
      displayName: 'Product photo ' + n,
    })),
    isLoading: false,
    hasMore: false,
  }),
}));
jest.mock('@/components/library/references/ReferenceUploadZone', () => ({
  ReferenceUploadZone: () => null,
}));
jest.mock('@/components/library/references/ReferenceThumbnail', () => ({
  ReferenceThumbnail: ({ alt }: { alt: string }) => <img alt={alt} />,
}));
const options = {
  evaluation_id: 'evaluation',
  assets: [
    {
      asset_version_id: 'exact',
      label: 'Exact tested ad',
      localization: true,
      scale: false,
      scope: 'exact_asset_only',
    },
  ],
  offers: [
    {
      id: 'offer',
      label: 'Single adapter',
      product: 'Adapter',
      context: { market: 'US', placement: 'feed' },
      item: { sku: 'adapter', quantity: 1 },
      landing_page: 'https://product.test/adapter',
    },
  ],
  existing: [],
};
const wrap = (ui: React.ReactNode) =>
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {ui}
    </SWRConfig>
  );
beforeEach(() => {
  jest.clearAllMocks();
  Object.defineProperty(globalThis.crypto, 'randomUUID', {
    configurable: true,
    value: () => 'action',
  });
  gateway.getMarketingEvidenceHandoff.mockResolvedValue({
    kind: 'ok',
    data: { id: 'round', data: options },
  });
  gateway.createMarketingEvidenceHandoff.mockResolvedValue({
    kind: 'created',
    data: { id: 'decision', data: { no_meta_writes: true } },
  });
});
test('requires exact source, applicable offer and explicit confirmation; creates no paid or Meta task', async () => {
  wrap(
    <MarketingEvidenceHandoff
      roundId="round"
      revision={9}
      evaluationId="evaluation"
    />
  );
  const button = await screen.findByRole('button', {
    name: messages['marketing-handoff-create'],
  });
  expect(button).toBeDisabled();
  fireEvent.change(screen.getByLabelText(messages['marketing-handoff-asset']), {
    target: { value: 'exact' },
  });
  fireEvent.change(screen.getByLabelText(messages['marketing-handoff-offer']), {
    target: { value: 'offer' },
  });
  expect(
    screen.getByText(messages['marketing-handoff-asset-scope'])
  ).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(messages['marketing-handoff-note']), {
    target: { value: 'Same product, variant, offer and audience.' },
  });
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(button);
  await waitFor(() =>
    expect(gateway.createMarketingEvidenceHandoff).toHaveBeenCalledWith({
      workspaceSlug: 'alpha',
      roundId: 'round',
      expectedRevision: 9,
      actionInstanceId: 'action',
      body: {
        evaluation_id: 'evaluation',
        asset_version_id: 'exact',
        offer_version_id: 'offer',
        confirms_tested_context: true,
        note: 'Same product, variant, offer and audience.',
      },
    })
  );
  expect(
    screen.queryByRole('link', {
      name: messages['marketing-handoff-open-scale'],
    })
  ).not.toBeInTheDocument();
});
test('no data or mock cannot be confirmed, and stale handoffs have no continue links', async () => {
  gateway.getMarketingEvidenceHandoff.mockResolvedValue({
    kind: 'ok',
    data: {
      id: 'round',
      data: {
        ...options,
        assets: [],
        offers: [],
        existing: [
          {
            decision_id: 'old',
            current: false,
            scope: 'repeatable_combination',
          },
        ],
      },
    },
  });
  wrap(
    <MarketingEvidenceHandoff
      roundId="round"
      revision={10}
      evaluationId="evaluation"
    />
  );
  expect(
    await screen.findByText(messages['marketing-handoff-not-qualified'])
  ).toBeInTheDocument();
  expect(
    screen.getByText(messages['marketing-handoff-stale'])
  ).toBeInTheDocument();
  expect(
    screen.queryByRole('button', { name: messages['marketing-handoff-create'] })
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole('link', {
      name: messages['marketing-handoff-open-scale'],
    })
  ).not.toBeInTheDocument();
});
test('photo selection caps at three and editing its source invalidates the attestation', () => {
  function Fixture() {
    const [value, setValue] = useState<ProductReferences>({
      reference_version_ids: [],
      source: '',
      confirms_real_product: false,
    });
    return (
      <MarketingProductReferences
        projectId="project"
        value={value}
        onChange={setValue}
        disabled={false}
      />
    );
  }
  wrap(<Fixture />);
  for (const n of [1, 2, 3])
    fireEvent.click(
      screen.getByRole('checkbox', { name: 'Product photo ' + n })
    );
  expect(
    screen.getByRole('checkbox', { name: 'Product photo 4' })
  ).toBeDisabled();
  const confirmation = screen.getByRole('checkbox', {
    name: messages['marketing-product-photo-confirmation'],
  });
  fireEvent.click(confirmation);
  expect(confirmation).toBeChecked();
  fireEvent.change(
    screen.getByLabelText(messages['marketing-product-photo-source']),
    { target: { value: 'Manufacturer photo, exact variant' } }
  );
  expect(confirmation).not.toBeChecked();
});
