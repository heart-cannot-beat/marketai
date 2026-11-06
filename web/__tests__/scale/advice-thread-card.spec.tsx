/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent } from '@testing-library/react';
import { AdviceThreadCard } from '@/components/scale/advice/AdviceThreadCard';
import type { AdviceThreadSummary } from '@/hooks/scale/useScaleAdvice';

jest.mock('next-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (
      key: string,
      opts?: { defaultValue?: string } & Record<string, unknown>
    ) => (opts?.defaultValue ? String(opts.defaultValue) : `${ns}:${key}`),
    i18n: { language: 'en' },
  }),
}));

const mockDispositionExecute = jest.fn();
const mockSnoozeExecute = jest.fn();
const mockConvertExecute = jest.fn();

jest.mock('@/hooks/scale/useScaleAdviceCommands', () => ({
  useCreateScaleAdviceDisposition: () => ({
    run: mockDispositionExecute,
    problem: undefined,
    isPending: false,
  }),
  useCreateScaleAdviceSnooze: () => ({
    run: mockSnoozeExecute,
    problem: undefined,
    isPending: false,
  }),
  useConvertScaleAdviceOccurrence: () => ({
    run: mockConvertExecute,
    problem: undefined,
    isPending: false,
  }),
}));

function baseThread(
  overrides: Partial<AdviceThreadSummary> = {}
): AdviceThreadSummary {
  return {
    id: 'thread-1',
    episode_id: 'ep-1',
    enrollment_id: 'enr-1',
    advice_type: 'refresh_creative',
    reason_signature: 'sig',
    severity: 'warning',
    lifecycle: 'active',
    predecessor_thread_id: null,
    first_occurrence_id: 'occ-1',
    latest_occurrence_id: 'occ-2',
    disposition_kind: null,
    disposition_at: null,
    snooze_until: null,
    opened_at: '2026-09-01T00:00:00Z',
    resolved_at: null,
    lifecycle_reason: null,
    revision: 1,
    allowed_actions: [],
    ...overrides,
  };
}

describe('AdviceThreadCard', () => {
  beforeEach(() => {
    mockDispositionExecute.mockReset().mockResolvedValue({ kind: 'success' });
    mockSnoozeExecute.mockReset().mockResolvedValue({ kind: 'success' });
    mockConvertExecute.mockReset().mockResolvedValue({
      kind: 'success',
      data: {
        generation_draft_id: 'generation-draft-1',
        scale_test_plan_draft_id: null,
      },
    });
  });

  it('renders no action buttons when the server lists none as allowed', () => {
    render(
      <AdviceThreadCard
        thread={baseThread({ allowed_actions: [] })}
        metaMode="customer_sandbox"
        onChanged={jest.fn()}
      />
    );
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('only renders the actions the server allowed (acknowledge + snooze, not convert)', () => {
    render(
      <AdviceThreadCard
        thread={baseThread({
          allowed_actions: [
            { action: 'acknowledge', allowed: true, reason_code: null },
            { action: 'snooze', allowed: true, reason_code: null },
          ],
        })}
        metaMode="customer_sandbox"
        onChanged={jest.fn()}
      />
    );
    expect(
      screen.getByText('scale:advice-card.acknowledge')
    ).toBeInTheDocument();
    expect(screen.getByText('scale:advice-card.snooze')).toBeInTheDocument();
    expect(
      screen.queryByText('scale:advice-card.dismiss')
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText('scale:advice-card.convert')
    ).not.toBeInTheDocument();
  });

  it('calls the disposition command with kind "acknowledged" on acknowledge', async () => {
    const onChanged = jest.fn();
    render(
      <AdviceThreadCard
        thread={baseThread({
          allowed_actions: [
            { action: 'acknowledge', allowed: true, reason_code: null },
          ],
        })}
        metaMode="customer_sandbox"
        onChanged={onChanged}
      />
    );
    fireEvent.click(screen.getByText('scale:advice-card.acknowledge'));
    await Promise.resolve();
    expect(mockDispositionExecute).toHaveBeenCalledWith(
      expect.objectContaining({ threadId: 'thread-1', kind: 'acknowledged' })
    );
  });

  it('does not call the command when the server denies the action', () => {
    render(
      <AdviceThreadCard
        thread={baseThread({
          allowed_actions: [
            {
              action: 'convert',
              allowed: false,
              reason_code: 'thread_not_active',
            },
          ],
        })}
        metaMode="customer_sandbox"
        onChanged={jest.fn()}
      />
    );
    fireEvent.click(screen.getByText('scale:advice-card.convert'));
    expect(mockConvertExecute).not.toHaveBeenCalled();
  });

  it('shows the unsubmitted draft identity returned by conversion', async () => {
    render(
      <AdviceThreadCard
        thread={baseThread({
          allowed_actions: [
            { action: 'convert', allowed: true, reason_code: null },
          ],
        })}
        metaMode="customer_sandbox"
        onChanged={jest.fn()}
      />
    );

    fireEvent.click(screen.getByText('scale:advice-card.convert'));
    expect(await screen.findByText('generation-draft-1')).toBeInTheDocument();
    expect(
      screen.getByText(/scale:advice-card.conversion-unsubmitted/)
    ).toBeInTheDocument();
  });
});
