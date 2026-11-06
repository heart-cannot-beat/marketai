import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { OfferFields } from '@/lib/library/draft-codecs';

interface OfferDraftEditorProps {
  session: UseDraftSessionResult;
}

export function OfferDraftEditor({ session }: OfferDraftEditorProps) {
  const { t } = useTranslation('library');
  const fields = session.buffer?.fields as OfferFields | undefined;
  if (!fields) return null;

  return (
    <div className="space-y-4">
      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">{t('fields.offer.display-name')}</span>
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
          <span className="label-text">{t('fields.offer.item-semantics')}</span>
        </div>
        <input
          type="text"
          className="input input-bordered w-full"
          value={fields.item_semantics}
          onChange={(e) =>
            session.updateField('item_semantics', e.target.value)
          }
          autoComplete="off"
        />
      </label>

      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.offer.market-semantics')}
          </span>
        </div>
        <input
          type="text"
          className="input input-bordered w-full"
          value={fields.market_semantics}
          onChange={(e) =>
            session.updateField('market_semantics', e.target.value)
          }
          autoComplete="off"
        />
      </label>

      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.offer.claim-semantics')}
          </span>
        </div>
        <input
          type="text"
          className="input input-bordered w-full"
          value={fields.claim_semantics}
          onChange={(e) =>
            session.updateField('claim_semantics', e.target.value)
          }
          autoComplete="off"
        />
      </label>

      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">{t('fields.offer.date-semantics')}</span>
        </div>
        <input
          type="text"
          className="input input-bordered w-full"
          value={fields.date_semantics}
          onChange={(e) =>
            session.updateField('date_semantics', e.target.value)
          }
          autoComplete="off"
        />
      </label>
    </div>
  );
}
