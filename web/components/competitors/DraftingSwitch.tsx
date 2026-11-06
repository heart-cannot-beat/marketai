import { useTranslation } from 'next-i18next';
import useSWR from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { dataOf, problemMessage, useCommand } from './shared';

/**
 * The product's "Use the competitor snapshot when drafting" switch (spec
 * §7.2; on unless a member turned it off). When it is off, a new draft cites
 * no snapshot; plans drafted before keep the one they cite.
 */
export function DraftingSwitch({ productId }: { productId: string }) {
  const { t } = useTranslation('competitors');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const command = useCommand();
  const settings = useSWR(
    ['competitor-settings', current.workspaceId, productId],
    async () =>
      dataOf(
        await gateway.getProductCompetitorSettings({
          workspaceSlug: current.slug,
          productId,
        })
      )
  );
  const on = settings.data?.competitor_snapshot_in_drafts;

  async function toggle(next: boolean) {
    const updated = await command.run(
      `drafting:${productId}:${next}`,
      async (action) =>
        dataOf(
          await gateway.updateProductCompetitorSettings({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            productId,
            body: { competitor_snapshot_in_drafts: next },
          })
        )
    );
    if (updated) await settings.mutate(updated, { revalidate: false });
  }

  return (
    <div
      className="space-y-1 border-t border-gray-200 pt-3"
      data-testid="competitor-drafting-switch"
      data-on={on === undefined ? '' : String(on)}
    >
      <div className="flex items-start justify-between gap-3">
        <span className="min-w-0">
          <span
            id="competitor-drafting-label"
            className="block text-sm font-medium"
          >
            {t('summary-drafting-label')}
          </span>
          <span className="block text-xs text-gray-600">
            {on === undefined
              ? t('summary-drafting-loading')
              : on
                ? t('summary-drafting-on')
                : t('summary-drafting-off')}
          </span>
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={on ?? false}
          aria-labelledby="competitor-drafting-label"
          disabled={on === undefined || !!command.busy}
          onClick={() => void toggle(!on)}
          className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition disabled:opacity-40 ${
            on ? 'bg-gray-900' : 'bg-gray-300'
          }`}
          data-testid="competitor-drafting-toggle"
        >
          <span
            className={`inline-block h-5 w-5 rounded-full bg-white shadow transition ${
              on ? 'translate-x-5' : 'translate-x-0.5'
            }`}
            aria-hidden="true"
          />
        </button>
      </div>
      {settings.error && (
        <p className="text-xs text-red-700" role="alert">
          {problemMessage(t, settings.error)}
        </p>
      )}
      {command.errorFor('drafting:') && (
        <p className="text-xs text-red-700" role="alert">
          {command.errorFor('drafting:')}
        </p>
      )}
    </div>
  );
}
