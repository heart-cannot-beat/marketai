import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigationBlocker } from '@/components/workspace/NavigationBlocker';
import { useLibraryCommand } from './useLibraryCommand';
import { useLibraryCache } from './useLibraryCache';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import type { LibraryEntryType } from '@/lib/library/navigation';
import {
  getCodec,
  isSchemaSupported,
  type ParsedDraft,
} from '@/lib/library/draft-codecs';

/**
 * One finding of `POST /library/entries/{id}/validate`, as the server's
 * `Finding.as_dict()` writes it (`app/library/contracts.py`). `path` locates
 * it in the draft content (`facts[3].currency`); it is empty when the finding
 * is about the draft as a whole.
 */
export interface ValidationFinding {
  code: string;
  message: string;
  path: string;
  severity: 'blocking' | 'warning';
}

/**
 * The server's verdict on one draft revision. Any blocking finding stops the
 * publish; each warning code must be acknowledged first, as the publish
 * command requires (`acknowledged_warnings`).
 */
export interface ValidationResult {
  blocking: ValidationFinding[];
  warnings: ValidationFinding[];
  /** The draft revision that was validated (sent as its precondition). */
  revision: number;
  contentHash: string;
  acknowledged: Set<string>;
}

type ValidateResponse = components['schemas']['LibraryValidationResult'];

/** The schema types each finding as an open object; read the four fields. */
function toFindings(
  items: ValidateResponse['blocking'],
  severity: ValidationFinding['severity']
): ValidationFinding[] {
  return (items ?? []).map((item) => ({
    code: typeof item.code === 'string' ? item.code : '',
    message: typeof item.message === 'string' ? item.message : '',
    path: typeof item.path === 'string' ? item.path : '',
    severity,
  }));
}

export interface ConflictState {
  serverRevision: number;
  serverContent: Record<string, unknown>;
  attemptedContent: Record<string, unknown>;
  baseContent: Record<string, unknown>;
}

interface ServerSnapshot {
  content: Record<string, unknown>;
  revision: number;
  etag: string;
  contentSchemaVersion: number;
}

export interface DraftSessionState {
  isSupported: boolean;
  buffer: ParsedDraft<unknown> | null;
  isDirty: boolean;
  isSaving: boolean;
  saveProblem: ProblemDetails | undefined;
  isValidating: boolean;
  validation: ValidationResult | null;
  conflict: ConflictState | null;
}

export interface DraftSessionActions {
  updateField: (field: string, value: unknown) => void;
  updateFields: (updates: Record<string, unknown>) => void;
  /** Replace the whole draft content (known fields *and* unknown keys) --
   * the raw JSON editor's write path, where keys can also be removed. */
  replaceContent: (content: Record<string, unknown>) => void;
  save: () => Promise<boolean>;
  validate: () => Promise<boolean>;
  acknowledgeWarning: (code: string) => void;
  resolveConflictWithServer: () => void;
  resolveConflictWithLocal: () => void;
  cancelConflict: () => void;
  discard: () => void;
  canPublish: () => boolean;
}

export type UseDraftSessionResult = DraftSessionState & DraftSessionActions;

interface DraftData {
  id: string;
  entry_id: string;
  revision: number;
  state: string;
  content_schema_version: number;
  content?: Record<string, unknown>;
  content_hash?: string;
  etag?: string;
}

