/**
 * @jest-environment jsdom
 */
import { createElement } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) => {
      if (opts?.defaultValue) return `library:${key}`;
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

import {
  EntryActions,
  type AllowedAction,
} from '@/components/library/commands/EntryActions';
import { VersionActions } from '@/components/library/commands/VersionActions';

function makeEntryHandlers() {
  return {
    onPublish: jest.fn(),
    onArchive: jest.fn(),
    onFork: jest.fn(),
    onClone: jest.fn(),
    onApplyTemplate: jest.fn(),
    onCreateEntry: jest.fn(),
    onBindReference: jest.fn(),
  };
}

function makeVersionHandlers() {
  return {
    onWithdraw: jest.fn(),
    onFork: jest.fn(),
    onClone: jest.fn(),
    onDownload: jest.fn(),
  };
}

describe('EntryActions', () => {
  it('renders toolbar with allowed buttons', () => {
    const handlers = makeEntryHandlers();
    const allowedActions: AllowedAction[] = [
      { command: 'publish', allowed: true, reason_code: null },
      { command: 'archive', allowed: true, reason_code: null },
      { command: 'fork', allowed: true, reason_code: null },
    ];

    render(
      createElement(EntryActions, {
        entryId: 'e1',
        entryType: 'product' as const,
        lifecycle: 'active',
        allowedActions,
        ...handlers,
      })
    );

    const toolbar = screen.getByRole('toolbar');
    expect(toolbar).toHaveAttribute(
      'aria-label',
      'library:commands.entry-actions'
    );

    expect(screen.getByText('library:commands.publish')).toBeInTheDocument();
    expect(screen.getByText('library:commands.archive')).toBeInTheDocument();
    expect(screen.getByText('library:commands.fork')).toBeInTheDocument();
  });

  it('calls handler on allowed action click', () => {
    const handlers = makeEntryHandlers();
    const allowedActions: AllowedAction[] = [
      { command: 'publish', allowed: true, reason_code: null },
      { command: 'clone', allowed: true, reason_code: null },
    ];

    render(
      createElement(EntryActions, {
        entryId: 'e1',
        entryType: 'product' as const,
        lifecycle: 'active',
        allowedActions,
        ...handlers,
      })
    );

    fireEvent.click(screen.getByText('library:commands.publish'));
    expect(handlers.onPublish).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText('library:commands.clone'));
    expect(handlers.onClone).toHaveBeenCalledTimes(1);
  });

  it('suppresses click on denied action', () => {
    const handlers = makeEntryHandlers();
    const allowedActions: AllowedAction[] = [
      {
        command: 'publish',
        allowed: false,
        reason_code: 'no_draft',
      },
    ];

    render(
      createElement(EntryActions, {
        entryId: 'e1',
        entryType: 'product' as const,
        lifecycle: 'active',
        allowedActions,
        ...handlers,
      })
    );

    fireEvent.click(screen.getByText('library:commands.publish'));
    expect(handlers.onPublish).not.toHaveBeenCalled();
  });

  it('keeps a denied action focusable with aria-disabled and its reason', () => {
    const handlers = makeEntryHandlers();
    const allowedActions: AllowedAction[] = [
      {
        command: 'archive',
        allowed: false,
        reason_code: 'owner_required',
      },
    ];

    render(
      createElement(EntryActions, {
        entryId: 'e1',
        entryType: 'product' as const,
        lifecycle: 'active',
        allowedActions,
        ...handlers,
      })
    );

    const btn = screen.getByText('library:commands.archive');
    expect(btn).not.toBeDisabled();
    expect(btn).toHaveAttribute('aria-disabled', 'true');
    expect(btn).toHaveAttribute(
      'title',
      'library:commands.denied.owner_required'
    );
    expect(btn).toHaveAccessibleDescription('library:commands.denied-line');
  });

  it('returns null when no actions in allowedActions', () => {
    const handlers = makeEntryHandlers();
    const { container } = render(
      createElement(EntryActions, {
        entryId: 'e1',
        entryType: 'product' as const,
        lifecycle: 'active',
        allowedActions: [],
        ...handlers,
      })
    );

    expect(container.innerHTML).toBe('');
  });

  it('renders create_entry button when present', () => {
    const handlers = makeEntryHandlers();
    const allowedActions: AllowedAction[] = [
      { command: 'create_entry', allowed: true, reason_code: null },
    ];

    render(
      createElement(EntryActions, {
        entryId: 'e1',
        entryType: 'product' as const,
        lifecycle: 'active',
        allowedActions,
        ...handlers,
      })
    );

    const btn = screen.getByText('library:commands.create-entry');
    expect(btn).toBeInTheDocument();

    fireEvent.click(btn);
    expect(handlers.onCreateEntry).toHaveBeenCalledTimes(1);
  });

  it('renders both create_entry and entry commands together', () => {
    const handlers = makeEntryHandlers();
    const allowedActions: AllowedAction[] = [
      { command: 'create_entry', allowed: true, reason_code: null },
      { command: 'publish', allowed: true, reason_code: null },
      { command: 'bind_reference', allowed: true, reason_code: null },
    ];

    render(
      createElement(EntryActions, {
        entryId: 'e1',
        entryType: 'product' as const,
        lifecycle: 'active',
        allowedActions,
        ...handlers,
      })
    );

    expect(
      screen.getByText('library:commands.create-entry')
    ).toBeInTheDocument();
    expect(screen.getByText('library:commands.publish')).toBeInTheDocument();
    expect(
      screen.getByText('library:commands.bind_reference')
    ).toBeInTheDocument();
  });

  it('does not render buttons for unknown commands', () => {
    const handlers = makeEntryHandlers();
    const allowedActions: AllowedAction[] = [
      { command: 'withdraw' as any, allowed: true, reason_code: null },
    ];

    const { container } = render(
      createElement(EntryActions, {
        entryId: 'e1',
        entryType: 'product' as const,
        lifecycle: 'active',
        allowedActions,
        ...handlers,
      })
    );

    expect(container.innerHTML).toBe('');
  });
});

