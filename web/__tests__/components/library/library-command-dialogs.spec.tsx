/**
 * @jest-environment jsdom
 */
import { createElement } from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

let mockRun: jest.Mock;
let mockReset: jest.Mock;
let mockInvalidate: jest.Mock;
let mockImpactFetch: jest.Mock;
let mockImpactClear: jest.Mock;
let mockImpactConfirm: jest.Mock;
let mockUseLibraryCommandImpact: jest.Mock;
let mockUseDomainQuery: jest.Mock;

jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, string>) => {
      if (opts) {
        let result = `library:${key}`;
        for (const [k, v] of Object.entries(opts)) {
          result = result.replace(`{{${k}}}`, String(v));
        }
        return result;
      }
      return `library:${key}`;
    },
  }),
}));

jest.mock('next/router', () => ({
  useRouter: () => ({
    query: { slug: 'acme' },
    asPath: '/w/acme/library',
    push: jest.fn(),
    replace: jest.fn(),
  }),
}));

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: {
      slug: 'acme',
      workspaceId: 'w1',
      role: 'OWNER',
      membershipId: 'm1',
      displayName: 'Acme',
    },
  }),
}));

jest.mock('@/hooks/library/useLibraryCommand', () => ({
  useLibraryCommand: () => ({
    run: mockRun,
    retry: jest.fn(),
    reset: mockReset,
    dismiss: jest.fn(),
    isPending: false,
    problem: undefined,
    reconciliation: undefined,
  }),
}));

jest.mock('@/hooks/library/useLibraryCache', () => ({
  useLibraryCache: () => ({ invalidate: mockInvalidate }),
}));

jest.mock('@/hooks/library/useLibraryCommandImpact', () => ({
  useLibraryCommandImpact: (...args: unknown[]) =>
    mockUseLibraryCommandImpact(...args),
}));

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (...args: unknown[]) => mockUseDomainQuery(...args),
}));

jest.mock('@/lib/library/navigation', () => ({
  rootTypeForCategory: (cat: string) => (cat === 'product' ? 'product' : cat),
}));

import { CreateRootEntryDialog } from '@/components/library/commands/CreateRootEntryDialog';
import { PublishDialog } from '@/components/library/commands/PublishDialog';
import { WithdrawDialog } from '@/components/library/commands/WithdrawDialog';
import { ArchiveDialog } from '@/components/library/commands/ArchiveDialog';
import { ForkDialog } from '@/components/library/commands/ForkDialog';
import { CloneProductDialog } from '@/components/library/commands/CloneProductDialog';
import { ReconciliationBanner } from '@/components/library/commands/ReconciliationBanner';

function defaultImpactHook() {
  return {
    impact: null,
    isLoading: false,
    problem: undefined,
    isStale: false,
    fetch: mockImpactFetch,
    clear: mockImpactClear,
    confirmed: false,
    confirm: mockImpactConfirm,
    resetConfirmation: jest.fn(),
  };
}

function loadedImpactHook(overrides?: Record<string, unknown>) {
  return {
    ...defaultImpactHook(),
    impact: {
      impactId: 'imp-1',
      impactHash: 'h1',
      commandKind: 'publish',
      targetId: 'v1',
      targetRevision: 1,
      contentHash: 'ch1',
      expiry: 9999,
      affectedSummaries: [],
      blocking: [],
      warnings: [],
      ...overrides,
    },
  };
}

function publishDialogProps(
  overrides: Partial<Parameters<typeof PublishDialog>[0]> = {}
) {
  return {
    entryId: 'e1',
    versionId: 'v1',
    open: true,
    onClose: jest.fn(),
    onPublished: jest.fn(),
    ...overrides,
  };
}

function withdrawDialogProps(
  overrides: Partial<Parameters<typeof WithdrawDialog>[0]> = {}
) {
  return {
    versionId: 'v1',
    open: true,
    onClose: jest.fn(),
    onWithdrawn: jest.fn(),
    ...overrides,
  };
}

beforeEach(() => {
  mockRun = jest.fn().mockResolvedValue({
    kind: 'success',
    data: { id: 'new-1' },
    meta: { etag: null, retryAt: null, location: null },
  });
  mockReset = jest.fn();
  mockInvalidate = jest.fn().mockResolvedValue(undefined);
  mockImpactFetch = jest.fn().mockResolvedValue(undefined);
  mockImpactClear = jest.fn();
  mockImpactConfirm = jest.fn();
  mockUseLibraryCommandImpact = jest.fn().mockReturnValue(defaultImpactHook());
  mockUseDomainQuery = jest.fn().mockReturnValue({ data: null });
});