export function useDraftSession(
  entryId: string | null,
  entryType: LibraryEntryType | null,
  draft: DraftData | null
): UseDraftSessionResult {
  const saveCmd = useLibraryCommand('save_draft');
  const validateCmd = useLibraryCommand('validate_draft');
  const { invalidate } = useLibraryCache();
  const blocker = useNavigationBlocker();

  const snapshotRef = useRef<ServerSnapshot | null>(null);
  const lastDraftKeyRef = useRef<string>('');
  const [buffer, setBuffer] = useState<ParsedDraft<unknown> | null>(null);
  const [isDirty, setIsDirty] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [saveProblem, setSaveProblem] = useState<ProblemDetails | undefined>();
  const [isValidating, setIsValidating] = useState(false);
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [conflict, setConflict] = useState<ConflictState | null>(null);

  const supported =
    entryType !== null &&
    draft !== null &&
    isSchemaSupported(entryType, draft.content_schema_version);

  useEffect(() => {
    if (!draft || !entryType || !supported) {
      snapshotRef.current = null;
      lastDraftKeyRef.current = '';
      setBuffer(null);
      setIsDirty(false);
      setSaveProblem(undefined);
      setValidation(null);
      setConflict(null);
      return;
    }

    const draftKey = `${draft.entry_id}:${draft.revision}:${draft.etag ?? ''}`;
    if (draftKey === lastDraftKeyRef.current) {
      return;
    }
    lastDraftKeyRef.current = draftKey;

    const content = draft.content ?? {};
    const codec = getCodec(entryType);
    const parsed = codec.parse(content);

    snapshotRef.current = {
      content,
      revision: draft.revision,
      etag: draft.etag ?? '',
      contentSchemaVersion: draft.content_schema_version,
    };

    setBuffer(parsed);
    setIsDirty(false);
    setSaveProblem(undefined);
    setValidation(null);
    setConflict(null);
  }, [draft, entryType, supported]);

  useEffect(() => {
    const unregister = blocker.register({
      isBlocked: () => isDirty,
      onDiscard: () => {
        if (snapshotRef.current && entryType) {
          const codec = getCodec(entryType);
          setBuffer(codec.parse(snapshotRef.current.content));
          setIsDirty(false);
          setSaveProblem(undefined);
          setConflict(null);
        }
      },
    });
    return unregister;
  }, [blocker, isDirty, entryType]);

  const updateField = useCallback((field: string, value: unknown) => {
    setBuffer((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        fields: { ...prev.fields, [field]: value },
      };
    });
    setIsDirty(true);
    setSaveProblem(undefined);
  }, []);

  const updateFields = useCallback((updates: Record<string, unknown>) => {
    setBuffer((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        fields: { ...prev.fields, ...updates },
      };
    });
    setIsDirty(true);
    setSaveProblem(undefined);
  }, []);

  const replaceContent = useCallback(
    (content: Record<string, unknown>) => {
      if (!entryType) return;
      const codec = getCodec(entryType);
      setBuffer(codec.parse(content));
      setIsDirty(true);
      setSaveProblem(undefined);
    },
    [entryType]
  );

  const save = useCallback(async (): Promise<boolean> => {
    if (!entryId || !entryType || !buffer || !snapshotRef.current) return false;

    const codec = getCodec(entryType);
    const content = codec.serialize(buffer);

    setIsSaving(true);
    setSaveProblem(undefined);

    try {
      const outcome = await saveCmd.run({
        entry_id: entryId,
        ifMatch: snapshotRef.current.revision,
        body: { content },
      });

      if (outcome.kind === 'success') {
        const result = outcome.data as Record<string, unknown>;
        const newRevision =
          typeof result.revision === 'number'
            ? result.revision
            : snapshotRef.current.revision + 1;
        const newEtag =
          typeof outcome.meta?.etag === 'string' ? outcome.meta.etag : '';

        snapshotRef.current = {
          content,
          revision: newRevision,
          etag: newEtag,
          contentSchemaVersion: snapshotRef.current.contentSchemaVersion,
        };

        setIsDirty(false);
        setValidation(null);
        saveCmd.reset();

        await invalidate({
          resources: ['entry', 'entryList', 'events'],
        });

        return true;
      }

      if (outcome.kind === 'problem') {
        if (outcome.problem.status === 409) {
          const serverData = outcome.problem as ProblemDetails & {
            server_revision?: number;
            server_content?: Record<string, unknown>;
          };

          setConflict({
            serverRevision:
              serverData.server_revision ?? snapshotRef.current.revision + 1,
            serverContent: serverData.server_content ?? {},
            attemptedContent: content,
            baseContent: snapshotRef.current.content,
          });
          return false;
        }

        setSaveProblem(outcome.problem);
        return false;
      }

      return false;
    } finally {
      setIsSaving(false);
    }
  }, [entryId, entryType, buffer, saveCmd, invalidate]);

  const validate = useCallback(async (): Promise<boolean> => {
    if (!entryId) return false;

    setIsValidating(true);

    try {
      const revision = snapshotRef.current?.revision;
      const outcome = await validateCmd.run({
        entry_id: entryId,
        ifMatch: revision,
      });

      if (outcome.kind === 'success') {
        const result = outcome.data as ValidateResponse;
        const blocking = toFindings(result.blocking, 'blocking');
        setValidation({
          blocking,
          warnings: toFindings(result.warnings, 'warning'),
          revision: revision ?? 0,
          contentHash: result.content_hash ?? '',
          acknowledged: new Set<string>(),
        });

        validateCmd.reset();
        return blocking.length === 0;
      }

      return false;
    } finally {
      setIsValidating(false);
    }
  }, [entryId, validateCmd]);

  const acknowledgeWarning = useCallback((code: string) => {
    setValidation((prev) => {
      if (!prev) return prev;
      const next = new Set(prev.acknowledged);
      next.add(code);
      return { ...prev, acknowledged: next };
    });
  }, []);

  const resolveConflictWithServer = useCallback(() => {
    if (!conflict || !entryType) return;

    const codec = getCodec(entryType);
    const parsed = codec.parse(conflict.serverContent);

    snapshotRef.current = {
      content: conflict.serverContent,
      revision: conflict.serverRevision,
      etag: '',
      contentSchemaVersion: snapshotRef.current?.contentSchemaVersion ?? 1,
    };

    setBuffer(parsed);
    setIsDirty(false);
    setConflict(null);
    setSaveProblem(undefined);
  }, [conflict, entryType]);

  const resolveConflictWithLocal = useCallback(() => {
    if (!conflict) return;

    snapshotRef.current = {
      content: snapshotRef.current?.content ?? {},
      revision: conflict.serverRevision,
      etag: '',
      contentSchemaVersion: snapshotRef.current?.contentSchemaVersion ?? 1,
    };

    setConflict(null);
    setIsDirty(true);
    setSaveProblem(undefined);
  }, [conflict]);

  const cancelConflict = useCallback(() => {
    setConflict(null);
  }, []);

  const discard = useCallback(() => {
    if (!entryType || !snapshotRef.current) return;
    const codec = getCodec(entryType);
    setBuffer(codec.parse(snapshotRef.current.content));
    setIsDirty(false);
    setSaveProblem(undefined);
    setValidation(null);
    setConflict(null);
  }, [entryType]);

  const canPublish = useCallback((): boolean => {
    if (isDirty) return false;
    if (!validation) return false;

    if (validation.blocking.length > 0) return false;

    const unacknowledged = validation.warnings.some(
      (f) => !validation.acknowledged.has(f.code)
    );
    if (unacknowledged) return false;

    if (snapshotRef.current) {
      if (validation.revision !== snapshotRef.current.revision) return false;
    }

    return true;
  }, [isDirty, validation]);

  return {
    isSupported: supported,
    buffer,
    isDirty,
    isSaving,
    saveProblem,
    isValidating,
    validation,
    conflict,
    updateField,
    updateFields,
    replaceContent,
    save,
    validate,
    acknowledgeWarning,
    resolveConflictWithServer,
    resolveConflictWithLocal,
    cancelConflict,
    discard,
    canPublish,
  };
}
