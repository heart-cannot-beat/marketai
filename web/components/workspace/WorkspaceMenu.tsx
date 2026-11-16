import { useCallback, useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';
import { Bars3Icon, XMarkIcon } from '@heroicons/react/24/outline';
import Brand from '@/components/shared/shell/Brand';
import { WorkspaceSelector } from './WorkspaceSelector';
import { WorkspaceNavigation } from './WorkspaceNavigation';

const FOCUSABLE =
  'a[href], button:not([disabled]), select:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
/** Tailwind's `lg`: from here on the sidebar is always shown. */
const DESKTOP_QUERY = '(min-width: 1024px)';

/**
 * The phone menu (design §5.8): below `lg` the sidebar folds into a menu
 * button in the top bar, and opens as a modal panel with the same content.
 * Following a link, Escape, the close button or a tap outside closes it.
 */
export function WorkspaceMenu({ slug }: { slug: string }) {
  const { t } = useTranslation('common');
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const openerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  // Focus goes back to the menu button only when the viewer dismissed the
  // menu; after following a link the layout focuses the new page's heading.
  const restoreFocus = useRef(false);

  const dismiss = useCallback(() => {
    restoreFocus.current = true;
    setOpen(false);
  }, []);

  useEffect(() => {
    setOpen(false);
  }, [router.asPath]);

  useEffect(() => {
    if (!open) {
      if (restoreFocus.current) openerRef.current?.focus();
      restoreFocus.current = false;
      return;
    }
    closeRef.current?.focus();
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = overflow;
    };
  }, [open]);

  // Widening the window past `lg` hides the panel; close it so the page
  // scrolls again.
  useEffect(() => {
    if (!open || typeof window.matchMedia !== 'function') return;
    const desktop = window.matchMedia(DESKTOP_QUERY);
    const onChange = () => {
      if (desktop.matches) setOpen(false);
    };
    desktop.addEventListener('change', onChange);
    return () => desktop.removeEventListener('change', onChange);
  }, [open]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      dismiss();
      return;
    }
    if (event.key !== 'Tab' || !panelRef.current) return;
    const items = Array.from(
      panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE)
    );
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <>
      <button
        ref={openerRef}
        type="button"
        className="btn btn-ghost btn-square btn-sm"
        aria-label={t('workspace-menu-open')}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls="workspace-menu"
        data-testid="workspace-menu-open"
        onClick={() => setOpen(true)}
      >
        <Bars3Icon className="h-6 w-6" aria-hidden="true" />
      </button>
      {/* A portal: the top bar is its own stacking context, so the panel
          would otherwise sit under anything on the page above `z-40`. */}
      {open &&
        createPortal(
          <div className="fixed inset-0 z-50 lg:hidden">
            <div
              className="fixed inset-0 bg-gray-600/80"
              aria-hidden="true"
              data-testid="workspace-menu-backdrop"
              onClick={dismiss}
            />
            <div
              id="workspace-menu"
              ref={panelRef}
              role="dialog"
              aria-modal="true"
              aria-label={t('workspace-menu-label')}
              data-testid="workspace-menu"
              onKeyDown={onKeyDown}
              className="fixed inset-y-0 left-0 flex w-full max-w-xs flex-col gap-y-3 overflow-y-auto bg-white px-4 pb-6 dark:bg-black"
            >
              <div className="flex items-center justify-between pt-3 [&>div]:pt-0">
                <Brand />
                <button
                  ref={closeRef}
                  type="button"
                  className="btn btn-ghost btn-square btn-sm"
                  aria-label={t('workspace-menu-close')}
                  data-testid="workspace-menu-close"
                  onClick={dismiss}
                >
                  <XMarkIcon className="h-6 w-6" aria-hidden="true" />
                </button>
              </div>
              <WorkspaceSelector id="workspace-menu-selector" />
              <nav className="flex flex-1 flex-col">
                <WorkspaceNavigation
                  slug={slug}
                  activePathname={router.asPath}
                  instance="menu"
                />
              </nav>
            </div>
          </div>,
          document.body
        )}
    </>
  );
}