// ── CreateRootEntryDialog ──────────────────────────────────────────

describe('CreateRootEntryDialog', () => {
  it('returns null when closed', () => {
    const { container } = render(
      createElement(CreateRootEntryDialog, {
        category: 'product' as const,
        open: false,
        onClose: jest.fn(),
        onCreated: jest.fn(),
      })
    );
    expect(container.innerHTML).toBe('');
  });

  it('shows title with entry type when open', () => {
    render(
      createElement(CreateRootEntryDialog, {
        category: 'product' as const,
        open: true,
        onClose: jest.fn(),
        onCreated: jest.fn(),
      })
    );
    expect(
      screen.getByText('library:commands.create-root-entry-title')
    ).toBeInTheDocument();
  });

  it('create button disabled when display name empty', () => {
    render(
      createElement(CreateRootEntryDialog, {
        category: 'product' as const,
        open: true,
        onClose: jest.fn(),
        onCreated: jest.fn(),
      })
    );
    const btn = screen.getByText('library:commands.create');
    expect(btn).toBeDisabled();
  });

  it('calls onCreated with entryId on success', async () => {
    const onCreated = jest.fn();
    render(
      createElement(CreateRootEntryDialog, {
        category: 'product' as const,
        open: true,
        onClose: jest.fn(),
        onCreated,
      })
    );

    const input = screen.getByRole('textbox', { hidden: true });
    fireEvent.change(input, { target: { value: 'My Product' } });
    fireEvent.submit(input.closest('form')!);

    await new Promise((r) => setTimeout(r, 0));
    expect(mockRun).toHaveBeenCalled();
    expect(onCreated).toHaveBeenCalledWith('new-1');
  });
});

// ── PublishDialog ──────────────────────────────────────────────────

