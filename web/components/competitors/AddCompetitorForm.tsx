import { useId, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { dataOf, useCommand, type Competitor } from './shared';

const BUTTON =
  'rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40';
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm focus:border-gray-600 focus:outline-none focus:ring-2 focus:ring-gray-200';

export type InputKind =
  | 'ad_library'
  | 'facebook_id'
  | 'facebook_alias'
  | 'website'
  | 'name';

/**
 * Which of the four §4.1 inputs a value is, so the form can say what happens
 * next. The server parses the link again; this only drives the hint and moves
 * a bare website into its own field (a website alone cannot be searched).
 */
export function inputKindOf(value: string): InputKind | null {
  const text = value.trim();
  if (!text) return null;
  const facebook = /^(?:https?:\/\/)?(?:[a-z0-9-]+\.)?(?:facebook|fb)\.com\//i;
  if (facebook.test(text)) {
    if (/[?&]view_all_page_id=\d+/.test(text)) return 'ad_library';
    if (/profile\.php\?(?:.*&)?id=\d+/.test(text)) return 'facebook_id';
    return 'facebook_alias';
  }
  if (/^(?:https?:\/\/)?[^\s/]+\.[a-z]{2,}(?:[/?#]\S*)?$/i.test(text))
    return 'website';
  return 'name';
}

export function AddCompetitorForm({
  productId,
  onAdded,
}: {
  productId: string;
  onAdded: (competitor: Competitor) => Promise<void>;
}) {
  const { t } = useTranslation('competitors');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const command = useCommand();
  const ids = { input: useId(), website: useId(), role: useId() };
  const [input, setInput] = useState('');
  const [website, setWebsite] = useState('');
  const [role, setRole] = useState<'competitor' | 'self'>('competitor');
  const [movedWebsite, setMovedWebsite] = useState(false);
  const kind = inputKindOf(input);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (kind === 'website') {
      // Keep the site for the domain check and ask for the name to search.
      if (!website.trim()) setWebsite(input.trim());
      setInput('');
      setMovedWebsite(true);
      return;
    }
    const body = {
      product_id: productId,
      input: input.trim(),
      website: website.trim() || null,
      role,
    };
    const added = await command.run(
      `create:${JSON.stringify(body)}`,
      async (action) =>
        dataOf(
          await gateway.createCompetitor({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            body,
          })
        )
    );
    if (added) {
      setInput('');
      setWebsite('');
      setRole('competitor');
      setMovedWebsite(false);
      await onAdded(added);
    }
  }

  return (
    <form
      className="space-y-3 rounded-lg border border-gray-200 bg-white p-4"
      onSubmit={(e) => void submit(e)}
      data-testid="competitor-add-form"
    >
      <div className="space-y-1 text-sm">
        <label htmlFor={ids.input} className="font-medium">
          {t('add-input-label')}
        </label>
        <input
          id={ids.input}
          className={INPUT}
          value={input}
          placeholder={t('add-input-placeholder')}
          onChange={(e) => {
            setInput(e.target.value);
            setMovedWebsite(false);
          }}
          disabled={!!command.busy}
          aria-describedby={`${ids.input}-hint`}
        />
        <p
          id={`${ids.input}-hint`}
          className="text-xs text-gray-500"
          data-testid="competitor-input-kind"
          data-kind={kind ?? ''}
        >
          {movedWebsite
            ? t('add-kind-website-moved')
            : kind
              ? t(`add-kind-${kind}`)
              : t('add-input-hint')}
        </p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1 text-sm">
          <label htmlFor={ids.website} className="font-medium">
            {t('add-website-label')}
          </label>
          <input
            id={ids.website}
            className={INPUT}
            value={website}
            inputMode="url"
            placeholder="example.com"
            onChange={(e) => setWebsite(e.target.value)}
            disabled={!!command.busy}
          />
          <p className="text-xs text-gray-500">{t('add-website-hint')}</p>
        </div>
        <div className="space-y-1 text-sm">
          <label htmlFor={ids.role} className="font-medium">
            {t('add-role-label')}
          </label>
          <select
            id={ids.role}
            className={INPUT}
            value={role}
            onChange={(e) => setRole(e.target.value as 'competitor' | 'self')}
            disabled={!!command.busy}
          >
            <option value="competitor">{t('role-competitor')}</option>
            <option value="self">{t('role-self')}</option>
          </select>
          <p className="text-xs text-gray-500">{t('add-role-hint')}</p>
        </div>
      </div>
      {command.errorFor('create:') && (
        <p className="text-sm text-red-700" role="alert">
          {command.errorFor('create:')}
        </p>
      )}
      <button
        type="submit"
        className={BUTTON}
        disabled={!!command.busy || !kind}
      >
        {kind === 'website' ? t('add-use-as-website') : t('add-submit')}
      </button>
    </form>
  );
}
