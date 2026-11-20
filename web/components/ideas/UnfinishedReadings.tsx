import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import type { UnfinishedFirstBatch } from '@/hooks/ideas/useUnfinishedFirstBatches';
import { firstBatchHref } from '@/lib/workspace-navigator';

/** The link as the user pasted it, without the scheme; `null` for pasted text or images. */
export function pastedLink(url: string | null | undefined): string | null {
  if (!url) return null;
  return url.replace(/^https?:\/\//i, '').replace(/\/$/, '');
}

/**
 * Products still being read, or stopped before they could be made (#204):
 * each links back to its reading page, the only place that shows it until
 * the product exists. Shown on the overview and on the start page.
 */
export function UnfinishedReadings({
  batches,
}: {
  batches: UnfinishedFirstBatch[];
}) {
  const { t } = useTranslation('common');
  const { current } = useWorkspace();
  if (batches.length === 0) return null;
  return (
    <section
      aria-labelledby="unfinished-readings-title"
      className="space-y-3"
      data-testid="unfinished-readings"
    >
      <h2 id="unfinished-readings-title" className="text-lg font-bold">
        {t('unfinished-readings-title')}
      </h2>
      <ul role="list" className="space-y-3">
        {batches.map((batch) => {
          const link = pastedLink(batch.source_url);
          return (
            <li
              key={batch.id}
              data-testid="unfinished-reading"
              data-state={batch.state}
              className="flex flex-col gap-3 rounded-xl border border-base-300 bg-base-100 p-4 sm:flex-row sm:items-center sm:p-5"
            >
              <div className="min-w-0 flex-1 space-y-1">
                <p className="break-words font-semibold">
                  {link ?? t('unfinished-reading-pasted')}
                </p>
                <p className="text-sm text-base-content/80">
                  {batch.needs_attention
                    ? t('unfinished-reading-stopped')
                    : t('unfinished-reading-running')}
                </p>
              </div>
              <GuardedLink
                href={firstBatchHref(current.slug, batch.id)}
                className={
                  batch.needs_attention
                    ? 'btn btn-primary w-full sm:w-auto'
                    : 'btn btn-outline w-full sm:w-auto'
                }
              >
                {batch.needs_attention
                  ? t('unfinished-reading-action-stopped')
                  : t('unfinished-reading-action')}
              </GuardedLink>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
