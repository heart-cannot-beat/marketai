/**
 * @jest-environment jsdom
 */
import { createElement, useEffect, type ReactNode } from 'react';
import { render, screen, act, fireEvent } from '@testing-library/react';
import { renderHook } from '@testing-library/react';
import {
  NavigationBlockerProvider,
  useNavigationBlocker,
  useOptionalNavigationBlocker,
} from '@/components/workspace/NavigationBlocker';
import { GuardedLink } from '@/components/workspace/GuardedLink';

const mockPush = jest.fn().mockResolvedValue(true);
const mockBeforePopState = jest.fn();
const mockAsPath = '/w/acme/library';

const mockRouter = {
  push: mockPush,
  asPath: mockAsPath,
  query: { slug: 'acme' },
  beforePopState: mockBeforePopState,
};

jest.mock('next/router', () => ({
  useRouter: () => mockRouter,
}));

jest.mock('next/link', () => ({
  __esModule: true,
  default: ({
    href,
    children,
    onClick,
    ...rest
  }: {
    href: string;
    children: ReactNode;
    onClick?: React.MouseEventHandler;
    [key: string]: unknown;
  }) => createElement('a', { href, onClick, ...rest }, children),
}));

function Wrapper({ children }: { children: ReactNode }) {
  return createElement(NavigationBlockerProvider, null, children);
}

describe('NavigationBlockerProvider', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockRouter.asPath = '/w/acme/library';
    mockRouter.push = mockPush;
    mockRouter.beforePopState = mockBeforePopState;
  });

  it('isBlocked returns false with no registrations', () => {
    const { result } = renderHook(() => useNavigationBlocker(), {
      wrapper: Wrapper,
    });
    expect(result.current.isBlocked()).toBe(false);
  });

  it('isBlocked returns true when a registration reports blocked', () => {
    const { result } = renderHook(() => useNavigationBlocker(), {
      wrapper: Wrapper,
    });

    act(() => {
      result.current.register({
        isBlocked: () => true,
        onDiscard: jest.fn(),
      });
    });

    expect(result.current.isBlocked()).toBe(true);
  });

  it('register returns an unsubscribe function that removes the blocker', () => {
    const { result } = renderHook(() => useNavigationBlocker(), {
      wrapper: Wrapper,
    });

    let unsub: () => void;
    act(() => {
      unsub = result.current.register({
        isBlocked: () => true,
        onDiscard: jest.fn(),
      });
    });
    expect(result.current.isBlocked()).toBe(true);

    act(() => {
      unsub();
    });
    expect(result.current.isBlocked()).toBe(false);
  });

  it('navigate routes directly when not blocked', async () => {
    const { result } = renderHook(() => useNavigationBlocker(), {
      wrapper: Wrapper,
    });

    let navigated: boolean | undefined;
    await act(async () => {
      navigated = await result.current.navigate('/w/acme/assets');
    });

    expect(navigated).toBe(true);
    expect(mockPush).toHaveBeenCalledWith('/w/acme/assets');
  });

  it('navigate shows confirm and routes on acceptance', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    const onDiscard = jest.fn();

    const { result } = renderHook(() => useNavigationBlocker(), {
      wrapper: Wrapper,
    });

    act(() => {
      result.current.register({ isBlocked: () => true, onDiscard });
    });

    let navigated: boolean | undefined;
    await act(async () => {
      navigated = await result.current.navigate('/w/acme/assets');
    });

    expect(confirmSpy).toHaveBeenCalled();
    expect(onDiscard).toHaveBeenCalled();
    expect(mockPush).toHaveBeenCalledWith('/w/acme/assets');
    expect(navigated).toBe(true);

    confirmSpy.mockRestore();
  });

  it('navigate returns false when user cancels confirmation', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);

    const { result } = renderHook(() => useNavigationBlocker(), {
      wrapper: Wrapper,
    });

    act(() => {
      result.current.register({
        isBlocked: () => true,
        onDiscard: jest.fn(),
      });
    });

    let navigated: boolean | undefined;
    await act(async () => {
      navigated = await result.current.navigate('/w/acme/assets');
    });

    expect(confirmSpy).toHaveBeenCalled();
    expect(mockPush).not.toHaveBeenCalled();
    expect(navigated).toBe(false);

    confirmSpy.mockRestore();
  });

  it('calls discardAll on all blocked registrations before routing', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    const discard1 = jest.fn();
    const discard2 = jest.fn();

    const { result } = renderHook(() => useNavigationBlocker(), {
      wrapper: Wrapper,
    });

    act(() => {
      result.current.register({ isBlocked: () => true, onDiscard: discard1 });
      result.current.register({ isBlocked: () => true, onDiscard: discard2 });
    });

    await act(async () => {
      await result.current.navigate('/w/acme/assets');
    });

    expect(discard1).toHaveBeenCalled();
    expect(discard2).toHaveBeenCalled();

    confirmSpy.mockRestore();
  });

  it('fires beforeunload preventDefault when blocked', () => {
    const { result } = renderHook(() => useNavigationBlocker(), {
      wrapper: Wrapper,
    });

    act(() => {
      result.current.register({
        isBlocked: () => true,
        onDiscard: jest.fn(),
      });
    });

    const event = new Event('beforeunload', { cancelable: true });
    const spy = jest.spyOn(event, 'preventDefault');
    window.dispatchEvent(event);

    expect(spy).toHaveBeenCalled();
  });

  it('does not fire beforeunload preventDefault when not blocked', () => {
    renderHook(() => useNavigationBlocker(), {
      wrapper: Wrapper,
    });

    const event = new Event('beforeunload', { cancelable: true });
    const spy = jest.spyOn(event, 'preventDefault');
    window.dispatchEvent(event);

    expect(spy).not.toHaveBeenCalled();
  });
});

