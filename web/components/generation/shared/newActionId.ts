/**
 * Mints a fresh idempotency key for one command attempt (e.g.
 * `create_generation_draft`, `update_generation_draft`,
 * `submit_generation_run`). Previously duplicated across `DraftComposer.tsx`,
 * `DraftWorkspace.tsx`, `RecoveryPanel.tsx`, `NewDraftDialog.tsx` and
 * `SubmitConfirmation.tsx` -- consolidated here now that a shared, non-lateral
 * home exists for it.
 */
export function newActionId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `action_${Math.random().toString(36).slice(2)}`;
}
