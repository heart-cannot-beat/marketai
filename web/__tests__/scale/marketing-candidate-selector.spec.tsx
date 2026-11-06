/** @jest-environment jsdom */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { CandidateSelector } from '@/components/scale/list/CandidateSelector';
const mockCandidates = jest.fn(),
  mockCreate = jest.fn(),
  mockEnroll = jest.fn();
jest.mock('next-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));
jest.mock('@/hooks/scale/useScaleCandidates', () => ({
  useScaleEnrollmentCandidates: () => mockCandidates(),
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
const source = {
  decision_id: 'mhdecision_exact',
  confirmation_id: 'confirm',
  experiment_version_id: 'version',
  candidate_id: 'candidate',
  asset_version_id: 'asset',
  evidence_origin: 'marketing_confirmed_combination',
};
beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value: function (this: HTMLDialogElement) {
      this.setAttribute('open', '');
    },
  });
  jest.clearAllMocks();
  mockCreate.mockResolvedValue({ kind: 'success', data: { id: 'candidate' } });
  mockEnroll.mockResolvedValue({ kind: 'success', data: { id: 'draft' } });
});
test('selects an observed compatible target without typing internal identifiers', async () => {
  mockCandidates.mockReturnValue({
    data: {
      items: [
        {
          ...source,
          delivery_targets: [
            {
              id: 'observed',
              label: 'US · feed · Meta ad 123',
              source_type: 'observed',
            },
          ],
        },
      ],
    },
    isLoading: false,
  });
  const created = jest.fn();
  render(
    <CandidateSelector
      open
      metaMode="customer_sandbox"
      preferredDecisionId="mhdecision_exact"
      onClose={jest.fn()}
      onCreated={created}
    />
  );
  const target = await screen.findByRole('combobox');
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  fireEvent.change(target, { target: { value: 'observed' } });
  fireEvent.click(
    screen.getByRole('button', { name: 'candidate-selector.create' })
  );
  await waitFor(() => expect(created).toHaveBeenCalledWith('draft'));
  expect(mockCreate).toHaveBeenCalledWith({
    metaMode: 'customer_sandbox',
    winnerDecisionId: 'mhdecision_exact',
    deliveryTargetId: 'observed',
  });
});
test('missing market truth or observed target leaves drafting disabled', async () => {
  mockCandidates.mockReturnValue({
    data: {
      items: [
        {
          ...source,
          delivery_targets: [],
          delivery_unavailable_reason: 'market_binding_required',
        },
      ],
    },
    isLoading: false,
  });
  render(
    <CandidateSelector
      open
      metaMode="customer_sandbox"
      preferredDecisionId="mhdecision_exact"
      onClose={jest.fn()}
      onCreated={jest.fn()}
    />
  );
  expect(
    await screen.findByText('candidate-selector.delivery-not-ready')
  ).toBeInTheDocument();
  expect(
    screen.getByRole('button', { name: 'candidate-selector.create' })
  ).toBeDisabled();
  expect(mockCreate).not.toHaveBeenCalled();
});
