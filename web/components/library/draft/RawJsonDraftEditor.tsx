import { useEffect, useState } from 'react';
import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { LibraryEntryType } from '@/lib/library/navigation';
import { getCodec } from '@/lib/library/draft-codecs';

interface RawJsonDraftEditorProps {
  entryType: LibraryEntryType;
  session: UseDraftSessionResult;
}

/**
 * Edits the whole draft content as JSON -- every key the server validates
 * (`app/library/contracts.py`), not only the ones a form renders. It is the
 * editor for entry types without an aligned form and the escape hatch for
 * the ones with one (template `constraints`, product `must_match`, …). For
 * an entry type without a form the codec passes the content through, so the
 * text is the stored draft and nothing else: no key the server does not read
 * is filled in (#21).
 *
 * Text is local until it parses: an unparsable buffer is reported inline and
 * never written to the session, so Save keeps working on the last valid
 * content. Parsed content replaces the draft through the entry type's codec
 * (`session.replaceContent`), so the form editor (if any) and the session's
 * known/unknown split stay in step and deleted keys really go away.
 */
export function RawJsonDraftEditor({
  entryType,
  session,
}: RawJsonDraftEditorProps) {
  const { t } = useTranslation('library');
  const codec = getCodec(entryType);
  const content = session.buffer ? codec.serialize(session.buffer) : null;
  const serialized = content ? JSON.stringify(content, null, 2) : '';

  const [text, setText] = useState(serialized);
  const [parseError, setParseError] = useState<string | null>(null);

  // Follow the session when it changes underneath us (a save, a discard, a
  // conflict resolution, or a field edited through the form), but never
  // clobber text the user is still correcting.
  useEffect(() => {
    if (parseError === null) {
      setText(serialized);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serialized]);

  if (!session.buffer) return null;

  const handleChange = (next: string) => {
    setText(next);
    let parsed: unknown;
    try {
      parsed = JSON.parse(next);
    } catch (err) {
      setParseError(err instanceof Error ? err.message : String(err));
      return;
    }
    if (
      parsed === null ||
      typeof parsed !== 'object' ||
      Array.isArray(parsed)
    ) {
      setParseError(t('draft.json-not-object'));
      return;
    }
    setParseError(null);
    // Whole-content replacement, so a key deleted from the JSON is gone
    // from the draft too (a field merge could only add or change keys).
    session.replaceContent(parsed as Record<string, unknown>);
  };

  return (
    <div className="space-y-2">
      <label className="form-control w-full">
        <div className="label">
          <span className="label-text text-xs">{t('draft.json-label')}</span>
        </div>
        <textarea
          className={`textarea textarea-bordered w-full font-mono text-xs ${
            parseError ? 'textarea-error' : ''
          }`}
          rows={16}
          spellCheck={false}
          value={text}
          onChange={(e) => handleChange(e.target.value)}
          aria-label={t('draft.json-label')}
          aria-invalid={parseError !== null}
        />
      </label>
      {parseError && (
        <p className="text-xs text-red-600 dark:text-red-400" role="alert">
          {t('draft.json-parse-error', { detail: parseError })}
        </p>
      )}
    </div>
  );
}
