import { useTranslation } from 'next-i18next';
import type { ReactNode } from 'react';
import { useLibraryRoute } from '@/hooks/library/useLibraryRoute';
import { useResolvedLibraryVersion } from '@/hooks/library/useResolvedLibraryVersion';
import {
  groupByFactRow,
  storedBlockingFindings,
} from '@/lib/library/fact-rows';
import { ProblemState } from '../../detail/ProblemState';

interface ManualFactSetFactListProps {
  entryId: string;
  currentVersionId: string | null;
  /** The open Draft's content (`GET /library/entries/{id}` embeds it), read
   * only while the fact set has no published version. */
  draftContent: Record<string, unknown> | null;
  /** That draft's stored ValidationReport (#23): its blocking findings mark
   * their rows, and the Draft tab lists each reason. */
  draftValidation?: Record<string, unknown> | null;
}

/**
 * The facts of a Manual Fact Set, read-only (#23): the current version's
 * `content.facts`, or the draft's while nothing is published, with a link to
 * the data source the facts were imported into. Imports start on that data
 * source's Facts tab, never here. A draft whose ValidationReport blocks
 * publishing marks the rows with problems and links to the Draft tab.
 */
export function ManualFactSetFactList({
  entryId,
  currentVersionId,
  draftContent,
  draftValidation = null,
}: ManualFactSetFactListProps) {
  const { t } = useTranslation('library');
  const { version, problem, mismatch, isLoading, refresh } =
    useResolvedLibraryVersion(currentVersionId, entryId);

  if (!currentVersionId) {
    if (!draftContent) return <Note>{t('fact-list.empty')}</Note>;
    return (
      <FactList
        entryId={entryId}
        content={draftContent}
        heading={t('fact-list.draft-heading')}
        isDraft
        validation={draftValidation}
      />
    );
  }

  if (mismatch) {
    return (
      <div className="py-8 text-center" role="alert">
        <p className="text-sm text-red-600 dark:text-red-400">
          {t('tabs.current.version-mismatch')}
        </p>
      </div>
    );
  }

  if (problem) return <ProblemState onRetry={() => void refresh()} />;

  if (isLoading || !version) {
    return (
      <div className="flex items-center justify-center py-12" aria-busy="true">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('detail.loading')}
        </p>
      </div>
    );
  }

  return (
    <FactList
      entryId={entryId}
      content={version.content ?? null}
      heading={t('fact-list.version-heading', {
        no: String(version.version_no),
      })}
      isDraft={false}
    />
  );
}

/** Columns read by name; #26 settles the value fields of each fact type, so
 * every other field is shown as it is under "Other values". */
const LISTED_FIELDS = new Set([
  'fact_id',
  'fact_type',
  'country_code',
  'amount',
  'currency',
  'valid_from',
  'valid_to',
]);

type Fact = Record<string, unknown>;

