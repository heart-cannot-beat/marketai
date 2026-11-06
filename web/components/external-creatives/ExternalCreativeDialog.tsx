import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { PhotoIcon, XMarkIcon } from '@heroicons/react/20/solid';
import { useProductDirectory } from '@/hooks/products/useProductDirectory';
import { ModalScrim } from '@/components/shared/ModalScrim';
import {
  useExternalCreatives,
  type ExternalProblem,
} from '@/hooks/external-creatives/useExternalCreatives';
import {
  MADE_BY,
  MAX_FILES,
  screenFiles,
  type MadeBy,
} from '@/lib/external-creatives/model';
import { TagReviewCard } from './TagReviewCard';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface ExternalCreativeDialogProps {
  open: boolean;
  onClose: () => void;
  /** Called after a confirmation, so the gallery can refresh. */
  onChanged?: () => void;
  /** Preselects the product (the Assets page's product filter). */
  defaultProductId?: string;
  /** Opens straight on the creatives waiting for confirmation (`''` = every product). */
  pendingProductId?: string;
}

function ProblemLine({ problem }: { problem: ExternalProblem }) {
  const { t, i18n } = useTranslation('external');
  const key = `problem-${problem.code}`;
  const text = i18n.exists(`external:${key}`)
    ? t(key)
    : problem.problem
      ? t('problem-generic', { message: errorMessage(t, problem.problem) })
      : t('problem-unknown');
  return (
    <div className="alert alert-error py-2 text-sm" role="alert">
      <span>{text}</span>
    </div>
  );
}

/**
 * "上传外部素材" (Issue #168): pick a product, drop up to ten images,
 * declare who made them and that the Workspace may use them, then review
 * the AI's dimension tags one card at a time and confirm.
 */
