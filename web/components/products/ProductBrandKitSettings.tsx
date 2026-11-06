import { useEffect, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { ReferenceThumbnail } from '@/components/library/references/ReferenceThumbnail';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ProductSummary } from '@/hooks/products/useProductDirectory';

type Content = components['schemas']['BrandKitContent'];
type Input = components['schemas']['BrandKitInput'];
type Placement = components['schemas']['BrandLogoInput']['placement'];

const PLACEMENTS: Placement[] = [
  'top_left',
  'top_right',
  'top_center',
  'bottom_left',
  'bottom_right',
  'bottom_center',
];
const MAX_COLORS = 5;
const MAX_FORBIDDEN = 8;

interface Form {
  logoItemId: string;
  placement: Placement;
  primary: string;
  secondary: string;
  palette: string[];
  textHeight: string;
  contrast: string;
  safeArea: string;
  forbidden: string;
}

function percent(ratio: number) {
  return String(Math.round(ratio * 1000) / 10);
}

export function brandKitForm(content?: Content | null): Form {
  const rules = content?.readability_rules;
  return {
    logoItemId: content?.logo?.item_id ?? '',
    placement: content?.logo?.placement ?? 'top_left',
    primary: content?.fonts.primary ?? '',
    secondary: content?.fonts.secondary ?? '',
    palette: content?.palette ?? ['#000000'],
    textHeight: percent(rules?.min_text_height_ratio ?? 0.03),
    contrast: String(rules?.min_contrast_ratio ?? 4.5),
    safeArea: percent(rules?.safe_area_ratio ?? 0.05),
    forbidden: (content?.forbidden ?? []).join('\n'),
  };
}

export function brandKitBody(form: Form): Input {
  return {
    logo: form.logoItemId
      ? { item_id: form.logoItemId, placement: form.placement }
      : null,
    fonts: { primary: form.primary.trim(), secondary: form.secondary.trim() },
    palette: form.palette.map((color) => color.toUpperCase()),
    readability_rules: {
      min_text_height_ratio: Number(form.textHeight) / 100,
      min_contrast_ratio: Number(form.contrast),
      safe_area_ratio: Number(form.safeArea) / 100,
    },
    forbidden: form.forbidden
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean),
  };
}

/** A product's versioned brand baseline. Saving appends a version; confirmed
 * plans keep the exact version they referenced, so nothing here rewrites them. */
