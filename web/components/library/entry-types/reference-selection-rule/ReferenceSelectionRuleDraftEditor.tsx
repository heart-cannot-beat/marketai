import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { ReferenceSelectionRuleFields } from '@/lib/library/draft-codecs';

interface ReferenceSelectionRuleDraftEditorProps {
  session: UseDraftSessionResult;
}

export function ReferenceSelectionRuleDraftEditor({
  session,
}: ReferenceSelectionRuleDraftEditorProps) {
  const { t } = useTranslation('library');
  const fields = session.buffer?.fields as
    | ReferenceSelectionRuleFields
    | undefined;
  if (!fields) return null;

  return (
    <div className="space-y-4">
      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.reference-selection-rule.display-name')}
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

      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.reference-selection-rule.rule-expression')}
          </span>
        </div>
        <textarea
          className="textarea textarea-bordered w-full font-mono text-sm"
          rows={4}
          value={fields.rule_expression}
          onChange={(e) =>
            session.updateField('rule_expression', e.target.value)
          }
        />
      </label>
    </div>
  );
}
