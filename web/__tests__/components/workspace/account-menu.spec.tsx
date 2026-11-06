/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import toast from 'react-hot-toast';
import { useSession } from 'next-auth/react';
import { WorkspaceAccountMenu } from '@/components/workspace/WorkspaceAccountMenu';
import { assignLocation } from '@/lib/browser-navigation';

const push = jest.fn();
jest.mock('next/router', () => ({ useRouter: () => ({ push }) }));
jest.mock('next-auth/react', () => ({ useSession: jest.fn() }));
jest.mock('@/lib/browser-navigation', () => ({ assignLocation: jest.fn() }));
jest.mock('react-hot-toast', () => ({
  __esModule: true,
  default: { error: jest.fn() },
}));

const mockedSession = useSession as jest.Mock;

function signedInAs(user: { name?: string | null; email?: string | null }) {
  mockedSession.mockReturnValue({ status: 'authenticated', data: { user } });
}

describe('WorkspaceAccountMenu', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    global.fetch = jest
      .fn()
      .mockResolvedValue({ ok: true }) as unknown as typeof fetch;
  });

  it('shows who is signed in and opens account, security and sign out', () => {
    signedInAs({ name: 'Wittgenstein', email: 'w@example.com' });
    render(<WorkspaceAccountMenu />);

    const trigger = screen.getByRole('button', { name: /Wittgenstein/ });
    expect(trigger).toHaveTextContent('w@example.com');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('menuitem', { name: 'Account' })).toHaveAttribute(
      'href',
      '/settings/account'
    );
    expect(screen.getByRole('menuitem', { name: 'Security' })).toHaveAttribute(
      'href',
      '/settings/security'
    );
    expect(
      screen.getByRole('menuitem', { name: 'Sign out' })
    ).toBeInTheDocument();
  });

  it('falls back to the email, then to "Account", when there is no name', () => {
    signedInAs({ name: null, email: 'w@example.com' });
    const { unmount } = render(<WorkspaceAccountMenu />);
    expect(
      screen.getByRole('button', { name: 'w@example.com' })
    ).toBeInTheDocument();
    unmount();

    mockedSession.mockReturnValue({ status: 'loading', data: null });
    render(<WorkspaceAccountMenu />);
    expect(screen.getByRole('button', { name: 'Account' })).toBeInTheDocument();
  });

  it('signs out through the control plane and leaves with a full page load', async () => {
    signedInAs({ name: 'Wittgenstein', email: 'w@example.com' });
    render(<WorkspaceAccountMenu />);
    fireEvent.click(screen.getByRole('button', { name: /Wittgenstein/ }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Sign out' }));

    await waitFor(() =>
      expect(assignLocation).toHaveBeenCalledWith('/auth/login')
    );
    expect(global.fetch).toHaveBeenCalledWith(
      '/api/auth/custom-signout',
      expect.objectContaining({ method: 'POST' })
    );
    expect(push).not.toHaveBeenCalled();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('stays put and says so when sign-out fails', async () => {
    signedInAs({ name: 'Wittgenstein', email: 'w@example.com' });
    global.fetch = jest
      .fn()
      .mockResolvedValue({ ok: false }) as unknown as typeof fetch;
    render(<WorkspaceAccountMenu />);
    fireEvent.click(screen.getByRole('button', { name: /Wittgenstein/ }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Sign out' }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(assignLocation).not.toHaveBeenCalled();
  });

  it('closes on Escape and on a click outside, returning focus to the trigger', () => {
    signedInAs({ name: 'Wittgenstein', email: 'w@example.com' });
    render(
      <div>
        <p data-testid="outside" />
        <WorkspaceAccountMenu placement="down" />
      </div>
    );
    const trigger = screen.getByRole('button', { name: /Wittgenstein/ });

    fireEvent.click(trigger);
    expect(screen.getByRole('menu')).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    fireEvent.click(trigger);
    fireEvent.mouseDown(screen.getByTestId('outside'));
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});
