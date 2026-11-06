import { useTranslation } from 'next-i18next';

interface PromptPolicyVersionViewerProps {
  version: Record<string, unknown>;
}

export function PromptPolicyVersionViewer({
  version,
}: PromptPolicyVersionViewerProps) {
  const { t } = useTranslation('library');
  const tokens = (version.registered_tokens as string[]) ?? [];
  const phrases = (version.locked_phrases as string[]) ?? [];

  return (
    <dl className="space-y-3">
      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.prompt-policy.display-name')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.display_name as string) || t('fields.version.no-value')}
        </dd>
      </div>

      {tokens.length > 0 && (
        <div>
          <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
            {t('fields.prompt-policy.registered-tokens')}
          </dt>
          <dd className="mt-1">
            <ul className="list-disc pl-4 text-sm text-gray-900 dark:text-gray-100">
              {tokens.map((tok, i) => (
                <li key={i}>{tok}</li>
              ))}
            </ul>
          </dd>
        </div>
      )}

      {phrases.length > 0 && (
        <div>
          <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
            {t('fields.prompt-policy.locked-phrases')}
          </dt>
          <dd className="mt-1">
            <ul className="list-disc pl-4 text-sm text-gray-900 dark:text-gray-100">
              {phrases.map((ph, i) => (
                <li key={i}>{ph}</li>
              ))}
            </ul>
          </dd>
        </div>
      )}

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.prompt-policy.brand-tone')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100 whitespace-pre-wrap">
          {(version.brand_tone as string) || t('fields.version.no-value')}
        </dd>
      </div>
    </dl>
  );
}
