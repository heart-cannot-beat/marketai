import { useTranslation } from 'next-i18next';
import { FindingText } from '@/components/library/FindingText';
import { groupByFactRow } from '@/lib/library/fact-rows';
import {
  findingText,
  type LibraryFinding,
} from '@/lib/library/validation-findings';

interface FactRowFindingsProps {
  findings: readonly LibraryFinding[];
  /** Each fact's `fact_id` by index, shown next to its row number. */
  factIds?: ReadonlyArray<string | null>;
}

/**
 * A Manual Fact Set's blocking findings, row by row (#23): the findings
 * about the fact set as a whole first, then one group per fact with its row
 * number, its fact ID and, for each problem, the field and the reason. An
 * import creates the draft even when rows have problems, and this is the
 * report the Owner reads before publishing (contract §11.4).
 */
export function FactRowFindings({ findings, factIds }: FactRowFindingsProps) {
  const { t, i18n } = useTranslation('library');
  const { general, rows } = groupByFactRow(findings);

  return (
    <div className="mt-2 space-y-3">
      {general.length > 0 && (
        <ul className="space-y-1" role="list">
          {general.map((finding, i) => (
            <li
              key={`${finding.code}-${i}`}
              className="text-sm text-red-700 dark:text-red-300"
            >
              <FindingText finding={finding} />
            </li>
          ))}
        </ul>
      )}

      {rows.length > 0 && (
        <>
          <p className="text-xs text-red-800 dark:text-red-200">
            {t('draft.fact-rows.summary', { count: String(rows.length) })}{' '}
            {t('draft.fact-rows.numbering')}
          </p>
          <ol className="space-y-2" aria-label={t('draft.fact-rows.label')}>
            {rows.map(({ index, findings: rowFindings }) => {
              const factId = factIds?.[index] ?? null;
              const row = t('draft.fact-rows.row', { row: String(index + 1) });
              return (
                <li
                  key={index}
                  className="rounded-md border border-red-200 bg-white/60 p-2 dark:border-red-800 dark:bg-red-950/40"
                >
                  <p className="flex flex-wrap items-baseline gap-x-2 text-sm font-medium text-red-900 dark:text-red-100">
                    <span>{row}</span>
                    {factId && (
                      <span className="font-mono text-xs break-all">
                        {factId}
                      </span>
                    )}
                  </p>
                  <ul className="mt-1 space-y-1" role="list">
                    {rowFindings.map(({ field, finding }, i) => (
                      <li
                        key={`${finding.code}-${i}`}
                        className="text-sm text-red-700 dark:text-red-300"
                      >
                        <span className="font-mono text-xs">
                          {field || t('draft.fact-rows.whole-fact')}
                        </span>
                        {': '}
                        {findingText(t, i18n?.language, finding)}
                      </li>
                    ))}
                  </ul>
                </li>
              );
            })}
          </ol>
        </>
      )}
    </div>
  );
}