export function ExternalCreativeDialog({
  open,
  onClose,
  onChanged,
  defaultProductId,
  pendingProductId,
}: ExternalCreativeDialogProps) {
  const { t } = useTranslation('external');
  const titleId = useId();
  const productFieldId = useId();
  const madeById = useId();
  const noteId = useId();
  const fileInput = useRef<HTMLInputElement>(null);
  const directory = useProductDirectory({ enabled: open });
  const flow = useExternalCreatives();

  const [productId, setProductId] = useState(defaultProductId ?? '');
  const [files, setFiles] = useState<File[]>([]);
  const [refusals, setRefusals] = useState<string[]>([]);
  const [madeBy, setMadeBy] = useState<MadeBy>('self');
  const [permission, setPermission] = useState(false);
  const [note, setNote] = useState('');
  const [dragging, setDragging] = useState(false);

  const { openPending, reset } = flow;
  useEffect(() => {
    if (open && pendingProductId !== undefined) {
      void openPending(pendingProductId || undefined);
    }
    if (!open) reset();
  }, [open, pendingProductId, openPending, reset]);

  useEffect(() => {
    if (!productId && directory.products.length === 1) {
      setProductId(directory.products[0].product_id);
    }
  }, [directory.products, productId]);

  if (!open) return null;

  const busy = flow.phase === 'uploading' || flow.phase === 'submitting';
  const reviewing = flow.phase === 'review';

  function addFiles(list: FileList | File[]) {
    const { accepted, refused } = screenFiles(Array.from(list));
    const messages = refused.map((item) =>
      item.reason === 'type'
        ? t('file-refused-type', { name: item.name })
        : t('file-refused-size', { name: item.name })
    );
    setFiles((previous) => {
      const merged = [...previous, ...accepted];
      if (merged.length > MAX_FILES) messages.push(t('file-refused-count'));
      return merged.slice(0, MAX_FILES);
    });
    setRefusals(messages);
  }

  const canSubmit =
    Boolean(productId) && files.length > 0 && permission && !busy;

  return (
    <ModalScrim labelledBy={titleId}>
      <div className="modal-box max-w-3xl space-y-4">
        <div>
          <h3 id={titleId} className="text-lg font-bold">
            {reviewing ? t('review-title') : t('dialog-title')}
          </h3>
          <p className="mt-1 max-w-prose text-sm text-base-content/70">
            {reviewing ? t('review-intro') : t('dialog-intro')}
          </p>
        </div>

        {flow.problem && <ProblemLine problem={flow.problem} />}

        {!reviewing && (
          <div className="space-y-4">
            <label className="form-control max-w-sm" htmlFor={productFieldId}>
              <span className="label-text text-sm">{t('product-label')}</span>
              <select
                id={productFieldId}
                className="select select-bordered select-sm"
                value={productId}
                disabled={busy}
                onChange={(event) => setProductId(event.target.value)}
              >
                <option value="">{t('product-placeholder')}</option>
                {directory.products.map((product) => (
                  <option key={product.product_id} value={product.product_id}>
                    {product.name || product.product_id}
                  </option>
                ))}
              </select>
            </label>
            {!directory.isLoading && directory.products.length === 0 && (
              <p className="text-sm text-base-content/70">
                {t('product-none')}
              </p>
            )}

            <div
              className={classNames(
                'flex flex-col items-center gap-2 rounded-lg border-2 border-dashed px-4 py-8 text-center transition-colors',
                dragging ? 'border-primary bg-primary/5' : 'border-base-300'
              )}
              onDragOver={(event) => {
                event.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(event) => {
                event.preventDefault();
                setDragging(false);
                if (!busy) addFiles(event.dataTransfer.files);
              }}
              data-testid="external-dropzone"
            >
              <PhotoIcon
                className="h-8 w-8 text-base-content/40"
                aria-hidden="true"
              />
              <p className="text-sm font-medium">{t('drop-title')}</p>
              <p className="text-xs text-base-content/60">{t('drop-hint')}</p>
              <input
                ref={fileInput}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                multiple
                className="hidden"
                data-testid="external-files"
                onChange={(event) => {
                  if (event.target.files) addFiles(event.target.files);
                  event.target.value = '';
                }}
              />
              <button
                type="button"
                className="btn btn-sm"
                disabled={busy}
                onClick={() => fileInput.current?.click()}
              >
                {t('drop-choose')}
              </button>
            </div>
            {refusals.length > 0 && (
              <ul className="space-y-0.5 text-sm text-warning" role="alert">
                {refusals.map((message) => (
                  <li key={message}>{message}</li>
                ))}
              </ul>
            )}
            {files.length > 0 && (
              <div className="space-y-1">
                <p className="text-sm">
                  {t('files-count', { count: files.length })}
                </p>
                <ul className="flex flex-wrap gap-1.5">
                  {files.map((file, index) => (
                    <li
                      key={`${file.name}-${index}`}
                      className="badge badge-ghost gap-1 py-3"
                    >
                      <span className="max-w-[12rem] truncate">
                        {file.name}
                      </span>
                      <button
                        type="button"
                        aria-label={t('file-remove', { name: file.name })}
                        disabled={busy}
                        onClick={() =>
                          setFiles((previous) =>
                            previous.filter((_, i) => i !== index)
                          )
                        }
                      >
                        <XMarkIcon className="h-3.5 w-3.5" aria-hidden="true" />
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div className="flex flex-wrap items-end gap-3">
              <label className="form-control w-48" htmlFor={madeById}>
                <span className="label-text text-sm">{t('made-by-label')}</span>
                <select
                  id={madeById}
                  className="select select-bordered select-sm"
                  value={madeBy}
                  disabled={busy}
                  onChange={(event) => setMadeBy(event.target.value as MadeBy)}
                >
                  {MADE_BY.map((value) => (
                    <option key={value} value={value}>
                      {t(`made-by-${value}`)}
                    </option>
                  ))}
                </select>
              </label>
              <label
                className="form-control min-w-[14rem] flex-1"
                htmlFor={noteId}
              >
                <span className="label-text text-sm">{t('note-label')}</span>
                <input
                  id={noteId}
                  className="input input-bordered input-sm"
                  placeholder={t('note-placeholder')}
                  maxLength={500}
                  value={note}
                  disabled={busy}
                  onChange={(event) => setNote(event.target.value)}
                />
              </label>
            </div>
            <label className="flex cursor-pointer items-start gap-2 text-sm">
              <input
                type="checkbox"
                className="checkbox checkbox-sm mt-0.5"
                checked={permission}
                disabled={busy}
                onChange={(event) => setPermission(event.target.checked)}
              />
              <span>{t('rights-label')}</span>
            </label>
            {busy && (
              <p className="flex items-center gap-2 text-sm" role="status">
                <span
                  className="loading loading-spinner loading-xs"
                  aria-hidden="true"
                />
                {flow.phase === 'uploading' && flow.uploadProgress
                  ? t('uploading', flow.uploadProgress)
                  : t('submitting')}
              </p>
            )}
          </div>
        )}

        {reviewing && (
          <div className="max-h-[60vh] space-y-3 overflow-y-auto pr-1">
            {flow.creatives.map((creative) => (
              <TagReviewCard
                key={creative.external_creative_id}
                creative={creative}
                confirming={flow.confirming === creative.external_creative_id}
                onConfirm={async (id, body) => {
                  const confirmed = await flow.confirm(id, body);
                  if (confirmed) onChanged?.();
                }}
              />
            ))}
          </div>
        )}

        <div className="modal-action">
          {!reviewing && (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={!canSubmit}
              onClick={() =>
                void flow.submit({
                  productId,
                  files,
                  madeBy,
                  permissionConfirmed: permission,
                  note,
                })
              }
            >
              {t('submit')}
            </button>
          )}
          {reviewing && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => {
                setFiles([]);
                setRefusals([]);
                flow.reset();
              }}
            >
              {t('upload-more')}
            </button>
          )}
          <button
            type="button"
            className="btn btn-sm"
            onClick={onClose}
            disabled={busy}
          >
            {reviewing ? t('close') : t('cancel')}
          </button>
        </div>
      </div>
    </ModalScrim>
  );
}
