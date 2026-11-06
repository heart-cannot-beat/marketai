import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'next-i18next';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type DuplicateGroup = components['schemas']['DuplicateGroup'];
type RemapTarget = components['schemas']['RemapTarget'];

export interface GroupResolution {
  action: string;
  reason: string;
  affectedCandidateFingerprint?: string;
  remapTargetId?: string;
}

interface DuplicateResolutionPanelProps {
  groups: DuplicateGroup[];
  onResolutionsChange: (resolutions: Map<string, GroupResolution>) => void;
  stateKey: string;
}

export function DuplicateResolutionPanel({
  groups,
  onResolutionsChange,
  stateKey,
}: DuplicateResolutionPanelProps) {
  const { t } = useTranslation('experiments');
  const [resolutions, setResolutions] = useState<Map<string, GroupResolution>>(
    () => new Map()
  );
  const [resetKey, setResetKey] = useState(stateKey);

  if (resetKey !== stateKey) {
    setResetKey(stateKey);
    setResolutions(new Map());
  }

  // Propagate every local resolution change up to the parent (which needs
  // the map to gate the freeze button and build the submitted plan) from an
  // effect rather than from inside the `setResolutions` updater above —
  // calling another component's setState from inside a state updater
  // function triggers React's "setState while rendering a different
  // component" warning, since updaters can run outside the commit phase.
  useEffect(() => {
    onResolutionsChange(resolutions);
  }, [resolutions, onResolutionsChange]);

  const updateGroup = useCallback(
    (groupHash: string, update: Partial<GroupResolution>) => {
      setResolutions((prev) => {
        const next = new Map(prev);
        const existing = next.get(groupHash) || { action: '', reason: '' };
        next.set(groupHash, { ...existing, ...update });
        return next;
      });
    },
    []
  );

  return (
    <div className="space-y-6">
      <h3 className="text-lg font-semibold">{t('duplicates.title')}</h3>
      <p className="text-sm text-gray-600">{t('duplicates.intro')}</p>
      {groups.map((group) => (
        <GroupCard
          key={group.duplicate_group_hash}
          group={group}
          resolution={resolutions.get(group.duplicate_group_hash)}
          onUpdate={(update) => updateGroup(group.duplicate_group_hash, update)}
        />
      ))}
    </div>
  );
}

function GroupCard({
  group,
  resolution,
  onUpdate,
}: {
  group: DuplicateGroup;
  resolution?: GroupResolution;
  onUpdate: (update: Partial<GroupResolution>) => void;
}) {
  const { t } = useTranslation('experiments');
  const members = group.members ?? [];
  const allowedActions = group.allowed_actions ?? [];
  const remapTargets = group.remap_targets ?? [];

  const needsAffectedCandidate =
    resolution?.action === 'exclude_source_candidate' ||
    resolution?.action === 'remap_before_freeze';
  const needsRemapTarget = resolution?.action === 'remap_before_freeze';

  return (
    <div className="rounded-lg border border-gray-200 p-4 space-y-3">
      <div className="font-medium">
        {group.dimension_key} — {group.concept_key_name}
      </div>
      <div className="text-sm text-gray-500">
        {t('duplicates.share', { count: members.length })}
      </div>
      <ul className="text-sm space-y-1">
        {members.map((m) => (
          <li
            key={m.source_candidate_fingerprint}
            className="font-mono text-xs"
          >
            {`${m.display_label} (${m.asset_version_id.slice(0, 12)}…)`}
          </li>
        ))}
      </ul>

      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">
          {t('duplicates.action')}
        </legend>
        {allowedActions.map((action) => (
          <label key={action} className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name={`action-${group.duplicate_group_hash}`}
              value={action}
              checked={resolution?.action === action}
              onChange={() =>
                onUpdate({
                  action,
                  affectedCandidateFingerprint: undefined,
                  remapTargetId: undefined,
                })
              }
            />
            {t(`duplicates.actions.${action}`, { defaultValue: action })}
          </label>
        ))}
      </fieldset>

      {needsAffectedCandidate && (
        <div className="space-y-1">
          <label
            className="text-sm font-medium"
            htmlFor={`affected-candidate-${group.duplicate_group_hash}`}
          >
            {t('duplicates.affected')}
          </label>
          <select
            id={`affected-candidate-${group.duplicate_group_hash}`}
            className="w-full rounded border px-2 py-1 text-sm"
            value={resolution?.affectedCandidateFingerprint ?? ''}
            onChange={(e) =>
              onUpdate({
                affectedCandidateFingerprint: e.target.value || undefined,
              })
            }
          >
            <option value="">{t('duplicates.select')}</option>
            {members.map((m) => (
              <option
                key={m.source_candidate_fingerprint}
                value={m.source_candidate_fingerprint}
              >
                {m.display_label}
              </option>
            ))}
          </select>
        </div>
      )}

      {needsRemapTarget && (
        <div className="space-y-1">
          <label
            className="text-sm font-medium"
            htmlFor={`remap-target-${group.duplicate_group_hash}`}
          >
            {t('duplicates.remap-to')}
          </label>
          <select
            id={`remap-target-${group.duplicate_group_hash}`}
            className="w-full rounded border px-2 py-1 text-sm"
            value={resolution?.remapTargetId ?? ''}
            onChange={(e) =>
              onUpdate({ remapTargetId: e.target.value || undefined })
            }
          >
            <option value="">{t('duplicates.select')}</option>
            {remapTargets.map((target: RemapTarget) => (
              <option key={target.concept_key_id} value={target.concept_key_id}>
                {target.concept_key_name}
              </option>
            ))}
          </select>
        </div>
      )}

      <div className="space-y-1">
        <label
          className="text-sm font-medium"
          htmlFor={`reason-${group.duplicate_group_hash}`}
        >
          {t('duplicates.reason')}
        </label>
        <textarea
          id={`reason-${group.duplicate_group_hash}`}
          className="w-full rounded border px-2 py-1 text-sm"
          rows={2}
          maxLength={2000}
          value={resolution?.reason ?? ''}
          onChange={(e) => onUpdate({ reason: e.target.value })}
          placeholder={t('duplicates.reason-placeholder')}
        />
      </div>
    </div>
  );
}
