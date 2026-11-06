import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { ProductFields } from '@/lib/library/draft-codecs';

interface ProductDraftEditorProps {
  session: UseDraftSessionResult;
}

export function ProductDraftEditor({ session }: ProductDraftEditorProps) {
  const { t } = useTranslation('library');
  const fields = session.buffer?.fields as ProductFields | undefined;
  if (!fields) return null;

  const points = fields.selling_points ?? [];

  return (
    <div className="space-y-4">
      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">{t('fields.product.name')}</span>
        </div>
        <input
          type="text"
          className="input input-bordered w-full"
          value={fields.name ?? ''}
          onChange={(e) => session.updateField('name', e.target.value)}
          autoComplete="off"
        />
      </label>

      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">{t('fields.product.description')}</span>
        </div>
        <textarea
          className="textarea textarea-bordered w-full"
          rows={3}
          value={fields.description ?? ''}
          onChange={(e) => session.updateField('description', e.target.value)}
        />
      </label>

      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">{t('fields.product.audience')}</span>
        </div>
        <textarea
          className="textarea textarea-bordered w-full"
          rows={2}
          value={fields.audience ?? ''}
          onChange={(e) => session.updateField('audience', e.target.value)}
        />
      </label>

      <fieldset>
        <legend className="text-sm font-medium text-gray-700 dark:text-gray-300">
          {t('fields.product.selling-points')}
        </legend>
        <div className="mt-2 space-y-2">
          {points.map((item, i) => (
            <div key={i} className="flex items-center gap-2">
              <input
                type="text"
                className="input input-bordered input-sm flex-1"
                value={item}
                onChange={(e) => {
                  const next = [...points];
                  next[i] = e.target.value;
                  session.updateField('selling_points', next);
                }}
                aria-label={t('fields.product.selling-point-item', {
                  index: String(i + 1),
                })}
              />
              <button
                type="button"
                className="btn btn-ghost btn-xs text-red-500"
                onClick={() => {
                  session.updateField(
                    'selling_points',
                    points.filter((_, j) => j !== i)
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
              session.updateField('selling_points', [...points, ''])
            }
          >
            {t('fields.product.add-selling-point')}
          </button>
        </div>
      </fieldset>
    </div>
  );
}
