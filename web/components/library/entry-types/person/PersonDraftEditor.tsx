import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { PersonFields } from '@/lib/library/draft-codecs';

interface PersonDraftEditorProps {
  session: UseDraftSessionResult;
}

export function PersonDraftEditor({ session }: PersonDraftEditorProps) {
  const { t } = useTranslation('library');
  const fields = session.buffer?.fields as PersonFields | undefined;
  if (!fields) return null;

  const fragments = fields.prompt_fragments ?? [];
  const regions = fields.rights_regions ?? [];

  return (
    <div className="space-y-4">
      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">{t('fields.person.display-name')}</span>
        </div>
        <input
          type="text"
          className="input input-bordered w-full"
          value={fields.display_name}
          onChange={(e) => session.updateField('display_name', e.target.value)}
          autoComplete="off"
        />
      </label>

      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">{t('fields.person.person-type')}</span>
        </div>
        <select
          className="select select-bordered w-full"
          value={fields.person_type}
          onChange={(e) => session.updateField('person_type', e.target.value)}
        >
          <option value="fictional">
            {t('fields.person.person-type-fictional')}
          </option>
          <option value="licensed">
            {t('fields.person.person-type-licensed')}
          </option>
        </select>
      </label>

      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.person.age-expression')}
          </span>
        </div>
        <input
          type="text"
          className="input input-bordered w-full"
          value={fields.age_expression}
          onChange={(e) =>
            session.updateField('age_expression', e.target.value)
          }
          autoComplete="off"
        />
      </label>

      <fieldset>
        <legend className="text-sm font-medium text-gray-700 dark:text-gray-300">
          {t('fields.person.prompt-fragments')}
        </legend>
        <div className="mt-2 space-y-2">
          {fragments.map((item, i) => (
            <div key={i} className="flex items-start gap-2">
              <textarea
                className="textarea textarea-bordered textarea-sm flex-1"
                rows={2}
                value={item}
                onChange={(e) => {
                  const next = [...fragments];
                  next[i] = e.target.value;
                  session.updateField('prompt_fragments', next);
                }}
                aria-label={t('fields.person.prompt-fragment-item', {
                  index: String(i + 1),
                })}
              />
              <button
                type="button"
                className="btn btn-ghost btn-xs text-red-500 mt-1"
                onClick={() => {
                  session.updateField(
                    'prompt_fragments',
                    fragments.filter((_, j) => j !== i)
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
              session.updateField('prompt_fragments', [...fragments, ''])
            }
          >
            {t('fields.person.add-prompt-fragment')}
          </button>
        </div>
      </fieldset>

      <fieldset>
        <legend className="text-sm font-medium text-gray-700 dark:text-gray-300">
          {t('fields.person.rights-regions')}
        </legend>
        <div className="mt-2 space-y-2">
          {regions.map((item, i) => (
            <div key={i} className="flex items-center gap-2">
              <input
                type="text"
                className="input input-bordered input-sm flex-1"
                value={item}
                onChange={(e) => {
                  const next = [...regions];
                  next[i] = e.target.value;
                  session.updateField('rights_regions', next);
                }}
                aria-label={t('fields.person.rights-region-item', {
                  index: String(i + 1),
                })}
              />
              <button
                type="button"
                className="btn btn-ghost btn-xs text-red-500"
                onClick={() => {
                  session.updateField(
                    'rights_regions',
                    regions.filter((_, j) => j !== i)
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
              session.updateField('rights_regions', [...regions, ''])
            }
          >
            {t('fields.person.add-rights-region')}
          </button>
        </div>
      </fieldset>
    </div>
  );
}
