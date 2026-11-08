/**
 * Rows of a Manual Fact Set's ValidationReport (#23). The server locates a
 * finding about one fact as `facts[<index>].<field>` (or `facts[<index>]` for
 * the fact as a whole, `app/library/contracts.py::_validate_manual_fact_set`);
 * any other path is about the fact set as a whole. A row is numbered from 1:
 * row 1 is `facts[0]`, the first fact of the file (in a CSV, the first line
 * after the header).
 */
const FACT_PATH = /^facts\[(\d+)\](?:\.(.+))?$/;

export interface FactRowFinding<F> {
  /** The fact's field the finding is about; empty for the whole fact. */
  field: string;
  finding: F;
}

export interface FactRow<F> {
  /** Index into `content.facts`; the row number is `index + 1`. */
  index: number;
  findings: FactRowFinding<F>[];
}

export interface FactRowGroups<F> {
  /** Findings about the fact set as a whole, in the server's order. */
  general: F[];
  /** Rows with findings, in row order; each keeps the server's order. */
  rows: FactRow<F>[];
}

export function groupByFactRow<F extends { path: string }>(
  findings: readonly F[]
): FactRowGroups<F> {
  const general: F[] = [];
  const byIndex = new Map<number, FactRowFinding<F>[]>();
  for (const finding of findings) {
    const match = FACT_PATH.exec(finding.path);
    if (!match) {
      general.push(finding);
      continue;
    }
    const index = Number(match[1]);
    const row = byIndex.get(index) ?? [];
    row.push({ field: match[2] ?? '', finding });
    byIndex.set(index, row);
  }
  const rows = Array.from(byIndex, ([index, rowFindings]) => ({
    index,
    findings: rowFindings,
  })).sort((a, b) => a.index - b.index);
  return { general, rows };
}

/** Each fact's `fact_id` by index, where the content has one. */
export function factIdsOf(content: unknown): Array<string | null> {
  const facts =
    content && typeof content === 'object'
      ? (content as { facts?: unknown }).facts
      : undefined;
  if (!Array.isArray(facts)) return [];
  return facts.map((fact) => {
    const id =
      fact && typeof fact === 'object'
        ? (fact as { fact_id?: unknown }).fact_id
        : undefined;
    return typeof id === 'string' && id.trim() ? id : null;
  });
}

/** The `blocking` findings of a stored ValidationReport, read field by field. */
export function storedBlockingFindings(
  report: unknown
): Array<{ code: string; message: string; path: string }> {
  const blocking =
    report && typeof report === 'object'
      ? (report as { blocking?: unknown }).blocking
      : undefined;
  if (!Array.isArray(blocking)) return [];
  const text = (value: unknown) => (typeof value === 'string' ? value : '');
  return blocking.map((item) => {
    const record =
      item && typeof item === 'object' ? (item as Record<string, unknown>) : {};
    return {
      code: text(record.code),
      message: text(record.message),
      path: text(record.path),
    };
  });
}
