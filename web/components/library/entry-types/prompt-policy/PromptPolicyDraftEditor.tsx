import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { PromptPolicyFields } from '@/lib/library/draft-codecs';

interface PromptPolicyDraftEditorProps {
  session: UseDraftSessionResult;
}

export function PromptPolicyDraftEditor({
  session,
}: PromptPolicyDraftEditorProps) {
  const { t } = useTranslation('library');
  const fields = session.buffer?.fields as PromptPolicyFields | undefined;
  if (!fields) return null;

  const tokens = fields.registered_tokens ?? [];
  const phrases = fields.locked_phrases ?? [];

  return (
    <div className="space-y-4">
      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.prompt-policy.display-name')}
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
          {t('fields.prompt-policy.registered-tokens')}
        </legend>
        <div className="mt-2 space-y-2">
          {tokens.map((item, i) => (
            <div key={i} className="flex items-center gap-2">
              <input
                type="text"
                className="input input-bordered input-sm flex-1"
                value={item}
                onChange={(e) => {
                  const next = [...tokens];
                  next[i] = e.target.value;
                  session.updateField('registered_tokens', next);
                }}
                aria-label={t('fields.prompt-policy.token-item', {
                  index: String(i + 1),
                })}
              />
              <button
                type="button"
                className="btn btn-ghost btn-xs text-red-500"
                onClick={() => {
                  session.updateField(
                    'registered_tokens',
                    tokens.filter((_, j) => j !== i)
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
              session.updateField('registered_tokens', [...tokens, ''])
            }
          >
            {t('fields.prompt-policy.add-token')}
          </button>
        </div>
      </fieldset>

      <fieldset>
        <legend className="text-sm font-medium text-gray-700 dark:text-gray-300">
          {t('fields.prompt-policy.locked-phrases')}
        </legend>
        <div className="mt-2 space-y-2">
          {phrases.map((item, i) => (
            <div key={i} className="flex items-center gap-2">
              <input
                type="text"
                className="input input-bordered input-sm flex-1"
                value={item}
                onChange={(e) => {
                  const next = [...phrases];
                  next[i] = e.target.value;
                  session.updateField('locked_phrases', next);
                }}
                aria-label={t('fields.prompt-policy.phrase-item', {
                  index: String(i + 1),
                })}
              />
              <button
                type="button"
                className="btn btn-ghost btn-xs text-red-500"
                onClick={() => {
                  session.updateField(
                    'locked_phrases',
                    phrases.filter((_, j) => j !== i)
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
              session.updateField('locked_phrases', [...phrases, ''])
            }
          >
            {t('fields.prompt-policy.add-phrase')}
          </button>
        </div>
      </fieldset>

      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.prompt-policy.brand-tone')}
          </span>
        </div>
        <textarea
          className="textarea textarea-bordered w-full"
          rows={3}
          value={fields.brand_tone}
          onChange={(e) => session.updateField('brand_tone', e.target.value)}
        />
      </label>
    </div>
  );
}
