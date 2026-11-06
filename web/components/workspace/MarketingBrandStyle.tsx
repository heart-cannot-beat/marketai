import { useId } from 'react';
import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { VOCABULARY, strategyVocabulary } from '@/lib/i18n/vocabulary';

type Plan = NonNullable<
  components['schemas']['MarketingProjectDetail']['plan']
>;
type BrandKit = components['schemas']['BrandKitVersion'];
type Variation = Plan['style_variation'];

const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm focus:border-gray-600 focus:outline-none focus:ring-2 focus:ring-gray-200';

/** Mirrors the server's label match: either language names the same factor. */
export function mentionsStyle(text: string) {
  const lower = text.toLowerCase();
  return Object.values(VOCABULARY).some((words) =>
    lower.includes(words['plan-label-visual-style'].toLowerCase())
  );
}

/** Moves "visual style" between the held and changed lists, so the declared
 * comparison always says what the choice means (spec §6.1). The label is in
 * the plan's strategy language, whatever the screen's language is. */
export function withStyleVariation(plan: Plan, variation: Variation): Plan {
  const label = strategyVocabulary(plan.strategy_language)[
    'plan-label-visual-style'
  ];
  const changed = plan.changed.filter((item) => !mentionsStyle(item));
  const held = plan.held_constant.filter((item) => !mentionsStyle(item));
  return {
    ...plan,
    style_variation: variation,
    changed: variation === 'tested' ? [...changed, label] : changed,
    held_constant: variation === 'held' ? [...held, label] : held,
    groups: plan.groups.map((group) => ({
      ...group,
      style: variation === 'held' ? '' : (group.style ?? ''),
    })),
  };
}

/** Refusals that withStyleVariation resolves for the plan's own choice. A
 * draft can carry them from before the server wrote its style fields (#354). */
const STYLE_FIXES = [
  'style_not_held',
  'group_style_not_held',
  'style_variation_undeclared',
];

/** Plans drafted before the brand baseline carry no style choice; a save records the held default. */
export function styleBody(plan: Plan) {
  return {
    style_variation: plan.style_variation ?? 'held',
    style_notes: plan.style_notes ?? '',
    groups: plan.groups.map((group) => ({
      ...group,
      style: group.style ?? '',
    })),
  };
}

function StyleField({
  label,
  value,
  onChange,
  path,
  errors,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  path: string;
  errors: string[];
  disabled: boolean;
}) {
  const id = useId();
  return (
    <div data-validation-field={path} className="block space-y-1.5 text-sm">
      <label htmlFor={id} className="font-medium text-gray-700">
        {label}
      </label>
      <textarea
        id={id}
        className={INPUT}
        rows={2}
        disabled={disabled}
        aria-invalid={errors.length > 0 || undefined}
        aria-describedby={errors.length ? `${id}-errors` : undefined}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      {errors.length > 0 && (
        <p id={`${id}-errors`} className="text-sm text-red-700">
          {errors.join(' ')}
        </p>
      )}
    </div>
  );
}

function Summary({ kit }: { kit: BrandKit }) {
  const { t } = useTranslation('common');
  const content = kit.content;
  return (
    <div className="space-y-1 rounded-lg bg-gray-50 p-3 text-xs text-gray-700">
      <ul
        className="flex flex-wrap gap-2"
        aria-label={t('marketing-brand-kit-palette')}
      >
        {content.palette.map((color) => (
          <li key={color} className="flex items-center gap-1">
            <span
              className="inline-block h-4 w-4 rounded border border-gray-300"
              style={{ backgroundColor: color }}
              aria-hidden="true"
            />
            <span className="font-mono">{color}</span>
          </li>
        ))}
      </ul>
      <p>
        {t('marketing-brand-kit-summary-fonts', {
          fonts: [content.fonts.primary, content.fonts.secondary]
            .filter(Boolean)
            .join(' / '),
        })}
      </p>
      <p>
        {content.logo
          ? t('marketing-brand-kit-summary-logo', {
              placement: t(
                `marketing-logo-placement-${content.logo.placement}`
              ),
            })
          : t('marketing-brand-kit-summary-no-logo')}
      </p>
      {!!content.forbidden?.length && (
        <p>
          {t('marketing-brand-kit-summary-forbidden', {
            items: content.forbidden.join(t('list-separator')),
          })}
        </p>
      )}
      {content.logo && !kit.logo_available && (
        <p className="text-amber-800">
          {t('marketing-brand-kit-logo-unavailable')}
        </p>
      )}
    </div>
  );
}