function isFact(value: unknown): value is Fact {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function text(value: unknown): string | null {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  return JSON.stringify(value);
}

function FactList({
  entryId,
  content,
  heading,
  isDraft,
  validation = null,
}: {
  entryId: string;
  content: Record<string, unknown> | null;
  heading: string;
  isDraft: boolean;
  validation?: Record<string, unknown> | null;
}) {
  const { t } = useTranslation('library');
  const { setEntry } = useLibraryRoute();
  const facts = content?.facts;

  if (!Array.isArray(facts)) {
    return <Note>{t('fact-list.unreadable')}</Note>;
  }

  // Indexes stay those of `content.facts`: the report's paths use them.
  const rows = facts
    .map((fact, index) => ({ fact, index }))
    .filter((row): row is { fact: Fact; index: number } => isFact(row.fact));
  const blocking = validation ? storedBlockingFindings(validation) : [];
  const report = groupByFactRow(blocking);
  const problemFields = new Map(
    report.rows.map(({ index, findings }) => [
      index,
      Array.from(
        new Set(
          findings.map(({ field }) => field || t('draft.fact-rows.whole-fact'))
        )
      ),
    ])
  );
  const showProblems = validation !== null && blocking.length > 0;
  const dataSourceId = text(content?.data_source_entry_id);
  const notSet = (
    <>
      <span aria-hidden="true">—</span>
      <span className="sr-only">{t('fact-list.not-set')}</span>
    </>
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h3 className="text-sm font-medium text-gray-900 dark:text-gray-100">
          {heading}
        </h3>
        {dataSourceId && (
          <button
            type="button"
            className="link link-primary text-sm"
            onClick={() => setEntry(dataSourceId, 'overview')}
          >
            {t('fact-list.open-source')}
          </button>
        )}
      </div>

      {isDraft && (
        <p className="text-xs text-amber-700 dark:text-amber-300">
          {t('fact-list.draft-notice')}
        </p>
      )}

      {showProblems && (
        <div
          className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-red-300 bg-red-50 p-3 dark:border-red-700 dark:bg-red-950"
          role="status"
        >
          <p className="text-sm text-red-800 dark:text-red-200">
            {report.rows.length > 0
              ? t('fact-list.problems.rows', {
                  count: String(report.rows.length),
                })
              : t('fact-list.problems.draft')}
          </p>
          <button
            type="button"
            className="link link-error text-sm"
            onClick={() => setEntry(entryId, 'draft')}
          >
            {t('fact-list.problems.open-draft')}
          </button>
        </div>
      )}

      {rows.length === 0 ? (
        <Note>{t('fact-list.empty')}</Note>
      ) : (
        <div className="overflow-x-auto">
          <table className="table table-sm" aria-label={heading}>
            <thead>
              <tr>
                <th scope="col">{t('fact-list.columns.fact-id')}</th>
                {showProblems && (
                  <th scope="col">{t('fact-list.columns.problems')}</th>
                )}
                <th scope="col">{t('fact-list.columns.fact-type')}</th>
                <th scope="col">{t('fact-list.columns.market')}</th>
                <th scope="col">{t('fact-list.columns.amount')}</th>
                <th scope="col">{t('fact-list.columns.valid')}</th>
                <th scope="col">{t('fact-list.columns.other')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ fact, index }) => {
                const amount = [text(fact.amount), text(fact.currency)]
                  .filter(Boolean)
                  .join(' ');
                const validFrom = text(fact.valid_from);
                const validTo = text(fact.valid_to);
                const others = Object.entries(fact).filter(
                  ([key, value]) => !LISTED_FIELDS.has(key) && text(value)
                );
                const fields = problemFields.get(index);
                return (
                  <tr
                    key={index}
                    className={
                      fields ? 'bg-red-50 dark:bg-red-950/40' : undefined
                    }
                  >
                    <td className="font-mono text-xs break-all">
                      {text(fact.fact_id) ?? notSet}
                    </td>
                    {showProblems && (
                      <td
                        className={
                          fields
                            ? 'text-xs text-red-700 dark:text-red-300'
                            : 'text-xs'
                        }
                      >
                        {fields ? (
                          t('fact-list.problems.row', {
                            row: String(index + 1),
                            fields: fields.join(', '),
                          })
                        ) : (
                          <>
                            <span aria-hidden="true">—</span>
                            <span className="sr-only">
                              {t('fact-list.problems.none')}
                            </span>
                          </>
                        )}
                      </td>
                    )}
                    <td className="font-mono text-xs">
                      {text(fact.fact_type) ?? notSet}
                    </td>
                    <td>{text(fact.country_code) ?? notSet}</td>
                    <td className="whitespace-nowrap">{amount || notSet}</td>
                    <td className="whitespace-nowrap">
                      {validFrom || validTo
                        ? t('fact-list.valid-range', {
                            from: validFrom ?? '—',
                            to: validTo ?? '—',
                          })
                        : notSet}
                    </td>
                    <td className="text-xs">
                      {others.length === 0
                        ? notSet
                        : others.map(([key, value]) => (
                            <div key={key} className="break-all">
                              <span className="font-mono">{key}</span>
                              {`: ${text(value)}`}
                            </div>
                          ))}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Note({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-center justify-center py-12 text-center">
      <p className="text-sm text-gray-500 dark:text-gray-400">{children}</p>
    </div>
  );
}
