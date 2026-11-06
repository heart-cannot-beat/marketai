import Link from 'next/link';
import { useEffect, useId, useRef, useState } from 'react';
import { useSession } from 'next-auth/react';
import { useTranslation } from 'next-i18next';
import {
  ArrowRightOnRectangleIcon,
  ShieldCheckIcon,
  UserCircleIcon,
} from '@heroicons/react/24/outline';
import { ChevronUpDownIcon } from '@heroicons/react/20/solid';
import { useCustomSignOut } from 'hooks/useCustomSignout';

interface WorkspaceAccountMenuProps {
  /** Where the menu opens relative to its button: up from the sidebar foot, down from the mobile bar. */
  placement?: 'up' | 'down';
}

/**
 * The signed-in person's own menu inside a Workspace: account settings,
 * security (password / MFA) and sign out. Workspace pages use
 * WorkspaceLayout, which has no AppShell header, so without this the only way
 * to sign out or reach personal settings was typing /settings/account.
 */
export function WorkspaceAccountMenu({
  placement = 'up',
}: WorkspaceAccountMenuProps) {
  const { t } = useTranslation('common');
  const { data } = useSession();
  // A full page load after sign-out drops every in-memory client cache (SWR
  // Workspace data, the BFF CSRF token), so the next person in this tab
  // starts clean.
  const signOut = useCustomSignOut({ hardRedirect: true });
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const name = data?.user?.name || data?.user?.email || t('account');
  const email = data?.user?.name ? data?.user?.email : null;

  const itemClass =
    'flex w-full items-center gap-x-2 rounded-md px-2 py-1.5 text-left text-sm text-gray-700 hover:bg-gray-100 focus:bg-gray-100 focus:outline-none dark:text-gray-200 dark:hover:bg-gray-800 dark:focus:bg-gray-800';

  return (
    <div
      ref={rootRef}
      className="relative"
      data-testid="workspace-account-menu"
    >
      <button
        ref={buttonRef}
        type="button"
        className="flex w-full items-center gap-x-2 rounded-md px-2 py-2 text-left text-sm hover:bg-gray-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-400 dark:hover:bg-gray-800"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        onClick={() => setOpen((value) => !value)}
      >
        <UserCircleIcon
          className="h-6 w-6 shrink-0 text-gray-500"
          aria-hidden="true"
        />
        <span className="min-w-0 flex-1">
          <span className="block truncate font-medium text-gray-900 dark:text-gray-100">
            {name}
          </span>
          {email && (
            <span className="block truncate text-xs text-gray-500">
              {email}
            </span>
          )}
        </span>
        <ChevronUpDownIcon
          className="h-5 w-5 shrink-0 text-gray-400"
          aria-hidden="true"
        />
      </button>

      {open && (
        <div
          id={menuId}
          role="menu"
          aria-label={t('account')}
          className={`absolute left-0 right-0 z-50 rounded-md border border-gray-200 bg-white p-1 shadow-lg dark:border-gray-700 dark:bg-gray-900 ${
            placement === 'up' ? 'bottom-full mb-2' : 'top-full mt-2'
          }`}
        >
          <Link
            href="/settings/account"
            role="menuitem"
            className={itemClass}
            onClick={() => setOpen(false)}
          >
            <UserCircleIcon className="h-5 w-5" aria-hidden="true" />
            {t('account')}
          </Link>
          <Link
            href="/settings/security"
            role="menuitem"
            className={itemClass}
            onClick={() => setOpen(false)}
          >
            <ShieldCheckIcon className="h-5 w-5" aria-hidden="true" />
            {t('security')}
          </Link>
          <button
            type="button"
            role="menuitem"
            className={itemClass}
            data-testid="workspace-sign-out"
            onClick={() => {
              setOpen(false);
              void signOut();
            }}
          >
            <ArrowRightOnRectangleIcon className="h-5 w-5" aria-hidden="true" />
            {t('logout')}
          </button>
        </div>
      )}
    </div>
  );
}