describe('VersionActions', () => {
  it('renders toolbar with allowed buttons', () => {
    const handlers = makeVersionHandlers();
    const allowedActions: AllowedAction[] = [
      { command: 'withdraw', allowed: true, reason_code: null },
      { command: 'fork', allowed: true, reason_code: null },
      {
        command: 'create_reference_download',
        allowed: true,
        reason_code: null,
      },
    ];

    render(
      createElement(VersionActions, {
        versionId: 'v1',
        allowedActions,
        ...handlers,
      })
    );

    const toolbar = screen.getByRole('toolbar');
    expect(toolbar).toHaveAttribute(
      'aria-label',
      'library:commands.version-actions'
    );

    expect(screen.getByText('library:commands.withdraw')).toBeInTheDocument();
    expect(screen.getByText('library:commands.fork')).toBeInTheDocument();
    expect(
      screen.getByText('library:commands.create_reference_download')
    ).toBeInTheDocument();
  });

  it('calls handler on allowed action click', () => {
    const handlers = makeVersionHandlers();
    const allowedActions: AllowedAction[] = [
      { command: 'withdraw', allowed: true, reason_code: null },
      { command: 'clone', allowed: true, reason_code: null },
    ];

    render(
      createElement(VersionActions, {
        versionId: 'v1',
        allowedActions,
        ...handlers,
      })
    );

    fireEvent.click(screen.getByText('library:commands.withdraw'));
    expect(handlers.onWithdraw).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText('library:commands.clone'));
    expect(handlers.onClone).toHaveBeenCalledTimes(1);
  });

  it('suppresses click on denied action', () => {
    const handlers = makeVersionHandlers();
    const allowedActions: AllowedAction[] = [
      {
        command: 'withdraw',
        allowed: false,
        reason_code: 'invalid_state',
      },
    ];

    render(
      createElement(VersionActions, {
        versionId: 'v1',
        allowedActions,
        ...handlers,
      })
    );

    const btn = screen.getByText('library:commands.withdraw');
    fireEvent.click(btn);
    expect(handlers.onWithdraw).not.toHaveBeenCalled();
    expect(btn).toHaveAttribute('aria-disabled', 'true');
    expect(btn).toHaveAttribute(
      'title',
      'library:commands.denied.invalid_state'
    );
  });

  it('returns null when no visible actions', () => {
    const handlers = makeVersionHandlers();
    const { container } = render(
      createElement(VersionActions, {
        versionId: 'v1',
        allowedActions: [],
        ...handlers,
      })
    );

    expect(container.innerHTML).toBe('');
  });
});
