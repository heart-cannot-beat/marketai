/**
 * Reading a Meta daily report the user picks, shared by the earlier tests'
 * round page (`MarketingExecution`) and a test's "test in progress" screen
 * (#257). Both send the file's text as it is; the server parses it.
 */

/** The largest report either upload accepts (`csv_too_large`). */
export const REPORT_FILE_LIMIT = 262144;

/** The columns a custom column mapping can name, in the order the form asks. */
export const REPORT_COLUMNS = [
  'date',
  'ad_id',
  'spend',
  'impressions',
  'clicks',
  'purchases',
  'purchase_value',
] as const;

export type ReportFile =
  | { ok: true; text: string; filename: string }
  | { ok: false; reason: 'too_large' | 'unreadable' };

/** The picked file's text, or why it cannot be sent. */
export async function readReportFile(file: File): Promise<ReportFile> {
  if (file.size > REPORT_FILE_LIMIT) return { ok: false, reason: 'too_large' };
  try {
    return { ok: true, text: await file.text(), filename: file.name };
  } catch {
    return { ok: false, reason: 'unreadable' };
  }
}

/** The mapping as sent: only the columns the user named, trimmed. */
export function columnMapping(
  mapping: Record<string, string>
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(mapping)
      .filter(([, value]) => value.trim())
      .map(([key, value]) => [key, value.trim()])
  );
}