describe('PublishDialog', () => {
  it('returns null when closed', () => {
    const { container } = render(
      createElement(PublishDialog, publishDialogProps({ open: false }))
    );
    expect(container.innerHTML).toBe('');
  });

  it('shows title when open', () => {
    render(createElement(PublishDialog, publishDialogProps()));
    expect(
      screen.getByText('library:commands.publish-title')
    ).toBeInTheDocument();
  });

  it('publish button disabled when no impact', () => {
    render(createElement(PublishDialog, publishDialogProps()));
    expect(screen.getByText('library:commands.confirm-publish')).toBeDisabled();
  });

  it('shows loading when impact isLoading', () => {
    mockUseLibraryCommandImpact.mockReturnValue({
      ...defaultImpactHook(),
      isLoading: true,
    });

    render(createElement(PublishDialog, publishDialogProps()));
    expect(
      screen.getByText('library:commands.loading-impact')
    ).toBeInTheDocument();
  });

  it('shows blocking reasons in alert', () => {
    mockUseLibraryCommandImpact.mockReturnValue(
      loadedImpactHook({
        blocking: [
          {
            code: 'fact_set_empty',
            message: '至少需要一条事实',
            path: 'facts',
          },
        ],
      })
    );

    render(createElement(PublishDialog, publishDialogProps()));

    // Worded through the finding's copy, never the server's own sentence.
    expect(
      screen.getByText('library:validation-findings.fact_set_empty')
    ).toBeInTheDocument();
    expect(screen.getByText('facts:', { exact: false })).toBeInTheDocument();
    expect(screen.queryByText('至少需要一条事实')).toBeNull();
    const alerts = screen.getAllByRole('alert', { hidden: true });
    expect(alerts.length).toBeGreaterThanOrEqual(1);
  });

  it('shows affected summaries and warnings', () => {
    mockUseLibraryCommandImpact.mockReturnValue(
      loadedImpactHook({
        affectedSummaries: [
          {
            id: 's1',
            display_name: 'Dep A',
            entry_type: 'product',
            effect: 'cascade',
          },
        ],
        warnings: [{ code: 'minor_impact', message: 'Minor impact', path: '' }],
      })
    );

    render(createElement(PublishDialog, publishDialogProps()));

    expect(screen.getByText('Dep A')).toBeInTheDocument();
    expect(screen.getByText('cascade')).toBeInTheDocument();
    expect(
      screen.getByText('library:validation-findings.minor_impact')
    ).toBeInTheDocument();
  });

  describe('no MFA after login (ADR 0015)', () => {
    it('publishes directly, without asking for an authenticator code', async () => {
      mockUseLibraryCommandImpact.mockReturnValue(loadedImpactHook());
      const onPublished = jest.fn();
      render(createElement(PublishDialog, publishDialogProps({ onPublished })));

      expect(
        screen.queryByTestId('mfa-challenge-code')
      ).not.toBeInTheDocument();
      fireEvent.click(screen.getByText('library:commands.confirm-publish'));
      await waitFor(() => expect(mockRun).toHaveBeenCalledTimes(1));

      const params = mockRun.mock.calls[0][0];
      expect(params).not.toHaveProperty('stepUpGrant');
      expect(params).toMatchObject({
        entry_id: 'e1',
        ifMatch: 1,
        body: {
          impact_id: 'imp-1',
          impact_hash: 'h1',
          acknowledged_warnings: [],
        },
      });
      await waitFor(() => expect(onPublished).toHaveBeenCalledTimes(1));
    });
  });

  describe('non-blocking warnings', () => {
    const warned = () =>
      loadedImpactHook({
        warnings: [
          { code: 'minor_impact', message: 'Minor impact', path: '' },
          { code: 'audience_review', message: 'Check the audience', path: '' },
        ],
      });

    it('requires an explicit acknowledgement before publishing', async () => {
      mockUseLibraryCommandImpact.mockReturnValue(warned());
      render(createElement(PublishDialog, publishDialogProps()));

      const confirm = screen.getByText('library:commands.confirm-publish');
      expect(confirm).toBeDisabled();

      fireEvent.click(screen.getByTestId('publish-acknowledge-warnings'));
      expect(confirm).not.toBeDisabled();

      fireEvent.click(confirm);
      await waitFor(() => expect(mockRun).toHaveBeenCalledTimes(1));
      expect(mockRun.mock.calls[0][0]).toMatchObject({
        body: {
          acknowledged_warnings: ['minor_impact', 'audience_review'],
        },
      });
    });

    it('only asks for the warnings the caller has not already acknowledged', async () => {
      mockUseLibraryCommandImpact.mockReturnValue(warned());
      render(
        createElement(
          PublishDialog,
          publishDialogProps({ acknowledgedWarnings: ['minor_impact'] })
        )
      );

      expect(
        screen.getByText('library:commands.confirm-publish')
      ).toBeDisabled();
      fireEvent.click(screen.getByTestId('publish-acknowledge-warnings'));
      fireEvent.click(screen.getByText('library:commands.confirm-publish'));
      await waitFor(() => expect(mockRun).toHaveBeenCalledTimes(1));

      const sent = mockRun.mock.calls[0][0].body.acknowledged_warnings;
      expect([...sent].sort()).toEqual(['audience_review', 'minor_impact']);
    });

    it('needs no acknowledgement when every warning is already acknowledged', () => {
      mockUseLibraryCommandImpact.mockReturnValue(warned());
      render(
        createElement(
          PublishDialog,
          publishDialogProps({
            acknowledgedWarnings: ['minor_impact', 'audience_review'],
          })
        )
      );

      expect(
        screen.queryByTestId('publish-acknowledge-warnings')
      ).not.toBeInTheDocument();
      expect(
        screen.getByText('library:commands.confirm-publish')
      ).not.toBeDisabled();
    });
  });
});

// ── WithdrawDialog ─────────────────────────────────────────────────

