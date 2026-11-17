import { useId } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type Understanding = components['schemas']['Understanding'];

export const PRODUCT_PANEL_ID = 'ideas-product-panel';

/**
 * "What the AI read about the product" beside the ad ideas (ADR 0018,
 * design §5.3, #196): each fact with where it was read, marked "not checked
 * yet" until the user checks it. Read only here; checking, editing and
 * deleting facts come with the product profile and the check before
 * downloading.
 */
export function ProductPanel({
  understanding,
}: {
  understanding: Understanding | null;
}) {
  const { t } = useTranslation('ideas');
  const title = useId();
  const facts = understanding?.facts ?? [];
  return (
    <aside
      id={PRODUCT_PANEL_ID}
      aria-labelledby={title}
      className="scroll-mt-4 space-y-4 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900 sm:p-5"
    >
      <div>
        <h2 id={title} className="text-base font-semibold">
          {t('ideas-panel-title')}
        </h2>
        {understanding?.product_name && (
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {understanding.product_name}
          </p>
        )}
      </div>
      <section className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-600 dark:text-gray-400">
          {t('ideas-panel-facts')}
        </h3>
        {facts.length === 0 ? (
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t('ideas-panel-no-facts')}
          </p>
        ) : (
          <ul role="list" className="space-y-3">
            {facts.map((fact) => {
              const checked = fact.confirmation === 'confirmed';
              return (
                <li key={fact.id} className="space-y-1">
                  <p className="text-sm">{fact.statement}</p>
                  <p
                    className={classNames(
                      'inline-block rounded-full px-2 py-px text-xs font-semibold',
                      checked
                        ? 'bg-emerald-100 text-emerald-900'
                        : 'bg-amber-100 text-amber-900'
                    )}
                  >
                    {checked
                      ? t('ideas-panel-fact-checked')
                      : t('ideas-panel-fact-unchecked')}
                  </p>
                  {fact.source_quote && (
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      {t('ideas-panel-fact-source', {
                        quote: fact.source_quote,
                      })}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>
      {facts.some((fact) => fact.confirmation !== 'confirmed') && (
        <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
          {t('ideas-panel-check-note')}
        </p>
      )}
    </aside>
  );
}
