import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { TruthResolutionPolicyFields } from '@/lib/library/draft-codecs';

interface TruthResolutionPolicyDraftEditorProps {
  session: UseDraftSessionResult;
}

export function TruthResolutionPolicyDraftEditor({
  session,
}: TruthResolutionPolicyDraftEditorProps) {
  const { t } = useTranslation('library');
  const fields = session.buffer?.fields as
    | TruthResolutionPolicyFields
    | undefined;
  if (!fields) return null;

  const priorities = fields.priority_order ?? [];

  return (
    <div className="space-y-4">
      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.truth-resolution-policy.display-name')}
          </span>
        </div>
        <input
          type="text"
          className="input input-bordered w-full"
          value={fields.display_name}
          onChange={(e) => session.updateField('display_name', e.target.value)}
          autoComplete="off"
        />
      </label>

      <fieldset>
        <legend className="text-sm font-medium text-gray-700 dark:text-gray-300">
          {t('fields.truth-resolution-policy.priority-order')}
        </legend>
        <div className="mt-2 space-y-2">
          {priorities.map((item, i) => (
            <div key={i} className="flex items-center gap-2">
              <input
                type="text"
                className="input input-bordered input-sm flex-1"
                value={item}
                onChange={(e) => {
                  const next = [...priorities];
                  next[i] = e.target.value;
                  session.updateField('priority_order', next);
                }}
                aria-label={t('fields.truth-resolution-policy.priority-item', {
                  index: String(i + 1),
                })}
              />
              <button
                type="button"
                className="btn btn-ghost btn-xs text-red-500"
                onClick={() => {
                  session.updateField(
                    'priority_order',
                    priorities.filter((_, j) => j !== i)
                  );
                }}
                aria-label={t('fields.remove-item')}
              >
                ×
              </button>
            </div>
          ))}
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={() =>
              session.updateField('priority_order', [...priorities, ''])
            }
          >
            {t('fields.truth-resolution-policy.add-priority')}
          </button>
        </div>
      </fieldset>

      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.truth-resolution-policy.conflict-strategy')}
          </span>
        </div>
        <select
          className="select select-bordered w-full"
          value={fields.conflict_strategy}
          onChange={(e) =>
            session.updateField('conflict_strategy', e.target.value)
          }
        >
          <option value="">{t('fields.version.no-value')}</option>
          <option value="first_wins">
            {t('fields.truth-resolution-policy.strategy-first-wins')}
          </option>
          <option value="last_wins">
            {t('fields.truth-resolution-policy.strategy-last-wins')}
          </option>
          <option value="manual_review">
            {t('fields.truth-resolution-policy.strategy-manual-review')}
          </option>
        </select>
      </label>
    </div>
  );
}
