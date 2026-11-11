/** @jest-environment jsdom */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { CandidateSelector } from '@/components/scale/list/CandidateSelector';

/**
 * #129: in New Enrollment, a person picks the observed ad the Winner runs in;
 * the server makes the DeliveryTarget and the dialog enrolls with it. A
 * refusal shows the server's reason and creates nothing.
 */
const mockCandidates = jest.fn(),
  mockObservedAds = jest.fn(),
  mockPick = jest.fn(),
  mockPickProblem = jest.fn(),
  mockCreate = jest.fn(),
  mockEnroll = jest.fn();
jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, values?: Record<string, unknown>) =>
      values && 'ad' in values ? `${key}:${values.ad}` : key,
  }),
}));
jest.mock('@/hooks/scale/useScaleCandidates', () => ({
  useScaleEnrollmentCandidates: () => mockCandidates(),
}));
jest.mock('@/hooks/scale/useScaleObservedAds', () => ({
  useScaleObservedAds: () => mockObservedAds(),
  useCreateScaleDeliveryTarget: () => ({
    run: mockPick,
    reset: jest.fn(),
    isPending: false,
    problem: mockPickProblem(),
  }),
}));
jest.mock('@/hooks/scale', () => ({
  useCreateScaleEnrollmentCandidate: () => ({
    run: mockCreate,
    reset: jest.fn(),
    isPending: false,
  }),
  useCreateScaleEnrollment: () => ({
    run: mockEnroll,
    reset: jest.fn(),
    isPending: false,
  }),
}));

const ad = {
  meta_connection_id: 'mcon_acme',
  ad_account_id: 'act_acme',
  campaign_remote_id: 'cmp_acme',
  adset_remote_id: 'set_acme',
  ad_remote_id: 'ad_acme_dock',
  ad_creative_id: 'creative_acme',
  source_type: 'curated_scenario',
  asset_version_ids: ['astv_acme_dock'],
};

function renderSelector(created = jest.fn()) {
  render(
    <CandidateSelector
      open
      metaMode="customer_sandbox"
      preferredDecisionId="decision_acme"
      onClose={jest.fn()}
      onCreated={created}
    />
  );
  return created;
}

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value: function (this: HTMLDialogElement) {
      this.setAttribute('open', '');
    },
  });
  jest.clearAllMocks();
  mockCandidates.mockReturnValue({
    data: {
      items: [
        {
          decision_id: 'decision_acme',
          confirmation_id: 'confirm',
          experiment_version_id: 'version',
          candidate_id: 'candidate',
          asset_version_id: 'astv_acme_dock',
          evidence_origin: 'legacy_experiment',
        },
      ],
    },
    isLoading: false,
  });
  mockObservedAds.mockReturnValue({
    data: { items: [ad], unavailable: [] },
    isLoading: false,
  });
  mockPickProblem.mockReturnValue(null);
  mockCreate.mockResolvedValue({ kind: 'success', data: { id: 'candidate' } });
  mockEnroll.mockResolvedValue({ kind: 'success', data: { id: 'draft' } });
});

test('picking an observed ad makes the target the enrollment uses', async () => {
  mockPick.mockResolvedValue({
    kind: 'success',
    data: {
      id: 'sdt_acme',
      market_code: 'US',
      placement: 'feed',
      ad_remote_id: 'ad_acme_dock',
    },
  });
  const created = renderSelector();

  fireEvent.click(
    await screen.findByRole('button', {
      name: 'candidate-selector.use-observed-ad:ad_acme_dock',
    })
  );

  await waitFor(() =>
    expect(screen.getByRole('textbox')).toHaveValue('sdt_acme')
  );
  expect(mockPick).toHaveBeenCalledWith({
    metaMode: 'customer_sandbox',
    winnerDecisionId: 'decision_acme',
    metaConnectionId: 'mcon_acme',
    adRemoteId: 'ad_acme_dock',
  });
  expect(screen.getByRole('status')).toHaveTextContent(
    'candidate-selector.observed-ad-picked:ad_acme_dock'
  );
  fireEvent.click(
    screen.getByRole('button', { name: 'candidate-selector.create' })
  );
  await waitFor(() => expect(created).toHaveBeenCalledWith('draft'));
  expect(mockCreate).toHaveBeenCalledWith({
    metaMode: 'customer_sandbox',
    winnerDecisionId: 'decision_acme',
    deliveryTargetId: 'sdt_acme',
  });
});

test('a refused pick shows its reason and leaves the target empty', async () => {
  mockPick.mockResolvedValue({
    kind: 'problem',
    problem: { code: 'target_market_mismatch' },
  });
  mockPickProblem.mockReturnValue({ code: 'target_market_mismatch' });
  renderSelector();

  fireEvent.click(
    await screen.findByRole('button', {
      name: 'candidate-selector.use-observed-ad:ad_acme_dock',
    })
  );

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'error-target_market_mismatch'
  );
  expect(screen.getByRole('textbox')).toHaveValue('');
  expect(
    screen.getByRole('button', { name: 'candidate-selector.create' })
  ).toBeDisabled();
});

test('a connection that has observed nothing says so', async () => {
  mockObservedAds.mockReturnValue({
    data: {
      items: [],
      unavailable: [
        {
          meta_connection_id: 'mcon_acme',
          reason: 'delivery_configuration_not_observed',
        },
      ],
    },
    isLoading: false,
  });
  renderSelector();

  expect(
    await screen.findByText('error-delivery_configuration_not_observed')
  ).toBeInTheDocument();
  expect(
    screen.queryByRole('button', { name: /use-observed-ad/ })
  ).not.toBeInTheDocument();
});
