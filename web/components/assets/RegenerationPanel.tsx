import { useCallback, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useSWRConfig } from 'swr';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import {
  matchesOperation,
  matchesWorkspace,
} from '@/lib/domain-client/query-keys';
import type { DomainQueryKey } from '@/lib/domain-client/query-keys';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { errorMessage } from '@/lib/i18n/domain-errors';

type RegenerationIntent =
  | 'same_recipe'
  | 'new_seed'
  | 'edit_prompt'
  | 'quality_feedback';

interface RegenerationPanelProps {
  versionId: string;
  onDraftCreated?: (draftId: string) => void;
  latestQcAttemptId?: string | null;
}

const INTENTS: Array<{ value: RegenerationIntent; labelKey: string }> = [
  { value: 'same_recipe', labelKey: 'intent-label-same-recipe' },
  { value: 'new_seed', labelKey: 'intent-label-new-seed' },
  { value: 'edit_prompt', labelKey: 'intent-label-edit-prompt' },
  { value: 'quality_feedback', labelKey: 'intent-label-quality-feedback' },
];

export function RegenerationPanel({
  versionId,
  onDraftCreated,
  latestQcAttemptId = null,
}: RegenerationPanelProps) {
  const { t } = useTranslation('assets');
  const command = useDomainCommand('create_generation_regeneration_draft');
  const [submitting, setSubmitting] = useState<RegenerationIntent | null>(null);
  const { mutate: globalMutate } = useSWRConfig();
  const { current } = useWorkspace();

  const visibleIntents = INTENTS.filter(
    ({ value }) => value !== 'quality_feedback' || latestQcAttemptId != null
  );

  const handleRegenerate = useCallback(
    async (intent: RegenerationIntent) => {
      setSubmitting(intent);
      const actionInstanceId = `regen:${versionId}:${intent}:${Date.now()}`;
      const outcome = await command.execute({
        pathParams: { version_id: versionId },
        body: {
          intent,
          ...(intent === 'quality_feedback'
            ? { qc_attempt_id: latestQcAttemptId }
            : {}),
        } as any,
        actionInstanceId,
      });
      setSubmitting(null);

      if (outcome.kind === 'success') {
        // A regeneration draft shows up both in the gallery (once
        // resolved into a new version) and in the drafts list -- revalidate
        // both rather than leaving either on stale cached data.
        globalMutate(
          (key: unknown) => {
            if (!Array.isArray(key) || key[0] !== 'domain') return false;
            const dk = key as unknown as DomainQueryKey;
            if (!matchesWorkspace(dk, current.slug)) return false;
            return (
              matchesOperation(dk, 'list_generation_assets') ||
              matchesOperation(dk, 'list_generation_drafts')
            );
          },
          undefined,
          { revalidate: true }
        );

        if (onDraftCreated) {
          onDraftCreated((outcome.data as any).draft_id);
        }
      }
    },
    [
      versionId,
      command,
      onDraftCreated,
      globalMutate,
      current.slug,
      latestQcAttemptId,
    ]
  );

  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-base-content/50">
        {t('regenerate')}
      </span>
      <div className="grid grid-cols-2 gap-1.5">
        {visibleIntents.map(({ value, labelKey }) => (
          <button
            key={value}
            type="button"
            className="btn btn-xs btn-outline"
            onClick={() => handleRegenerate(value)}
            disabled={submitting !== null}
            aria-label={t(labelKey)}
            data-testid={`regenerate-${value}`}
          >
            {submitting === value ? (
              <span className="loading loading-spinner loading-xs" />
            ) : (
              t(labelKey)
            )}
          </button>
        ))}
      </div>

      {command.problem && (
        <div role="alert" className="text-xs text-error">
          {errorMessage(t, command.problem)}
        </div>
      )}
    </div>
  );
}
