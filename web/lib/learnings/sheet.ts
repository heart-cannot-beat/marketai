import type { components } from '@/lib/domain-client/generated/domain-v1';

export type ResultsTemplate = components['schemas']['ResultsTemplate'];

/** Above this many characters a sheet is uploaded instead of sent inline. */
export const INLINE_CSV_LIMIT = 300_000;

function cell(value: string | number | null | undefined): string {
  const text = value === null || value === undefined ? '' : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/**
 * The results template a seller fills in: one row per image with its code
 * and what it shows; metric columns left blank. A UTF-8 BOM makes Excel
 * open the Chinese headers correctly.
 */
export function buildTemplateCsv(template: ResultsTemplate): string {
  const keys = template.columns.map((column) => column.key);
  const lines = [
    template.columns.map((column) => cell(column.label)).join(','),
  ];
  for (const row of template.rows ?? []) {
    const values: Record<string, string> = {
      asset_code: row.asset_code,
      direction: row.direction,
      combination: row.combination,
      currency: template.currency ?? '',
    };
    lines.push(keys.map((key) => cell(values[key] ?? '')).join(','));
  }
  return `\ufeff${lines.join('\r\n')}\r\n`;
}

/**
 * Decode a spreadsheet the seller picked: UTF-8 first, then GB18030 (what
 * Chinese Excel writes for "CSV"). Returns `null` when neither fits.
 */
export function decodeSheet(buffer: ArrayBuffer): string | null {
  for (const encoding of ['utf-8', 'gb18030']) {
    try {
      const text = new TextDecoder(encoding, { fatal: true }).decode(buffer);
      return text.replace(/^\ufeff/, '');
    } catch {
      // try the next encoding
    }
  }
  return null;
}

export function downloadText(fileName: string, text: string): void {
  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
