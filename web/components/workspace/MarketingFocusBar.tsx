import { useRef } from 'react';
import { useTranslation } from 'next-i18next';
import { ArrowDownIcon } from '@heroicons/react/24/outline';
import { ProductThumbnail } from '@/components/products/ProductThumbnail';

export const MAIN_BUTTON =
  'rounded-lg bg-gray-900 px-5 py-2.5 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-40';
const JUMP_BUTTON =
  'rounded-lg border border-gray-900 px-4 py-2 text-sm font-medium text-gray-900 disabled:opacity-40';

export interface FocusAction {
  label: string;
  run: () => void;
  disabled?: boolean;
  /** Takes the user to the section that commits the step instead of committing it here. */
  jump?: boolean;
}

export interface MoreAction {
  label: string;
  run: () => void;
  disabled?: boolean;
}

/**
 * The test page's fixed top area (J-32): what the test is for, what this
 * round asks, what is missing now and the one thing to do next. Every other
 * page-level action sits under "More".
 */
export function MarketingFocusBar({
  productName,
  photoVersionId,
  goal,
  question,
  gap,
  waiting,
  action,
  more,
}: {
  productName: string;
  photoVersionId?: string | null;
  goal: string | null;
  question: string | null;
  gap: string;
  waiting?: boolean;
  action?: FocusAction;
  more: MoreAction[];
}) {
  const { t } = useTranslation('common');
  const menu = useRef<HTMLDetailsElement>(null);
  const items = more.filter(Boolean);
  return (
    <section
      aria-label={t('marketing-focus-label')}
      data-testid="marketing-focus"
      className="sticky top-[4.25rem] z-30 -mx-4 border-b border-gray-200 bg-white px-4 py-3 shadow-sm sm:-mx-6 sm:px-6 lg:top-0 lg:-mx-8 lg:px-8"
    >
      <div className="flex items-start gap-3">
        <ProductThumbnail
          referenceVersionId={photoVersionId}
          name={productName}
          className="h-10 w-10 shrink-0 rounded-md border border-gray-200"
        />
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-sm font-semibold text-gray-900 outline-none">
            {productName}
          </h1>
          <dl className="mt-1 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
            {(
              [
                ['marketing-focus-goal', goal, 'marketing-focus-goal-pending'],
                [
                  'marketing-focus-question',
                  question,
                  'marketing-focus-question-pending',
                ],
              ] as const
            ).map(([label, value, pending]) => (
              // On a phone label and value share one line to keep the area short.
              <div key={label} className="flex min-w-0 gap-1.5 sm:block">
                <dt className="shrink-0 text-xs font-medium uppercase leading-5 tracking-wide text-gray-500">
                  {t(label)}
                </dt>
                <dd className="line-clamp-2 min-w-0 break-words text-gray-800">
                  {value || t(pending)}
                </dd>
              </div>
            ))}
          </dl>
        </div>
        {items.length > 0 && (
          <details ref={menu} className="relative shrink-0">
            <summary className="cursor-pointer list-none rounded-lg border border-gray-300 px-3 py-1.5 text-sm [&::-webkit-details-marker]:hidden">
              {t('marketing-focus-more')}
            </summary>
            <ul className="absolute right-0 z-40 mt-1 w-64 max-w-[calc(100vw-2rem)] rounded-lg border border-gray-200 bg-white p-1 shadow-lg">
              {items.map((item) => (
                <li key={item.label}>
                  <button
                    type="button"
                    className="block w-full rounded px-3 py-2 text-left text-sm hover:bg-gray-50 disabled:opacity-40"
                    disabled={item.disabled}
                    onClick={() => {
                      if (menu.current) menu.current.open = false;
                      item.run();
                    }}
                  >
                    {item.label}
                  </button>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
      <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
        <p
          role="status"
          aria-busy={waiting || undefined}
          className="min-w-0 flex-1 text-sm text-gray-800"
        >
          <span className="font-medium">{t('marketing-focus-now')}</span> {gap}
        </p>
        {action && (
          <button
            type="button"
            className={`${action.jump ? JUMP_BUTTON : MAIN_BUTTON} w-full sm:w-auto`}
            disabled={action.disabled}
            onClick={action.run}
          >
            <span className="inline-flex items-center justify-center gap-1.5">
              {action.label}
              {action.jump && (
                <ArrowDownIcon className="h-4 w-4" aria-hidden="true" />
              )}
            </span>
          </button>
        )}
      </div>
    </section>
  );
}
