import { useEffect, useState, type CSSProperties, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

/**
 * Pins the scrim to the whole viewport whatever the page around it does.
 * Inline, so neither a parent utility nor a later stylesheet can move it:
 * rendered inside a `space-y-*` column, daisyUI's `.modal` used to pick up
 * that utility's `margin-top` (a more specific selector than daisyUI's
 * `margin: 0`) and leave a ~24px strip at the top of the page uncovered
 * (Issue #193). The padding keeps the dialog box clear of notches and home
 * indicators while the scrim itself still covers the safe areas.
 */
export const MODAL_SCRIM_STYLE: CSSProperties = {
  position: 'fixed',
  top: 0,
  right: 0,
  bottom: 0,
  left: 0,
  margin: 0,
  paddingTop: 'env(safe-area-inset-top)',
  paddingRight: 'env(safe-area-inset-right)',
  paddingBottom: 'env(safe-area-inset-bottom)',
  paddingLeft: 'env(safe-area-inset-left)',
};

interface ModalScrimProps {
  /** Id of the dialog's heading. */
  labelledBy: string;
  /** The `.modal-box` (and anything else the dialog shows). */
  children: ReactNode;
  /** Called on a click on the scrim itself, outside the box. */
  onScrimClick?: () => void;
}

/**
 * The full-viewport scrim the front-flow dialogs sit on (test plan run
 * confirmation, results import, learnings, external creatives). It renders
 * into `document.body` so no ancestor -- spacing utilities, a transformed
 * or sticky container -- can offset or clip it, and it is a `role="dialog"`
 * with `aria-modal` labelled by the dialog's heading.
 */
export function ModalScrim({
  labelledBy,
  children,
  onScrimClick,
}: ModalScrimProps) {
  // Portals need `document`: render nothing on the server and on the first
  // client pass, so hydration never sees a node the server did not render.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  if (!mounted) return null;

  return createPortal(
    <div
      className="modal modal-open"
      role="dialog"
      aria-modal="true"
      aria-labelledby={labelledBy}
      style={MODAL_SCRIM_STYLE}
      data-testid="modal-scrim"
      onClick={
        onScrimClick
          ? (event) => {
              if (event.target === event.currentTarget) onScrimClick();
            }
          : undefined
      }
    >
      {children}
    </div>,
    document.body
  );
}
