import { useState, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';

/**
 * Stable identifiers, hashes and model settings stay out of the page's text
 * (J-32); they live in a folded "Technical details" panel, each value with a
 * copy button for support and audit work.
 */
export function CopyValue({ label, value }: { label: string; value: string }) {
  const { t } = useTranslation('common');
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="rounded border border-gray-300 px-2 py-0.5 text-xs"
      aria-label={t('marketing-copy-value', { label })}
      onClick={() => {
        void navigator.clipboard
          ?.writeText(value)
          .then(() => setCopied(true))
          .catch(() => setCopied(false));
      }}
    >
      {copied ? t('marketing-copied') : t('marketing-copy')}
    </button>
  );
}

export function TechnicalFacts({
  facts,
}: {
  facts: [string, string | null | undefined][];
}) {
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-x-3 gap-y-2">
      {facts
        .filter((fact): fact is [string, string] => !!fact[1])
        .map(([term, value]) => (
          <div key={term} className="contents">
            <dt className="text-gray-600">{term}</dt>
            <dd className="flex min-w-0 items-start justify-between gap-2">
              <span className="min-w-0 break-all font-mono text-xs">
                {value}
              </span>
              <CopyValue label={term} value={value} />
            </dd>
          </div>
        ))}
    </dl>
  );
}

export function TechnicalDetails({
  facts,
  children,
  className = 'text-sm',
  onToggle,
}: {
  facts: [string, string | null | undefined][];
  children?: ReactNode;
  className?: string;
  onToggle?: (open: boolean) => void;
}) {
  const { t } = useTranslation('common');
  if (!facts.some(([, value]) => value) && !children) return null;
  return (
    <details
      className={className}
      onToggle={(e) => onToggle?.(e.currentTarget.open)}
    >
      <summary className="cursor-pointer text-gray-600">
        {t('marketing-technical-details')}
      </summary>
      <div className="mt-2 space-y-3">
        <TechnicalFacts facts={facts} />
        {children}
      </div>
    </details>
  );
}