describe('WithdrawDialog', () => {
  it('returns null when closed', () => {
    const { container } = render(
      createElement(WithdrawDialog, withdrawDialogProps({ open: false }))
    );
    expect(container.innerHTML).toBe('');
  });

  it('shows title and description when open', () => {
    render(createElement(WithdrawDialog, withdrawDialogProps()));
    expect(
      screen.getByText('library:commands.withdraw-title')
    ).toBeInTheDocument();
    expect(
      screen.getByText('library:commands.withdraw-description')
    ).toBeInTheDocument();
  });

  it('withdraw button disabled when reason empty', () => {
    mockUseLibraryCommandImpact.mockReturnValue(
      loadedImpactHook({ commandKind: 'withdraw' })
    );

    render(createElement(WithdrawDialog, withdrawDialogProps()));
    expect(
      screen.getByText('library:commands.confirm-withdraw')
    ).toBeDisabled();
  });

  it('shows reason textarea', () => {
    render(createElement(WithdrawDialog, withdrawDialogProps()));
    expect(
      screen.getByText('library:commands.withdraw-reason-label')
    ).toBeInTheDocument();
  });

  describe('no MFA after login (ADR 0015)', () => {
    it('withdraws directly, without asking for an authenticator code', async () => {
      mockUseLibraryCommandImpact.mockReturnValue(
        loadedImpactHook({ commandKind: 'withdraw' })
      );
      const onWithdrawn = jest.fn();
      render(
        createElement(WithdrawDialog, withdrawDialogProps({ onWithdrawn }))
      );

      expect(
        screen.queryByTestId('mfa-challenge-code')
      ).not.toBeInTheDocument();
      fireEvent.change(screen.getByRole('textbox', { hidden: true }), {
        target: { value: 'recall for legal review' },
      });
      fireEvent.click(screen.getByText('library:commands.confirm-withdraw'));
      await waitFor(() => expect(mockRun).toHaveBeenCalledTimes(1));

      const params = mockRun.mock.calls[0][0];
      expect(params).not.toHaveProperty('stepUpGrant');
      expect(params).toMatchObject({
        version_id: 'v1',
        body: { reason: 'recall for legal review' },
      });
      await waitFor(() => expect(onWithdrawn).toHaveBeenCalledTimes(1));
    });
  });
});

// ── ArchiveDialog ──────────────────────────────────────────────────

describe('ArchiveDialog', () => {
  it('returns null when closed', () => {
    const { container } = render(
      createElement(ArchiveDialog, {
        entryId: 'e1',
        open: false,
        onClose: jest.fn(),
        onArchived: jest.fn(),
      })
    );
    expect(container.innerHTML).toBe('');
  });

  it('shows title, description, and amber notice', () => {
    render(
      createElement(ArchiveDialog, {
        entryId: 'e1',
        open: true,
        onClose: jest.fn(),
        onArchived: jest.fn(),
      })
    );
    expect(
      screen.getByText('library:commands.archive-title')
    ).toBeInTheDocument();
    expect(
      screen.getByText('library:commands.archive-description')
    ).toBeInTheDocument();
    expect(
      screen.getByText('library:commands.archive-not-deletion')
    ).toBeInTheDocument();
  });

  it('archive button disabled until checkbox checked and impact loaded', () => {
    mockUseLibraryCommandImpact.mockReturnValue(
      loadedImpactHook({ commandKind: 'archive' })
    );

    render(
      createElement(ArchiveDialog, {
        entryId: 'e1',
        open: true,
        onClose: jest.fn(),
        onArchived: jest.fn(),
      })
    );

    const archiveBtn = screen.getByText('library:commands.confirm-archive');
    expect(archiveBtn).toBeDisabled();

    const checkbox = screen.getByRole('checkbox', { hidden: true });
    fireEvent.click(checkbox);
    expect(archiveBtn).not.toBeDisabled();
  });

  it('archive button disabled when no impact even with checkbox', () => {
    render(
      createElement(ArchiveDialog, {
        entryId: 'e1',
        open: true,
        onClose: jest.fn(),
        onArchived: jest.fn(),
      })
    );

    const checkbox = screen.getByRole('checkbox', { hidden: true });
    fireEvent.click(checkbox);

    expect(screen.getByText('library:commands.confirm-archive')).toBeDisabled();
  });
});

// ── ForkDialog ─────────────────────────────────────────────────────

describe('ForkDialog', () => {
  it('returns null when closed', () => {
    const { container } = render(
      createElement(ForkDialog, {
        versionId: 'v1',
        sourceDisplayName: 'Widget',
        open: false,
        onClose: jest.fn(),
        onForked: jest.fn(),
      })
    );
    expect(container.innerHTML).toBe('');
  });

  it('defaults display name to source (fork)', () => {
    render(
      createElement(ForkDialog, {
        versionId: 'v1',
        sourceDisplayName: 'Widget',
        open: true,
        onClose: jest.fn(),
        onForked: jest.fn(),
      })
    );

    const input = screen.getByRole('textbox', {
      hidden: true,
    }) as HTMLInputElement;
    expect(input.value).toBe('Widget (fork)');
  });

  it('fork button disabled when name empty', () => {
    render(
      createElement(ForkDialog, {
        versionId: 'v1',
        sourceDisplayName: 'Widget',
        open: true,
        onClose: jest.fn(),
        onForked: jest.fn(),
      })
    );

    const input = screen.getByRole('textbox', { hidden: true });
    fireEvent.change(input, { target: { value: '' } });

    expect(screen.getByText('library:commands.confirm-fork')).toBeDisabled();
  });
});

