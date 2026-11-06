import { useState } from 'react';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';
import toast from 'react-hot-toast';
import { Button } from 'react-daisyui';

import { Card } from '@/components/shared';
import { SUPPORTED_LOCALES, isSupportedLocale } from '@/lib/i18n/locales';
import { VOCABULARY } from '@/lib/i18n/vocabulary';
import { reloadInLocale, saveLocalePreference } from '@/lib/i18n/switch-locale';

/** `''` in the select stands for "follow the browser" (a null preference). */
const FOLLOW_BROWSER = '';

const UpdateLanguage = ({ locale }: { locale: string | null }) => {
  const { t } = useTranslation('common');
  const router = useRouter();
  const saved = isSupportedLocale(locale) ? locale : FOLLOW_BROWSER;
  const [value, setValue] = useState<string>(saved);
  const [saving, setSaving] = useState(false);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    const next = isSupportedLocale(value) ? value : null;
    setSaving(true);
    try {
      await saveLocalePreference(next);
      reloadInLocale(next, router.asPath);
    } catch {
      toast.error(t('language-save-failed'));
      setSaving(false);
    }
  };

  return (
    <form onSubmit={save}>
      <Card>
        <Card.Body>
          <Card.Header>
            <Card.Title>{t('language')}</Card.Title>
            <Card.Description>{t('language-description')}</Card.Description>
          </Card.Header>
          <select
            name="locale"
            aria-label={t('language')}
            data-testid="account-language"
            className="select select-bordered w-full max-w-md"
            value={value}
            onChange={(event) => setValue(event.target.value)}
          >
            <option value={FOLLOW_BROWSER}>
              {t('language-follow-browser')}
            </option>
            {SUPPORTED_LOCALES.map((option) => (
              <option key={option} value={option} lang={option}>
                {VOCABULARY[option]['language-self-name']}
              </option>
            ))}
          </select>
        </Card.Body>
        <Card.Footer>
          <Button
            type="submit"
            color="primary"
            loading={saving}
            disabled={saving || value === saved}
            size="md"
          >
            {t('save-changes')}
          </Button>
        </Card.Footer>
      </Card>
    </form>
  );
};

export default UpdateLanguage;
