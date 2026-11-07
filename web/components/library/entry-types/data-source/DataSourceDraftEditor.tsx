import { useId } from 'react';
import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { DataSourceFields } from '@/lib/library/draft-codecs';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';

interface DataSourceDraftEditorProps {
  session: UseDraftSessionResult;
}

export interface DataSourceAdapterOption {
  /** The `adapter_kind` the option writes. */
  kind: string;
  /** The `execution_mode` written with it: a controlled Mock source is its
   * own source with its own adapter, not a switch on a real one. */
  executionMode: 'real' | 'mock';
  /** Whether an adapter can serve the source yet; the others are shown but
   * cannot be chosen. */
  available: boolean;
}

/**
 * The source types the server accepts, each with the execution mode it pairs
 * with (`ADAPTER_EXECUTION_MODES` in `app/library/contracts.py`; any other
 * pair is blocked at publish with `ds_execution_mode_mismatch`) and whether
 * it has an adapter (`IMPLEMENTED_ADAPTERS` in
 * `app/library/connection_checks.py`). `__tests__/locales/
 * data-source-adapters.spec.ts` holds the three together (#396).
 */
export const DATA_SOURCE_ADAPTERS: readonly DataSourceAdapterOption[] = [
  { kind: 'manual', executionMode: 'real', available: true },
  { kind: 'mock', executionMode: 'mock', available: true },
  { kind: 'shopify', executionMode: 'real', available: false },
];

/**
 * The source type is the only choice: choosing one writes `adapter_kind` and
 * its `execution_mode` together, and the mode shows read-only. A draft whose
 * stored values the form does not offer (hand-written JSON) is not rewritten
 * on open, which would mark it unsaved; the form asks to choose again.
 */
export function DataSourceDraftEditor({ session }: DataSourceDraftEditorProps) {
  const { t } = useTranslation('library');
  const hintId = useId();
  const fields = session.buffer?.fields as DataSourceFields | undefined;
  if (!fields) return null;

  const configEntries = Object.entries(fields.config ?? {});
  const stored = DATA_SOURCE_ADAPTERS.find(
    (option) => option.kind === fields.adapter_kind
  );
  const invalidKind = !stored && Boolean(fields.adapter_kind);
  const mismatch = stored && fields.execution_mode !== stored.executionMode;
  const hint = invalidKind
    ? t('fields.data-source.adapter-invalid')
    : mismatch
      ? t('fields.data-source.execution-mode-mismatch')
      : undefined;

  return (
    <div className="space-y-4">
      <div>
        <label className="form-control w-full">
          <div className="label">
            <span className="label-text">
              {t('fields.data-source.adapter-kind')}
            </span>
          </div>
          <select
            className="select select-bordered w-full"
            value={stored?.kind ?? ''}
            aria-describedby={hint ? hintId : undefined}
            onChange={(e) => {
              const option = DATA_SOURCE_ADAPTERS.find(
                (o) => o.kind === e.target.value
              );
              if (!option) return;
              session.updateFields({
                adapter_kind: option.kind,
                execution_mode: option.executionMode,
              });
            }}
          >
            <option value="" disabled>
              {t('fields.data-source.adapter-placeholder')}
            </option>
            {DATA_SOURCE_ADAPTERS.map((option) => {
              const name = t(`fields.data-source.adapter-${option.kind}`);
              return (
                <option
                  key={option.kind}
                  value={option.kind}
                  disabled={!option.available}
                >
                  {option.available
                    ? name
                    : t('fields.data-source.adapter-unavailable', { name })}
                </option>
              );
            })}
          </select>
        </label>
        {hint && (
          <p
            id={hintId}
            className="mt-1 text-sm text-amber-700 dark:text-amber-300"
          >
            {hint}
          </p>
        )}
      </div>

      <div>
        <p className="text-sm font-medium text-gray-700 dark:text-gray-300">
          {t('fields.data-source.execution-mode')}
        </p>
        {stored && !mismatch && (
          <div className="mt-1">
            <ExecutionModeTag mode={stored.executionMode} />
          </div>
        )}
        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
          {t('fields.data-source.execution-mode-help')}
        </p>
      </div>

      <div>
        <p className="text-sm font-medium text-gray-700 dark:text-gray-300">
          {t('fields.data-source.config')}
        </p>
        {configEntries.length > 0 ? (
          <pre className="mt-1 rounded border border-gray-200 bg-gray-50 p-3 text-xs text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 overflow-x-auto">
            {JSON.stringify(fields.config, null, 2)}
          </pre>
        ) : (
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            {t('fields.data-source.config-empty')}
          </p>
        )}
      </div>
    </div>
  );
}
