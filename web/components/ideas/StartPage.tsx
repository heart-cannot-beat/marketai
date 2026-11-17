import { useTranslation } from 'next-i18next';
import { StartForm } from './StartForm';

/**
 * The examples under the start form (design §5.1): a made-up Acme Charge
 * product's three claims and six ads, drawn for this page and marked as
 * examples. They show what the first batch gives before anyone has a key;
 * they are never presented as the user's own ads.
 */
export const EXAMPLES = [
  {
    claim: 'start-example-claim-1',
    ads: ['/examples/acme-charge/ad-1.svg', '/examples/acme-charge/ad-2.svg'],
  },
  {
    claim: 'start-example-claim-2',
    ads: ['/examples/acme-charge/ad-3.svg', '/examples/acme-charge/ad-4.svg'],
  },
  {
    claim: 'start-example-claim-3',
    ads: ['/examples/acme-charge/ad-5.svg', '/examples/acme-charge/ad-6.svg'],
  },
] as const;

/**
 * The start page (ADR 0018, design §5.1, #195): paste a product link and see
 * what its ads could look like. One button starts reading the product,
 * drafting claims and making the first ads; it says the most that can cost.
 */
export function StartPage() {
  const { t } = useTranslation('ideas');
  return (
    <div className="mx-auto max-w-3xl space-y-10">
      <div className="space-y-6">
        <div className="space-y-2">
          <h1 className="text-2xl font-bold sm:text-3xl">{t('start-title')}</h1>
          <p className="text-sm text-base-content/70">{t('start-intro')}</p>
        </div>
        <StartForm label={t('start-paste-label')} autoFocus />
      </div>
      <Examples />
    </div>
  );
}

function Examples() {
  const { t } = useTranslation('ideas');
  return (
    <section
      aria-labelledby="start-examples-title"
      className="space-y-4"
      data-testid="start-examples"
    >
      <div className="space-y-1">
        <h2 id="start-examples-title" className="text-lg font-bold">
          {t('start-examples-title')}
        </h2>
        <p className="text-sm text-base-content/70">{t('start-examples-note')}</p>
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        {EXAMPLES.map((group) => (
          <figure key={group.claim} className="space-y-2">
            <figcaption className="text-sm font-semibold">
              {t(group.claim)}
            </figcaption>
            <div className="grid grid-cols-2 gap-2">
              {group.ads.map((src) => (
                <div
                  key={src}
                  className="relative overflow-hidden rounded-lg border border-base-300"
                >
                  {/* Static files drawn for this page; next/image adds nothing here. */}
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={src}
                    alt={t('start-example-alt', { claim: t(group.claim) })}
                    className="aspect-[4/5] w-full object-cover"
                    loading="lazy"
                  />
                  <span className="absolute left-1.5 top-1.5 rounded bg-black/70 px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-white">
                    {t('start-example-badge')}
                  </span>
                </div>
              ))}
            </div>
          </figure>
        ))}
      </div>
    </section>
  );
}
