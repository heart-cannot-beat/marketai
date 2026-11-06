import { useTranslation } from 'next-i18next';
import { useState } from 'react';

interface CredentialMetadata {
  id: string;
  provider: string;
  createdAt: string;
  expiresAt: string | null;
  status: string;
}

interface CredentialBindingPanelProps {
  credentials: CredentialMetadata[];
  onBind: (credentialId: string) => Promise<void>;
}

export function CredentialBindingPanel({
  credentials,
  onBind,
}: CredentialBindingPanelProps) {
  const { t } = useTranslation('library');
  const [binding, setBinding] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleBind = async (credentialId: string) => {
    setError(null);
    setSelectedId(credentialId);
    setBinding(true);
    try {
      await onBind(credentialId);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBinding(false);
      setSelectedId(null);
    }
  };

  if (credentials.length === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400">
        {t('data-source-ops.no-credentials')}
      </p>
    );
  }

  return (
    <>
      <div className="space-y-3">
        <h3 className="text-sm font-medium text-gray-900 dark:text-gray-100">
          {t('data-source-ops.credentials-title')}
        </h3>

        {error && (
          <div
            className="rounded bg-red-50 p-2 text-xs text-red-600 dark:bg-red-900/20 dark:text-red-400"
            role="alert"
          >
            {error}
          </div>
        )}

        <ul className="divide-y divide-gray-100 dark:divide-gray-800">
          {credentials.map((cred) => (
            <li
              key={cred.id}
              className="flex items-center justify-between py-2"
            >
              <div>
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                  {cred.provider}
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  {t('data-source-ops.credential-status', {
                    status: cred.status,
                  })}
                </p>
              </div>
              <button
                type="button"
                className="btn btn-primary btn-xs"
                disabled={binding && selectedId === cred.id}
                onClick={() => handleBind(cred.id)}
              >
                {binding && selectedId === cred.id
                  ? t('data-source-ops.binding')
                  : t('data-source-ops.bind')}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}
