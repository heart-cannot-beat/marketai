import { useTranslation } from 'next-i18next';

export type DraftSaveState = 'idle' | 'saving' | 'saved' | 'conflict';

interface DraftSaveIndicatorProps {
  state: DraftSaveState;
}

/**
 * Small `aria-live` status text next to the draft's editing controls,
 * reflecting the outcome of the most recent CAS `update_generation_draft`
 * call. `conflict` means the server returned `412 Precondition Failed` --
 * another session (or another tab) changed the draft's `revision` since
 * this session last fetched it -- so further edits are blocked until the
 * draft is reloaded (see `DraftComposer.handleUpdate`).
 */
export function DraftSaveIndicator({ state }: DraftSaveIndicatorProps) {
  const { t } = useTranslation('common');

  const config: Record<DraftSaveState, { text: string; className: string }> = {
    idle: { text: '', className: '' },
    saving: {
      text: t('generation-draft-save-saving'),
      className: 'text-info',
    },
    saved: {
      text: t('generation-draft-save-saved'),
      className: 'text-success',
    },
    conflict: {
      text: t('generation-draft-save-conflict'),
      className: 'text-error',
    },
  };

  const { text, className } = config[state];
  if (!text) return null;

  return (
    <span className={`text-xs ${className}`} aria-live="polite">
      {text}
    </span>
  );
}
