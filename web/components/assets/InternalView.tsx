import { useState, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';

interface InternalViewProps {
  /** Rendered only while the view is open, so the internal ids and raw
   * data inside never reach the page text otherwise. */
  children: () => ReactNode;
  testId?: string;
}

/**
 * The collapsed「内部视图」of the asset screens (front-flow contract §1
 * principle 2, Issue #191): raw ids, hashes and evidence for whoever has to
 * debug, hidden by default -- the same toggle the test page offers.
 */
export function InternalView({ children, testId }: InternalViewProps) {
  const { t } = useTranslation('assets');
  const [open, setOpen] = useState(false);
  return (
    <div className="flex flex-col gap-1" data-testid={testId}>
      <button
        type="button"
        className="btn btn-ghost btn-xs self-start text-base-content/60"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? t('internal-toggle-hide') : t('internal-toggle-show')}
      </button>
      {open && (
        <div className="flex flex-col gap-1">
          <p className="text-xs text-base-content/50">{t('internal-intro')}</p>
          {children()}
        </div>
      )}
    </div>
  );
}
