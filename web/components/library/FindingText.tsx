import { useTranslation } from 'next-i18next';
import {
  findingText,
  type LibraryFinding,
} from '@/lib/library/validation-findings';

/**
 * One validation finding as the draft page and the publish dialog show it:
 * where it sits in the draft content, then its sentence (`findingText`). A
 * finding about the whole draft has no path.
 */
export function FindingText({ finding }: { finding: LibraryFinding }) {
  const { t, i18n } = useTranslation('library');
  return (
    <>
      {finding.path && (
        <span className="font-mono text-xs">{finding.path}: </span>
      )}
      {findingText(t, i18n?.language, finding)}
    </>
  );
}
