import { useEffect, useRef } from 'react';
import { useTranslation } from 'next-i18next';

interface ForbiddenStateProps {
  context?: 'no-membership' | 'owner-only';
  traceId?: string;
}

export function ForbiddenState({ context, traceId }: ForbiddenStateProps) {
  const { t } = useTranslation('common');
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  const message =
    context === 'owner-only'
      ? t('workspace-owner-only')
      : context === 'no-membership'
        ? t('workspace-no-membership')
        : t('workspace-no-resource-access');

  return (
    <div className="flex flex-col items-center justify-center py-16">
      <h1
        ref={headingRef}
        tabIndex={-1}
        className="text-lg font-semibold outline-none"
      >
        {t('workspace-access-denied')}
      </h1>
      <p className="mt-2 text-sm text-gray-600">{message}</p>
      {traceId && (
        <p className="mt-4 font-mono text-xs text-gray-400">
          {t('workspace-trace', { traceId })}
        </p>
      )}
    </div>
  );
}