describe('useOptionalNavigationBlocker', () => {
  it('returns null outside of provider', () => {
    const { result } = renderHook(() => useOptionalNavigationBlocker());
    expect(result.current).toBeNull();
  });

  it('returns context inside provider', () => {
    const { result } = renderHook(() => useOptionalNavigationBlocker(), {
      wrapper: Wrapper,
    });
    expect(result.current).not.toBeNull();
    expect(result.current!.isBlocked).toBeDefined();
  });
});

describe('useNavigationBlocker', () => {
  it('throws outside of provider', () => {
    const consoleError = jest
      .spyOn(console, 'error')
      .mockImplementation(() => {});

    expect(() => {
      renderHook(() => useNavigationBlocker());
    }).toThrow(
      'useNavigationBlocker must be used within NavigationBlockerProvider'
    );

    consoleError.mockRestore();
  });
});

describe('GuardedLink', () => {
  it('renders children as a link', () => {
    render(
      createElement(
        Wrapper,
        null,
        createElement(GuardedLink, { href: '/w/acme/assets' }, 'Assets')
      )
    );
    const link = screen.getByText('Assets');
    expect(link).toBeInTheDocument();
    expect(link.closest('a')).toHaveAttribute('href', '/w/acme/assets');
  });

  it('passes through click when not blocked', () => {
    render(
      createElement(
        Wrapper,
        null,
        createElement(GuardedLink, { href: '/w/acme/assets' }, 'Assets')
      )
    );
    const link = screen.getByText('Assets');
    fireEvent.click(link);
    // No preventDefault — click proceeds normally (router handles it)
    expect(mockPush).not.toHaveBeenCalled(); // GuardedLink delegates to Link, not router.push
  });

  it('intercepts click and calls navigate when blocked', () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);

    function BlockedChild() {
      const blocker = useNavigationBlocker();
      useEffect(() => {
        return blocker.register({
          isBlocked: () => true,
          onDiscard: jest.fn(),
        });
      }, [blocker]);
      return createElement(GuardedLink, { href: '/w/acme/assets' }, 'Assets');
    }

    render(createElement(Wrapper, null, createElement(BlockedChild)));
    const link = screen.getByText('Assets');
    fireEvent.click(link);

    expect(confirmSpy).toHaveBeenCalled();
    expect(mockPush).not.toHaveBeenCalled();

    confirmSpy.mockRestore();
  });

  it('works outside provider context (falls through to normal Link)', () => {
    // No Wrapper — GuardedLink uses useOptionalNavigationBlocker
    render(createElement(GuardedLink, { href: '/w/acme/assets' }, 'Assets'));
    const link = screen.getByText('Assets');
    expect(link).toBeInTheDocument();
    fireEvent.click(link);
    // No blocker context — click passes through normally
  });
});
