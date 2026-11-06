import { useId, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { ModalScrim } from '@/components/shared/ModalScrim';
import type { ProductLearnings } from '@/lib/learnings/format';
import { LearningsCard } from './LearningsCard';

/**
 * The learnings line on a product card (front-flow §3.3): the strongest
 * conclusion, "尚无投放数据", or "样本不足"; one button opens the full
 * 「这个产品我们已经学到了什么」 card (with the import flow and
 * "按结论生成下一轮") in a dialog. Shares the SWR cache with the card.
 */
export function ProductLearningsSummary({
  productId,
  productName,
}: {
  productId: string;
  productName: string;
}) {
  const { t } = useTranslation('learnings');
  const [open, setOpen] = useState(false);
  const titleId = useId();
  const query = useDomainQuery('get_product_learnings', {
    product_id: productId,
  });
  const learnings = query.data as ProductLearnings | undefined;
  const highlight = learnings?.highlights?.[0];

  let line: string | null = null;
  if (learnings?.status === 'no_data') line = t('summary-no-data');
  else if (learnings && highlight) line = highlight;
  else if (learnings) line = t('summary-insufficient');

  return (
    <div className="mt-1 space-y-1" data-testid="product-learnings">
      {line && (
        <p className="line-clamp-3 text-sm text-base-content/80">
          {learnings?.evidence_mode &&
            learnings.evidence_mode !== 'production' && (
              <span className="badge badge-warning badge-sm mr-1">
                {t(`mode-${learnings.evidence_mode}`)}
              </span>
            )}
          {line}
        </p>
      )}
      <button
        type="button"
        className="btn btn-ghost btn-xs -ml-2 text-primary"
        onClick={() => setOpen(true)}
        aria-label={t('view-learnings-for', { name: productName })}
      >
        {t('view-learnings')}
      </button>
      {open && (
        <ModalScrim labelledBy={titleId}>
          <div className="modal-box max-w-4xl p-0">
            <h3 id={titleId} className="sr-only">
              {t('view-learnings-for', { name: productName })}
            </h3>
            <LearningsCard productId={productId} />
            <div className="modal-action px-5 pb-5">
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => setOpen(false)}
              >
                {t('close')}
              </button>
            </div>
          </div>
        </ModalScrim>
      )}
    </div>
  );
}
