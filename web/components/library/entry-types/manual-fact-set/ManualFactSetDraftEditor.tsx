import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { ManualFactSetFields } from '@/lib/library/draft-codecs';

interface ManualFactSetDraftEditorProps {
  session: UseDraftSessionResult;
}

function newFactId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `fact_${Math.random().toString(36).slice(2)}`;
}

export function ManualFactSetDraftEditor({
  session,
}: ManualFactSetDraftEditorProps) {
  const { t } = useTranslation('library');
  const fields = session.buffer?.fields as ManualFactSetFields | undefined;
  if (!fields) return null;

  const facts = fields.facts ?? [];

  return (
    <div className="space-y-4">
      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.manual-fact-set.display-name')}
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
          {t('fields.manual-fact-set.facts')}
        </legend>
        <div className="mt-2 space-y-3">
          {facts.map((fact, i) => (
            <div
              key={fact.id}
              className="rounded-lg border border-gray-200 p-3 dark:border-gray-700"
            >
              <div className="flex items-start gap-2">
                <div className="flex-1 space-y-2">
                  <label className="form-control w-full">
                    <div className="label">
                      <span className="label-text text-xs">
                        {t('fields.manual-fact-set.fact-key')}
                      </span>
                    </div>
                    <input
                      type="text"
                      className="input input-bordered input-sm w-full"
                      value={fact.key}
                      onChange={(e) => {
                        const next = [...facts];
                        next[i] = { ...fact, key: e.target.value };
                        session.updateField('facts', next);
                      }}
                      autoComplete="off"
                    />
                  </label>
                  <label className="form-control w-full">
                    <div className="label">
                      <span className="label-text text-xs">
                        {t('fields.manual-fact-set.fact-value')}
                      </span>
                    </div>
                    <textarea
                      className="textarea textarea-bordered textarea-sm w-full"
                      rows={2}
                      value={fact.value}
                      onChange={(e) => {
                        const next = [...facts];
                        next[i] = { ...fact, value: e.target.value };
                        session.updateField('facts', next);
                      }}
                    />
                  </label>
                </div>
                <button
                  type="button"
                  className="btn btn-ghost btn-xs text-red-500 mt-6"
                  onClick={() => {
                    session.updateField(
                      'facts',
                      facts.filter((_, j) => j !== i)
                    );
                  }}
                  aria-label={t('fields.remove-item')}
                >
                  ×
                </button>
              </div>
            </div>
          ))}
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={() => {
              session.updateField('facts', [
                ...facts,
                { id: newFactId(), key: '', value: '' },
              ]);
            }}
          >
            {t('fields.manual-fact-set.add-fact')}
          </button>
        </div>
      </fieldset>
    </div>
  );
}
