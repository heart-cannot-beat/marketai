import { useState, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { ArrowUpTrayIcon } from '@heroicons/react/20/solid';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { ExternalCreativeDialog } from './ExternalCreativeDialog';

/** Gallery source: every asset, generated ones, or external uploads. */
export type AssetSource = 'all' | 'generated' | 'external';

const ORIGIN_FOR_SOURCE: Record<AssetSource, string | undefined> = {
  all: undefined,
  generated: 'generation',
  external: 'upload',
};

export function sourceOfOrigin(originKind: string | undefined): AssetSource {
  if (originKind === 'upload') return 'external';
  if (originKind === 'generation') return 'generated';
  return 'all';
}

interface ExternalCreativesToolbarProps {
  /** The gallery's current `origin_kind` filter. */
  originKind: string | undefined;
  onOriginKindChange: (originKind: string | undefined) => void;
  /** The gallery's product filter, preselected in the upload dialog. */
  productId?: string;
  /** After a confirmation, so the gallery shows the newly approved images. */
  onChanged?: () => void;
  /**
   * Whether the 全部 / 生成 / 外部 switch applies. The page hides it while a
   * Run is selected (flat or grouped by direction): an external creative
   * belongs to no Run, so the switch could only empty the view.
   */
  showSourceSwitch?: boolean;
  /** Page controls that share the row (#170's 按方向 / 全部素材 toggle). */
  children?: ReactNode;
}

/**
 * The Assets page's entry to external creatives (Issue #168): a 生成 / 外部
 * source switch over the gallery's `origin_kind` filter, the
 * "上传外部素材" button, and -- when uploads are waiting -- a way back to
 * the tag review.
 */
export function ExternalCreativesToolbar({
  originKind,
  onOriginKindChange,
  productId,
  onChanged,
  showSourceSwitch = true,
  children,
}: ExternalCreativesToolbarProps) {
  const { t } = useTranslation('external');
  const [dialog, setDialog] = useState<'closed' | 'upload' | 'pending'>(
    'closed'
  );
  const [changed, setChanged] = useState(false);
  const pending = useDomainQuery('list_external_creatives', {
    status: 'ready',
    product_id: productId,
  });
  const waiting = (
    (pending.data as { items?: unknown[] } | undefined)?.items ?? []
  ).length;
  const source = sourceOfOrigin(originKind);

  function close() {
    setDialog('closed');
    void pending.refresh();
    if (changed) onChanged?.();
    setChanged(false);
  }

  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-base-300 px-4 py-2">
      {showSourceSwitch && (
        <div role="radiogroup" aria-label={t('source-label')} className="join">
          {(['all', 'generated', 'external'] as AssetSource[]).map((value) => (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={source === value}
              className={classNames(
                'btn join-item btn-xs',
                source === value ? 'btn-active btn-neutral' : 'btn-ghost'
              )}
              onClick={() => onOriginKindChange(ORIGIN_FOR_SOURCE[value])}
            >
              {t(`source-${value}`)}
            </button>
          ))}
        </div>
      )}
      <div className="ml-auto flex flex-wrap items-center gap-2">
        {children}
        {waiting > 0 && (
          <button
            type="button"
            className="btn btn-ghost btn-xs text-warning"
            onClick={() => setDialog('pending')}
          >
            {t('pending-button', { count: waiting })}
          </button>
        )}
        <button
          type="button"
          className="btn btn-primary btn-sm"
          onClick={() => setDialog('upload')}
        >
          <ArrowUpTrayIcon className="h-4 w-4" aria-hidden="true" />
          {t('upload-button')}
        </button>
      </div>
      <ExternalCreativeDialog
        open={dialog !== 'closed'}
        onClose={close}
        onChanged={() => setChanged(true)}
        defaultProductId={productId}
        pendingProductId={dialog === 'pending' ? (productId ?? '') : undefined}
      />
    </div>
  );
}