export function ProductBrandKitSettings({
  product,
}: {
  product: ProductSummary;
}) {
  const { t } = useTranslation('products');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const canEdit = current.role === 'OWNER';
  const [opened, setOpened] = useState(false);
  const [form, setForm] = useState<Form>(brandKitForm());
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [saved, setSaved] = useState<number | null>(null);
  const query = useDomainQuery(
    'get_product_brand_kit',
    { product_id: product.product_id },
    { enabled: opened }
  );
  const references = useDomainQuery(
    'get_product_references',
    { product_id: product.product_id },
    { enabled: opened && !!product.entry_id && product.ready }
  );
  const data = query.data;
  const version = data?.current;
  // Reset the form only when another version arrives, never on a refetch of the same one.
  useEffect(() => {
    setForm(brandKitForm(version?.content));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version?.id]);
  const logos = (references.data?.items ?? []).filter(
    (item) => item.role === 'logo'
  );
  const selectedLogo = logos.find((item) => item.item_id === form.logoItemId);
  const forbiddenCount = brandKitBody(form).forbidden?.length ?? 0;
  const update = (patch: Partial<Form>) => {
    setSaved(null);
    setForm((value) => ({ ...value, ...patch }));
  };

  async function save() {
    if (!data || busy) return;
    setBusy(true);
    setFailure(null);
    try {
      const result = await gateway.saveProductBrandKit({
        workspaceSlug: current.slug,
        productId: product.product_id,
        actionInstanceId: `product-brand-kit:${crypto.randomUUID()}`,
        ifMatch: data.revision,
        body: brandKitBody(form),
      });
      if (result.kind === 'problem') {
        setFailure(result.problem.code);
        if (result.problem.code === 'stale_brand_kit_revision')
          await query.refresh();
        return;
      }
      if (result.kind === 'reconciliation_required') {
        setFailure('reconciliation_required');
        return;
      }
      setSaved(result.data.revision);
      await query.refresh();
    } catch {
      setFailure('network');
    } finally {
      setBusy(false);
    }
  }

  return (
    <details
      className="mt-3 border-t border-base-300 pt-3 text-sm"
      onToggle={(event) => setOpened(event.currentTarget.open)}
    >
      <summary className="cursor-pointer font-medium">
        {t('brand-kit.title')}
      </summary>
      {opened && (
        <div className="mt-3 space-y-3">
          <p className="text-xs text-base-content/70">
            {t('brand-kit.description')}
          </p>
          {query.isLoading && <p role="status">{t('brand-kit.loading')}</p>}
          {query.problem && (
            <div role="alert">
              <p>{t('brand-kit.load-error')}</p>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                onClick={() => void query.refresh()}
              >
                {t('retry')}
              </button>
            </div>
          )}
          {data && (
            <p className="text-xs text-base-content/60">
              {version
                ? t('brand-kit.version', { version: version.version })
                : t('brand-kit.none')}
            </p>
          )}
          {version && version.content.logo && !version.logo_available && (
            <p className="text-xs text-warning">
              {t('brand-kit.logo-unavailable')}
            </p>
          )}
          {!canEdit && <p className="text-xs">{t('brand-kit.owner-only')}</p>}
          {failure && (
            <p role="alert" className="text-error">
              {t(`brand-kit.error-${failure}`, {
                defaultValue: t('brand-kit.error-default'),
              })}
            </p>
          )}
          {saved !== null && (
            <p role="status" className="text-success">
              {t('brand-kit.saved', { version: saved })}
            </p>
          )}
          {data && (
            <fieldset disabled={!canEdit || busy} className="space-y-3">
              <legend className="sr-only">{t('brand-kit.title')}</legend>
              <label className="block text-xs">
                {t('brand-kit.logo')}
                <select
                  className="select select-bordered select-sm mt-1 w-full"
                  value={form.logoItemId}
                  onChange={(event) =>
                    update({ logoItemId: event.target.value })
                  }
                >
                  <option value="">{t('brand-kit.logo-none')}</option>
                  {logos.map((item, index) => (
                    <option
                      key={item.item_id}
                      value={item.item_id}
                      disabled={!item.available}
                    >
                      {item.note ||
                        t('brand-kit.logo-option', { index: index + 1 })}
                    </option>
                  ))}
                </select>
              </label>
              {!logos.length && (
                <p className="text-xs text-base-content/60">
                  {t('brand-kit.logo-hint')}
                </p>
              )}
              {selectedLogo && (
                <div className="w-24 overflow-hidden rounded border border-base-300">
                  <ReferenceThumbnail
                    versionId={selectedLogo.reference_version_id}
                    alt={t('brand-kit.logo')}
                  />
                </div>
              )}
              {form.logoItemId && (
                <label className="block text-xs">
                  {t('brand-kit.placement')}
                  <select
                    className="select select-bordered select-sm mt-1 w-full"
                    value={form.placement}
                    onChange={(event) =>
                      update({ placement: event.target.value as Placement })
                    }
                  >
                    {PLACEMENTS.map((placement) => (
                      <option key={placement} value={placement}>
                        {t(`brand-kit.placement-${placement}`)}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                <label className="block text-xs">
                  {t('brand-kit.primary-font')}
                  <input
                    className="input input-bordered input-sm mt-1 w-full"
                    value={form.primary}
                    maxLength={60}
                    onChange={(event) =>
                      update({ primary: event.target.value })
                    }
                  />
                </label>
                <label className="block text-xs">
                  {t('brand-kit.secondary-font')}
                  <input
                    className="input input-bordered input-sm mt-1 w-full"
                    value={form.secondary}
                    maxLength={60}
                    onChange={(event) =>
                      update({ secondary: event.target.value })
                    }
                  />
                </label>
              </div>
              <fieldset className="space-y-2">
                <legend className="text-xs">{t('brand-kit.palette')}</legend>
                <ul className="flex flex-wrap gap-2">
                  {form.palette.map((color, index) => (
                    <li key={index} className="flex items-center gap-1">
                      <input
                        type="color"
                        aria-label={t('brand-kit.color', { index: index + 1 })}
                        value={color.toLowerCase()}
                        onChange={(event) =>
                          update({
                            palette: form.palette.map((value, i) =>
                              i === index ? event.target.value : value
                            ),
                          })
                        }
                      />
                      <span className="font-mono text-xs">
                        {color.toUpperCase()}
                      </span>
                      {form.palette.length > 1 && (
                        <button
                          type="button"
                          className="btn btn-ghost btn-xs"
                          aria-label={t('brand-kit.remove-color', {
                            color: color.toUpperCase(),
                          })}
                          onClick={() =>
                            update({
                              palette: form.palette.filter(
                                (_, i) => i !== index
                              ),
                            })
                          }
                        >
                          ×
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
                {form.palette.length < MAX_COLORS && (
                  <button
                    type="button"
                    className="btn btn-ghost btn-xs"
                    onClick={() =>
                      update({ palette: [...form.palette, '#FFFFFF'] })
                    }
                  >
                    {t('brand-kit.add-color')}
                  </button>
                )}
              </fieldset>
              <fieldset className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <legend className="mb-1 text-xs">
                  {t('brand-kit.readability')}
                </legend>
                <label className="block text-xs">
                  {t('brand-kit.min-text-height')}
                  <input
                    type="number"
                    min="1"
                    max="20"
                    step="0.5"
                    className="input input-bordered input-sm mt-1 w-full"
                    value={form.textHeight}
                    onChange={(event) =>
                      update({ textHeight: event.target.value })
                    }
                  />
                </label>
                <label className="block text-xs">
                  {t('brand-kit.min-contrast')}
                  <input
                    type="number"
                    min="3"
                    max="21"
                    step="0.5"
                    className="input input-bordered input-sm mt-1 w-full"
                    value={form.contrast}
                    onChange={(event) =>
                      update({ contrast: event.target.value })
                    }
                  />
                </label>
                <label className="block text-xs">
                  {t('brand-kit.safe-area')}
                  <input
                    type="number"
                    min="0"
                    max="20"
                    step="0.5"
                    className="input input-bordered input-sm mt-1 w-full"
                    value={form.safeArea}
                    onChange={(event) =>
                      update({ safeArea: event.target.value })
                    }
                  />
                </label>
              </fieldset>
              <label className="block text-xs">
                {t('brand-kit.forbidden')}
                <textarea
                  className="textarea textarea-bordered textarea-sm mt-1 w-full"
                  rows={3}
                  value={form.forbidden}
                  onChange={(event) =>
                    update({ forbidden: event.target.value })
                  }
                />
              </label>
              {forbiddenCount > MAX_FORBIDDEN && (
                <p className="text-xs text-warning">
                  {t('brand-kit.forbidden-limit', { count: MAX_FORBIDDEN })}
                </p>
              )}
              <button
                type="button"
                className="btn btn-primary btn-sm w-full"
                disabled={
                  !canEdit ||
                  busy ||
                  !form.primary.trim() ||
                  forbiddenCount > MAX_FORBIDDEN
                }
                onClick={() => void save()}
              >
                {busy ? t('brand-kit.saving') : t('brand-kit.save')}
              </button>
            </fieldset>
          )}
        </div>
      )}
    </details>
  );
}
