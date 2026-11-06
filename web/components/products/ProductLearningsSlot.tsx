import { ProductLearningsSummary } from '@/components/learnings/ProductLearningsSummary';

/**
 * Mount point for "这个产品我们已经学到了什么" (front-flow contract §3.3) on
 * the product home card. Issue #166 fills it: the strongest conclusion (or
 * "尚无投放数据") and a button that opens the full learnings card, with the
 * results import and "按结论生成下一轮". Not mounted since the legacy flow was
 * frozen (#337); kept until that flow is deleted.
 */
export interface ProductLearningsSlotProps {
  productId: string;
  /** Display name, for headings and accessible labels (never the id). */
  productName: string;
}

export function ProductLearningsSlot({
  productId,
  productName,
}: ProductLearningsSlotProps) {
  return (
    <ProductLearningsSummary productId={productId} productName={productName} />
  );
}