// ── CloneProductDialog ─────────────────────────────────────────────

describe('CloneProductDialog', () => {
  it('returns null when closed', () => {
    const { container } = render(
      createElement(CloneProductDialog, {
        versionId: 'v1',
        sourceDisplayName: 'Widget',
        open: false,
        onClose: jest.fn(),
        onCloned: jest.fn(),
      })
    );
    expect(container.innerHTML).toBe('');
  });

  it('defaults display name to source (copy)', () => {
    render(
      createElement(CloneProductDialog, {
        versionId: 'v1',
        sourceDisplayName: 'Widget',
        open: true,
        onClose: jest.fn(),
        onCloned: jest.fn(),
      })
    );

    const input = screen.getByRole('textbox', {
      hidden: true,
    }) as HTMLInputElement;
    expect(input.value).toBe('Widget (copy)');
  });

  it('shows inclusion checkboxes', () => {
    render(
      createElement(CloneProductDialog, {
        versionId: 'v1',
        sourceDisplayName: 'Widget',
        open: true,
        onClose: jest.fn(),
        onCloned: jest.fn(),
      })
    );

    const checkboxes = screen.getAllByRole('checkbox', { hidden: true });
    expect(checkboxes).toHaveLength(3);
    expect(
      screen.getByText('library:commands.clone-include-offers')
    ).toBeInTheDocument();
    expect(
      screen.getByText('library:commands.clone-include-references')
    ).toBeInTheDocument();
    expect(
      screen.getByText('library:commands.clone-include-dimension_schemas')
    ).toBeInTheDocument();
  });
});

// ── ReconciliationBanner ───────────────────────────────────────────

describe('ReconciliationBanner', () => {
  it('shows retry_safe title and retry button', () => {
    const onRetry = jest.fn();
    render(
      createElement(ReconciliationBanner, {
        state: { kind: 'retry_safe' as const, message: 'Timed out' },
        onDismiss: jest.fn(),
        onRetry,
      })
    );

    expect(
      screen.getByText('library:commands.reconciliation.retry_safe-title')
    ).toBeInTheDocument();
    expect(screen.getByText('Timed out')).toBeInTheDocument();

    const retryBtn = screen.getByText('library:commands.reconciliation.retry');
    fireEvent.click(retryBtn);
    expect(onRetry).toHaveBeenCalled();
  });

  it('shows lookup_in_progress title without retry', () => {
    render(
      createElement(ReconciliationBanner, {
        state: { kind: 'lookup_in_progress' as const },
        onDismiss: jest.fn(),
      })
    );

    expect(
      screen.getByText(
        'library:commands.reconciliation.lookup_in_progress-title'
      )
    ).toBeInTheDocument();
    expect(
      screen.queryByText('library:commands.reconciliation.retry')
    ).toBeNull();
  });

  it('shows terminal_problem by its code, not its detail', () => {
    render(
      createElement(ReconciliationBanner, {
        state: {
          kind: 'terminal_problem' as const,
          problem: {
            type: 'error',
            title: 'Error',
            status: 500,
            detail: 'Permanently failed',
            instance: '',
            code: 'internal_error',
            trace_id: '',
            retryable: false,
            field_errors: [],
          },
        },
        onDismiss: jest.fn(),
      })
    );

    expect(
      screen.getByText('library:error-internal_error')
    ).toBeInTheDocument();
    expect(screen.queryByText('Permanently failed')).toBeNull();
  });

  it('dismiss calls onDismiss', () => {
    const onDismiss = jest.fn();
    render(
      createElement(ReconciliationBanner, {
        state: { kind: 'not_applied' as const },
        onDismiss,
      })
    );

    fireEvent.click(
      screen.getByLabelText('library:commands.reconciliation.dismiss')
    );
    expect(onDismiss).toHaveBeenCalled();
  });

  it('has role=alert and aria-live=polite', () => {
    render(
      createElement(ReconciliationBanner, {
        state: { kind: 'not_applied' as const },
        onDismiss: jest.fn(),
      })
    );

    const alert = screen.getByRole('alert');
    expect(alert).toHaveAttribute('aria-live', 'polite');
  });
});
