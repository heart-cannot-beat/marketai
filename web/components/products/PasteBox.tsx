import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
} from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { PhotoIcon, XMarkIcon } from '@heroicons/react/24/outline';
import {
  ACCEPTED_IMAGE_TYPES,
  acceptImages,
  linkHost,
  splitPaste,
  type ImageRejection,
} from '@/lib/products/intake-model';

export interface PasteBoxProps {
  id: string;
  label: string;
  value: string;
  onValueChange: (value: string) => void;
  files: File[];
  onFilesChange: (files: File[]) => void;
  onSubmit: () => void;
  submitLabel: string;
  busy?: boolean;
  busyLabel?: string;
  /** Smaller variant used inside the failure panel. */
  compact?: boolean;
  autoFocus?: boolean;
}

function FileChip({ file, onRemove }: { file: File; onRemove: () => void }) {
  const { t } = useTranslation('products');
  const [preview, setPreview] = useState<string | null>(null);
  useEffect(() => {
    if (typeof URL.createObjectURL !== 'function') return;
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  return (
    <li className="relative h-16 w-16 overflow-hidden rounded-lg border border-base-300 bg-base-200">
      {preview ? (
        <img
          src={preview}
          alt={file.name}
          className="h-full w-full object-cover"
        />
      ) : (
        <PhotoIcon
          className="m-auto h-6 w-6 text-base-content/40"
          aria-hidden="true"
        />
      )}
      <button
        type="button"
        className="btn btn-circle btn-xs absolute right-0.5 top-0.5 border-0 bg-base-100/90"
        aria-label={t('paste-remove-image', { name: file.name })}
        onClick={onRemove}
      >
        <XMarkIcon className="h-3 w-3" aria-hidden="true" />
      </button>
    </li>
  );
}

/**
 * The one input of "设置产品" (front-flow contract §3.1): a link, a
 * paragraph and images -- dropped, picked or pasted -- in any combination.
 * The first link in the text is what gets fetched; the rest of the text is
 * sent as the description.
 */
export function PasteBox({
  id,
  label,
  value,
  onValueChange,
  files,
  onFilesChange,
  onSubmit,
  submitLabel,
  busy = false,
  busyLabel,
  compact = false,
  autoFocus = false,
}: PasteBoxProps) {
  const { t } = useTranslation('products');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [rejections, setRejections] = useState<
    Array<{ name: string; reason: ImageRejection }>
  >([]);

  const parts = useMemo(() => splitPaste(value), [value]);
  const host = linkHost(parts.url);
  const isEmpty = !parts.url && !parts.text && files.length === 0;

  function addFiles(incoming: File[]) {
    if (incoming.length === 0) return;
    const { accepted, rejected } = acceptImages(files.length, incoming);
    setRejections(
      rejected.map(({ file, reason }) => ({ name: file.name, reason }))
    );
    if (accepted.length > 0) onFilesChange([...files, ...accepted]);
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    if (busy) return;
    addFiles(Array.from(event.dataTransfer?.files ?? []));
  }

  function handlePaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const pasted = Array.from(event.clipboardData?.files ?? []);
    if (pasted.length > 0) {
      event.preventDefault();
      addFiles(pasted);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isEmpty || busy) return;
    onSubmit();
  }

  return (
    <form onSubmit={handleSubmit} noValidate>
      <label htmlFor={id} className="mb-2 block text-sm font-medium">
        {label}
      </label>
      <div
        className={classNames(
          'rounded-box border-2 border-dashed bg-base-100 transition-colors motion-reduce:transition-none',
          dragging
            ? 'border-primary bg-primary/5'
            : 'border-base-300 focus-within:border-primary',
          busy && 'opacity-70'
        )}
        onDragOver={(event) => {
          event.preventDefault();
          if (!busy) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        data-testid={`${id}-dropzone`}
      >
        <textarea
          id={id}
          className={classNames(
            'textarea w-full resize-y border-0 bg-transparent text-base leading-relaxed focus:outline-none',
            compact ? 'min-h-24' : 'min-h-44'
          )}
          placeholder={t('paste-placeholder')}
          value={value}
          onChange={(event) => onValueChange(event.target.value)}
          onPaste={handlePaste}
          disabled={busy}
          autoFocus={autoFocus}
        />

        {files.length > 0 && (
          <ul
            className="flex flex-wrap gap-2 px-4 pb-3"
            aria-label={t('paste-images-count', { count: files.length })}
          >
            {files.map((file, index) => (
              <FileChip
                key={`${file.name}-${file.size}-${index}`}
                file={file}
                onRemove={() =>
                  onFilesChange(files.filter((_, i) => i !== index))
                }
              />
            ))}
          </ul>
        )}

        <div className="flex flex-wrap items-center gap-3 border-t border-base-300 px-4 py-3">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => fileInputRef.current?.click()}
            disabled={busy}
          >
            <PhotoIcon className="h-4 w-4" aria-hidden="true" />
            {t('paste-add-images')}
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept={ACCEPTED_IMAGE_TYPES.join(',')}
            multiple
            className="hidden"
            data-testid={`${id}-file-input`}
            onChange={(event) => {
              addFiles(Array.from(event.target.files ?? []));
              event.target.value = '';
            }}
          />
          <span className="flex-1 text-sm text-base-content/60">
            {dragging
              ? t('paste-dropping')
              : host
                ? t('paste-link-detected', { host })
                : t('paste-drop-hint')}
          </span>
          <button
            type="submit"
            className="btn btn-primary btn-sm"
            disabled={isEmpty || busy}
            aria-describedby={isEmpty ? `${id}-empty-hint` : undefined}
          >
            {busy && (
              <span
                className="loading loading-spinner loading-xs"
                aria-hidden="true"
              />
            )}
            {busy && busyLabel ? busyLabel : submitLabel}
          </button>
        </div>
      </div>
      {isEmpty && (
        <p id={`${id}-empty-hint`} className="sr-only">
          {t('paste-empty')}
        </p>
      )}
      {rejections.length > 0 && (
        <ul className="mt-2 space-y-1 text-sm text-error" role="alert">
          {rejections.map((rejection, index) => (
            <li key={`${rejection.name}-${index}`}>
              {t(`paste-rejected-${rejection.reason}`, {
                name: rejection.name,
              })}
            </li>
          ))}
        </ul>
      )}
    </form>
  );
}
