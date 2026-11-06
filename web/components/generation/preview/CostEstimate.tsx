import { useTranslation } from 'next-i18next';
import { CostDisplay } from '@/components/generation/shared/CostDisplay';

interface MaximumCost {
  amount: string;
  currency: string;
}

interface CostEstimateProps {
  /** `GenerationPreviewDetail.maximum_cost` — null when the domain has not
   * priced the preview yet. */
  maximumCost: MaximumCost | null;
}

export function CostEstimate({ maximumCost }: CostEstimateProps) {
  const { t } = useTranslation('common');

  return (
    <div className="flex items-center gap-2 text-sm">
      <span className="font-medium">{t('generation-preview-cost-label')}</span>
      {maximumCost ? (
        <CostDisplay
          amount={maximumCost.amount}
          currency={maximumCost.currency}
        />
      ) : (
        <span className="text-base-content/60">
          {t('generation-preview-cost-unavailable')}
        </span>
      )}
    </div>
  );
}