/** The plan's exact brand baseline version and its style rule. */
export function MarketingBrandStyle({
  plan,
  frozen,
  onChange,
  fieldMessages,
  errorCodes = [],
  disabled,
}: {
  plan: Plan;
  frozen: BrandKit | null | undefined;
  onChange: (plan: Plan) => void;
  fieldMessages: (path: string) => string[];
  /** The field error codes the last save or confirmation was refused with. */
  errorCodes?: string[];
  disabled: boolean;
}) {
  const { t } = useTranslation('common');
  const kits = useDomainQuery('list_marketing_brand_kits');
  const items = kits.data?.items ?? [];
  const variation: Variation = plan.style_variation ?? 'held';
  const fixable = errorCodes.some((code) => STYLE_FIXES.includes(code));
  // Held hides each group's style field, so the fix names what it clears.
  const cleared =
    variation === 'held'
      ? plan.groups
          .map((group, index) => ({ group, index }))
          .filter(({ group }) => group.style?.trim())
      : [];
  const selectedId = plan.brand_kit?.id ?? '';
  const listed = items.find((item) => item.current.id === selectedId);
  const shown =
    frozen && frozen.id === selectedId ? frozen : (listed?.current ?? null);
  const newer = plan.brand_kit
    ? items.find(
        (item) =>
          item.product_id === plan.brand_kit!.product_id &&
          item.current.version > plan.brand_kit!.version
      )
    : undefined;
  const choose = (id: string) => {
    const item = items.find((entry) => entry.current.id === id);
    onChange({
      ...plan,
      brand_kit: item
        ? {
            id: item.current.id,
            product_id: item.current.product_id,
            version: item.current.version,
            content_hash: item.current.content_hash,
          }
        : null,
    });
  };
  return (
    <section
      className="space-y-4 border-t border-gray-200 pt-5"
      aria-labelledby="marketing-brand-style"
    >
      <h3 id="marketing-brand-style" className="font-medium">
        {t('marketing-brand-and-style')}
      </h3>
      <p className="text-sm text-gray-600">
        {t('marketing-brand-and-style-intro')}
      </p>
      <div data-validation-field="plan.brand_kit" className="space-y-2 text-sm">
        <label className="block space-y-1.5">
          <span className="font-medium text-gray-700">
            {t('marketing-brand-kit')}
          </span>
          <select
            className={INPUT}
            value={selectedId}
            disabled={disabled}
            onChange={(e) => choose(e.target.value)}
          >
            <option value="">{t('marketing-brand-kit-none')}</option>
            {plan.brand_kit && !listed && (
              <option value={plan.brand_kit.id}>
                {t('marketing-brand-kit-frozen-option', {
                  version: plan.brand_kit.version,
                })}
              </option>
            )}
            {items.map((item) => (
              <option key={item.current.id} value={item.current.id}>
                {t('marketing-brand-kit-option', {
                  product: item.product_name || item.product_id,
                  version: item.current.version,
                })}
              </option>
            ))}
          </select>
        </label>
        {kits.problem && (
          <p role="alert" className="text-red-700">
            {t('marketing-brand-kit-load-error')}
          </p>
        )}
        {fieldMessages('plan.brand_kit').map((message) => (
          <p key={message} className="text-red-700">
            {message}
          </p>
        ))}
        {shown && <Summary kit={shown} />}
        {newer && (
          <p className="text-amber-800">
            {t('marketing-brand-kit-newer', { version: newer.current.version })}
          </p>
        )}
      </div>
      <fieldset
        data-validation-field="plan.style_variation"
        className="space-y-2 text-sm"
        disabled={disabled}
      >
        <legend className="font-medium text-gray-700">
          {t('marketing-style-variation')}
        </legend>
        {(['held', 'tested'] as const).map((value) => (
          <label key={value} className="flex items-start gap-2">
            <input
              type="radio"
              name="marketing-style-variation"
              checked={variation === value}
              onChange={() => onChange(withStyleVariation(plan, value))}
            />
            <span>{t(`marketing-style-${value}`)}</span>
          </label>
        ))}
        <p className="text-gray-600">
          {t(`marketing-style-variation-note-${variation}`)}
        </p>
        {fieldMessages('plan.style_variation').map((message) => (
          <p key={message} className="text-red-700">
            {message}
          </p>
        ))}
      </fieldset>
      {fixable && (
        <div
          role="note"
          className="space-y-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm"
        >
          <p>{t(`marketing-style-fix-${variation}`)}</p>
          {cleared.length > 0 && (
            <>
              <p>{t('marketing-style-fix-clears')}</p>
              <ul className="list-disc pl-5">
                {cleared.map(({ group, index }) => (
                  <li
                    key={group.id}
                    data-validation-field={`plan.groups.${index}.style`}
                  >
                    {t('marketing-style-fix-group', {
                      group: group.name,
                      style: group.style,
                    })}
                  </li>
                ))}
              </ul>
            </>
          )}
          <button
            type="button"
            className="rounded border border-gray-400 bg-white px-3 py-2 disabled:opacity-40"
            disabled={disabled}
            onClick={() => onChange(withStyleVariation(plan, variation))}
          >
            {t('marketing-style-fix')}
          </button>
        </div>
      )}
      <StyleField
        label={t(`marketing-style-notes-${variation}`)}
        value={plan.style_notes ?? ''}
        path="plan.style_notes"
        errors={fieldMessages('plan.style_notes')}
        disabled={disabled}
        onChange={(value) => onChange({ ...plan, style_notes: value })}
      />
      {variation === 'tested' &&
        plan.groups.map((group, index) => (
          <StyleField
            key={group.id}
            label={t('marketing-group-style', { group: group.name })}
            value={group.style ?? ''}
            path={`plan.groups.${index}.style`}
            errors={fieldMessages(`plan.groups.${index}.style`)}
            disabled={disabled}
            onChange={(value) =>
              onChange({
                ...plan,
                groups: plan.groups.map((g) =>
                  g.id === group.id ? { ...g, style: value } : g
                ),
              })
            }
          />
        ))}
    </section>
  );
}
