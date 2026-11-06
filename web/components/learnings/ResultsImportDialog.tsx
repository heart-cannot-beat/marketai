import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { ArrowDownTrayIcon, ArrowUpTrayIcon } from '@heroicons/react/20/solid';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { ModalScrim } from '@/components/shared/ModalScrim';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import {
  useResultsImport,
  type ImportProblem,
  type ResultsImportView,
} from '@/hooks/learnings/useResultsImport';
import { buildTemplateCsv, downloadText } from '@/lib/learnings/sheet';
import { errorCodeMessage, errorMessage } from '@/lib/i18n/domain-errors';

const CURRENCIES = ['USD', 'CNY', 'HKD', 'EUR', 'GBP', 'JPY'];
const ROW_STATUS_TONE: Record<string, string> = {
  ready: 'badge-success badge-outline',
  replaces: 'badge-info badge-outline',
  unattributable: 'badge-warning badge-outline',
  rejected: 'badge-error badge-outline',
};
const MAX_ROWS_SHOWN = 60;

function ProblemLine({ problem }: { problem: ImportProblem }) {
  const { t, i18n } = useTranslation('learnings');
  const key = `problem-${problem.code}`;
  const text = i18n.exists(`learnings:${key}`)
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

function Preview({ view }: { view: ResultsImportView }) {
  const { t } = useTranslation('learnings');
  // A row's reason reads from its code and params in the shared error table
  // (#253, #255); the server's stored `reason` sentence is for logs only.
  const reasonText = (
    code: string | null | undefined,
    params?: Record<string, string> | null
  ) => (code ? errorCodeMessage(t, code, params) : '');
  const summary = view.summary;
  const byStatus = summary.by_status ?? {};
  const demo =
    (summary.evidence_modes?.mock ?? 0) +
    (summary.evidence_modes?.sandbox ?? 0);
  const allRows = view.rows ?? [];
  const reasons = summary.rejected_reasons ?? [];
  const rows = allRows.slice(0, MAX_ROWS_SHOWN);
  return (
    <div className="space-y-3" data-testid="import-preview">
      {view.duplicate && (
        <div className="alert alert-info py-2 text-sm" role="status">
          <span>{t('duplicate-notice')}</span>
        </div>
      )}
      <p className="text-sm">
        {t('preview-summary', {
          total: summary.total_rows,
          importable: summary.importable_rows,
          rejected: byStatus.rejected ?? 0,
        })}
      </p>
      <ul className="list-disc space-y-1 pl-5 text-sm text-base-content/80">
        <li>
          {t('preview-attributable', { count: summary.attributable_assets })}
        </li>
        {(byStatus.replaces ?? 0) > 0 && (
          <li>{t('preview-replaces', { count: byStatus.replaces })}</li>
        )}
        {(byStatus.unattributable ?? 0) > 0 && (
          <li>
            {t('preview-unattributable', { count: byStatus.unattributable })}
          </li>
        )}
        {demo > 0 && <li>{t('preview-demo', { count: demo })}</li>}
      </ul>
      {reasons.length > 0 && (
        <div className="text-sm">
          <p className="font-medium">{t('preview-reasons')}</p>
          <ul className="list-disc pl-5 text-base-content/80">
            {reasons.map((reason) => (
              <li key={reason.code}>
                {t('preview-reason', {
                  reason: reasonText(reason.code),
                  count: reason.count,
                })}
              </li>
            ))}
          </ul>
        </div>
      )}
      <details className="text-sm">
        <summary className="cursor-pointer text-base-content/70">
          {t('preview-rows')}
        </summary>
        <div className="max-h-72 overflow-auto">
          <table className="table table-xs">
            <tbody>
              {rows.map((row) => (
                <tr key={row.line}>
                  <td className="whitespace-nowrap text-base-content/60">
                    {t('row-line', { line: row.line })}
                  </td>
                  <td>
                    <span
                      className={classNames(
                        'badge badge-sm whitespace-nowrap',
                        ROW_STATUS_TONE[row.status]
                      )}
                    >
                      {t(`status-${row.status}`)}
                    </span>
                  </td>
                  <td className="whitespace-nowrap font-medium">
                    {row.asset_code ?? ''}
                  </td>
                  <td>
                    {row.status === 'rejected'
                      ? reasonText(row.reason_code, row.reason_params)
                      : row.description_code
                        ? t(`description-${row.description_code}`)
                        : row.description}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {(view.rows_truncated || allRows.length > MAX_ROWS_SHOWN) && (
          <p className="text-xs text-base-content/60">
            {t('preview-truncated')}
          </p>
        )}
      </details>
    </div>
  );
}

export interface ResultsImportDialogProps {
  productId: string;
  open: boolean;
  onClose: () => void;
  /** After a commit: refresh the learnings. */
  onCommitted?: (view: ResultsImportView) => void;
}

/**
 * Import ad results (Issue #166): download the template (every image with
 * its code), upload a CSV or paste a table, read the server's row-by-row
 * match preview, confirm. Nothing is counted before the confirm.
 */
export function ResultsImportDialog({
  productId,
  open,
  onClose,
  onCommitted,
}: ResultsImportDialogProps) {
  const { t } = useTranslation('learnings');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const flow = useResultsImport(productId);
  const [pasted, setPasted] = useState('');
  const [currency, setCurrency] = useState('USD');
  const [template, setTemplate] = useState<'idle' | 'working' | 'failed'>(
    'idle'
  );
  const fileInput = useRef<HTMLInputElement>(null);
  const titleId = useId();
  const pasteId = useId();
  const currencyId = useId();
  const busy = ['reading', 'uploading', 'previewing', 'committing'].includes(
    flow.phase
  );

  useEffect(() => {
    if (!open) {
      flow.reset();
      setPasted('');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const downloadTemplate = async () => {
    setTemplate('working');
    const result = await gateway.getResultsTemplate({
      workspaceSlug: current.slug,
      productId,
    });
    if (result.kind !== 'ok') {
      setTemplate('failed');
      return;
    }
    downloadText(result.data.file_name, buildTemplateCsv(result.data));
    setTemplate('idle');
  };

  const confirm = async () => {
    const committed = await flow.commit();
    if (committed) onCommitted?.(committed);
  };

  const preview = flow.preview;
  const showInput =
    flow.phase === 'idle' ||
    flow.phase === 'reading' ||
    flow.phase === 'uploading' ||
    flow.phase === 'previewing';

  return (
    <ModalScrim labelledBy={titleId}>
      <div className="modal-box max-w-3xl space-y-4">
        <div>
          <h3 id={titleId} className="text-lg font-bold">
            {t('import-title')}
          </h3>
          <p className="mt-1 max-w-prose text-sm text-base-content/70">
            {t('import-intro')}
          </p>
        </div>

        {flow.problem && <ProblemLine problem={flow.problem} />}

        {showInput && (
          <div className="space-y-4">
            <button
              type="button"
              className="btn btn-ghost btn-sm px-0 text-primary"
              onClick={downloadTemplate}
              disabled={template === 'working'}
            >
              <ArrowDownTrayIcon className="h-4 w-4" aria-hidden="true" />
              {template === 'working'
                ? t('template-downloading')
                : t('template-download')}
            </button>
            {template === 'failed' && (
              <p className="text-sm text-error" role="alert">
                {t('template-failed')}
              </p>
            )}
            <div className="flex flex-wrap items-end gap-3">
              <label className="form-control w-44" htmlFor={currencyId}>
                <span className="label-text text-sm">
                  {t('currency-label')}
                </span>
                <select
                  id={currencyId}
                  className="select select-bordered select-sm"
                  value={currency}
                  onChange={(event) => setCurrency(event.target.value)}
                  disabled={busy}
                >
                  {CURRENCIES.map((code) => (
                    <option key={code} value={code}>
                      {code}
                    </option>
                  ))}
                </select>
              </label>
              <input
                ref={fileInput}
                type="file"
                accept=".csv,text/csv,.tsv,.txt"
                className="hidden"
                data-testid="results-file"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  event.target.value = '';
                  if (file) void flow.previewFile(file, currency);
                }}
              />
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={() => fileInput.current?.click()}
                disabled={busy}
              >
                <ArrowUpTrayIcon className="h-4 w-4" aria-hidden="true" />
                {t('choose-file')}
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => void flow.previewMetaSync()}
                disabled={busy}
              >
                {t('meta-sync')}
              </button>
            </div>
            <div className="space-y-2">
              <label htmlFor={pasteId} className="text-sm font-medium">
                {t('or-paste')}
              </label>
              <textarea
                id={pasteId}
                className="textarea textarea-bordered h-28 w-full font-sans text-sm"
                placeholder={t('paste-placeholder')}
                aria-label={t('paste-label')}
                value={pasted}
                onChange={(event) => setPasted(event.target.value)}
                disabled={busy}
              />
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => void flow.previewText(pasted, currency)}
                disabled={busy || !pasted.trim()}
              >
                {t('check-paste')}
              </button>
            </div>
            {busy && (
              <p className="flex items-center gap-2 text-sm" role="status">
                <span
                  className="loading loading-spinner loading-xs"
                  aria-hidden="true"
                />
                {t(flow.phase)}
              </p>
            )}
          </div>
        )}

        {preview && !showInput && <Preview view={preview} />}

        {flow.phase === 'committed' && preview && !preview.duplicate && (
          <div className="alert alert-success py-2 text-sm" role="status">
            <span>
              {t('committed', { count: preview.summary.importable_rows })}
            </span>
          </div>
        )}

        <div className="modal-action">
          {flow.phase === 'preview' && preview && (
            <>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={flow.reset}
              >
                {t('back')}
              </button>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={confirm}
                disabled={preview.summary.importable_rows === 0}
              >
                {t('confirm-import', {
                  count: preview.summary.importable_rows,
                })}
              </button>
            </>
          )}
          {flow.phase === 'committing' && (
            <span className="flex items-center gap-2 text-sm" role="status">
              <span
                className="loading loading-spinner loading-xs"
                aria-hidden="true"
              />
              {t('committing')}
            </span>
          )}
          <button
            type="button"
            className="btn btn-sm"
            onClick={onClose}
            disabled={flow.phase === 'committing'}
          >
            {flow.phase === 'committed' ? t('close') : t('cancel')}
          </button>
        </div>
      </div>
    </ModalScrim>
  );
}
