import { useCallback, useState } from 'react';
import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { usePublishedReferenceAssets } from '@/hooks/library/usePublishedReferenceAssets';
import { ReferenceUploadZone } from '@/components/library/references/ReferenceUploadZone';
import { ReferenceThumbnail } from '@/components/library/references/ReferenceThumbnail';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ProductSummary } from '@/hooks/products/useProductDirectory';

type Item = components['schemas']['ProductReferenceItem'];
const ROLES: Item['role'][] = [
  'primary',
  'detail',
  'logo',
  'certification_mark',
];

function ReferenceItem({
  item,
  disabled,
  needsModel,
  update,
  remove,
}: {
  item: Item;
  disabled: boolean;
  needsModel: boolean;
  update: (
    item: Item,
    body: {
      role?: Item['role'];
      identity_status?: Item['identity_status'];
      note?: string;
    }
  ) => void;
  remove: (item: Item) => void;
}) {
  const { t } = useTranslation('products');
  return (
    <li className="space-y-2 rounded border border-base-300 p-2">
      <div className="mx-auto aspect-square w-32 overflow-hidden rounded">
        <ReferenceThumbnail
          versionId={item.reference_version_id}
          alt={t(`references.role-${item.role}`)}
        />
      </div>
      {!item.available && (
        <p className="text-xs text-warning">{t('references.unavailable')}</p>
      )}
      <label className="block text-xs">
        {t('references.role')}
        <select
          className="select select-bordered select-sm mt-1 w-full"
          value={item.role}
          disabled={disabled || needsModel}
          onChange={(event) =>
            update(item, { role: event.target.value as Item['role'] })
          }
        >
          {ROLES.map((role) => (
            <option key={role} value={role}>
              {t(`references.role-${role}`)}
            </option>
          ))}
        </select>
      </label>
      <label className="block text-xs">
        {t('references.identity')}
        <select
          className="select select-bordered select-sm mt-1 w-full"
          value={item.identity_status}
          disabled={disabled || needsModel}
          onChange={(event) =>
            update(item, {
              identity_status: event.target.value as Item['identity_status'],
            })
          }
        >
          <option value="unverified">{t('references.unverified')}</option>
          <option value="verified_physical">{t('references.verified')}</option>
        </select>
      </label>
      {needsModel && (
        <p className="text-xs text-warning">{t('references.model-needed')}</p>
      )}
      <label className="block text-xs">
        {t('references.note')}
        <textarea
          className="textarea textarea-bordered textarea-sm mt-1 w-full"
          disabled={disabled}
          defaultValue={item.note}
          maxLength={1200}
          rows={2}
          onBlur={(event) => {
            if (event.target.value !== item.note)
              update(item, { note: event.target.value });
          }}
        />
      </label>
      <button
        type="button"
        className="btn btn-ghost btn-xs"
        disabled={disabled}
        onClick={() => remove(item)}
      >
        {t('references.remove')}
      </button>
    </li>
  );
}

/** Product settings use the exact same upload/gate/promote flow as Library.
 * Metadata edits only publish product bindings; they never recopy the file. */
export function ProductReferenceSettings({
  product,
  onChanged,
}: {
  product: ProductSummary;
  onChanged: () => void;
}) {
  const { t } = useTranslation('products');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const [opened, setOpened] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [model, setModel] = useState(product.variant_label ?? '');
  const [addRole, setAddRole] = useState<Item['role']>('detail');
  const [selectedReference, setSelectedReference] = useState('');
  const [pendingObject, setPendingObject] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const query = useDomainQuery(
    'get_product_references',
    { product_id: product.product_id },
    {
      enabled: opened && !!product.entry_id && product.ready,
    }
  );
  const library = usePublishedReferenceAssets({ enabled: opened });
  const data = query.data;
  const disabled = busy || !!data?.has_unpublished_changes;
  const libraryHref = `/w/${current.slug}/library?cat=product`;

  const command = useCallback(
    async (
      kind: 'add' | 'update' | 'delete',
      body: object = {},
      itemId = ''
    ) => {
      if (!data || busy) return;
      setBusy(true);
      setFailure(null);
      const common = {
        workspaceSlug: current.slug,
        productId: product.product_id,
        actionInstanceId: `product-reference:${crypto.randomUUID()}`,
        ifMatch: data.revision,
      };
      try {
        const result =
          kind === 'add'
            ? await gateway.addProductReference({
                ...common,
                body: {
                  role: addRole,
                  identity_status: 'unverified',
                  note: '',
                  ...body,
                  model_variant: model,
                },
              })
            : kind === 'update'
              ? await gateway.updateProductReference({
                  ...common,
                  itemId,
                  body,
                })
              : await gateway.deleteProductReference({ ...common, itemId });
        if (result.kind === 'problem') {
          setFailure(result.problem.code);
          if (result.problem.code === 'revision_conflict')
            await query.refresh();
          return;
        }
        if (result.kind === 'reconciliation_required') {
          setFailure('reconciliation_required');
          return;
        }
        if (kind === 'add') {
          setSelectedReference('');
          setPendingObject(null);
          setUploadOpen(false);
        }
        await query.refresh();
        void library.refresh();
        onChanged();
      } catch {
        setFailure('network');
      } finally {
        setBusy(false);
      }
    },
    [
      data,
      busy,
      current.slug,
      product.product_id,
      model,
      addRole,
      gateway,
      query,
      library,
      onChanged,
    ]
  );

  const uploaded = useCallback(
    (referenceObjectId: string) => {
      setPendingObject(referenceObjectId);
      void command('add', { reference_object_id: referenceObjectId });
    },
    [command]
  );

  const groups = data
    ? Array.from(
        new Set([
          ...data.model_variants,
          ...data.items.map((item) => item.model_variant),
        ])
      )
    : [];
  const modelCount =
    data?.items.filter((item) => item.model_variant === model).length ?? 0;
  const modelRequired =
    !!data?.model_variants.length && !model && addRole !== 'logo';
  return (
    <details
      className="mt-3 border-t border-base-300 pt-3 text-sm"
      onToggle={(event) => setOpened(event.currentTarget.open)}
    >
      <summary className="cursor-pointer font-medium">
        {t('references.title')}
      </summary>
      {opened && (
        <div className="mt-3 space-y-3">
          <p className="text-xs text-base-content/70">
            {t('references.description')}
          </p>
          {(!product.entry_id || !product.ready) && (
            <p>{t('references.profile-required')}</p>
          )}
          {query.isLoading && <p role="status">{t('references.loading')}</p>}
          {query.problem && (
            <div role="alert">
              <p>{t('references.load-error')}</p>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                onClick={() => void query.refresh()}
              >
                {t('retry')}
              </button>
            </div>
          )}
          {failure && (
            <p role="alert" className="text-error">
              {t(`references.error-${failure}`, {
                defaultValue: t('references.error-default'),
              })}
            </p>
          )}
          {data?.has_unpublished_changes && (
            <p role="status">{t('references.draft-conflict')}</p>
          )}
          <Link href={libraryHref} className="link text-xs">
            {t('references.library')}
          </Link>
          {data && (
            <>
              {groups.map((label) => {
                const items = data.items.filter(
                  (item) => item.model_variant === label
                );
                return (
                  <section
                    key={label}
                    className="space-y-2"
                    aria-label={label || t('references.general')}
                  >
                    <h3 className="font-medium">
                      {label || t('references.general')}
                    </h3>
                    {items.length === 0 ? (
                      <p className="text-xs text-base-content/60">
                        {t('references.empty')}
                      </p>
                    ) : (
                      <ul className="grid grid-cols-1 gap-2">
                        {items.map((item) => (
                          <ReferenceItem
                            key={item.item_id}
                            item={item}
                            disabled={disabled}
                            needsModel={
                              !!data.model_variants.length &&
                              !item.model_variant &&
                              item.role !== 'logo'
                            }
                            update={(item, body) =>
                              void command('update', body, item.item_id)
                            }
                            remove={(item) =>
                              void command('delete', {}, item.item_id)
                            }
                          />
                        ))}
                      </ul>
                    )}
                  </section>
                );
              })}
              {groups.length === 0 && <p>{t('references.empty')}</p>}
              <fieldset disabled={disabled} className="space-y-2">
                <legend className="mb-2 font-medium">
                  {t('references.add')}
                </legend>
                <label className="block text-xs">
                  {t('references.new-role')}
                  <select
                    className="select select-bordered select-sm mt-1 w-full"
                    value={addRole}
                    disabled={uploadOpen || disabled}
                    onChange={(event) =>
                      setAddRole(event.target.value as Item['role'])
                    }
                  >
                    {ROLES.map((role) => (
                      <option key={role} value={role}>
                        {t(`references.role-${role}`)}
                      </option>
                    ))}
                  </select>
                </label>
                {data.model_variants.length > 0 && (
                  <label className="block text-xs">
                    {t('references.model')}
                    <select
                      className="select select-bordered select-sm mt-1 w-full"
                      value={model}
                      disabled={uploadOpen || disabled}
                      onChange={(event) => setModel(event.target.value)}
                    >
                      <option value="">{t('references.general')}</option>
                      {data.model_variants.map((label) => (
                        <option key={label} value={label}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <p className="text-xs text-base-content/60">
                  {t('references.limit', { count: modelCount })}
                </p>
                {modelRequired && (
                  <p className="text-xs text-warning">
                    {t('references.choose-model')}
                  </p>
                )}
                {modelCount < 8 && !modelRequired && (
                  <>
                    <button
                      className="btn btn-outline btn-sm w-full"
                      type="button"
                      onClick={() => setUploadOpen(!uploadOpen)}
                    >
                      {t('references.upload')}
                    </button>
                    {uploadOpen && product.entry_id && (
                      <ReferenceUploadZone
                        contextEntryId={product.entry_id}
                        maxFileBytes={10 * 1024 * 1024}
                        onComplete={uploaded}
                      />
                    )}
                    {pendingObject && (
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={() =>
                          void command('add', {
                            reference_object_id: pendingObject,
                          })
                        }
                      >
                        {t('references.retry-bind')}
                      </button>
                    )}
                    <label className="block text-xs">
                      {t('references.reuse')}
                      <select
                        className="select select-bordered select-sm mt-1 w-full"
                        value={selectedReference}
                        onChange={(event) =>
                          setSelectedReference(event.target.value)
                        }
                      >
                        <option value="">{t('references.choose')}</option>
                        {library.items
                          .filter(
                            (reference) =>
                              !data.items.some(
                                (item) =>
                                  item.model_variant === model &&
                                  item.reference_version_id ===
                                    reference.versionId
                              )
                          )
                          .map((reference) => (
                            <option
                              key={reference.versionId}
                              value={reference.versionId}
                            >
                              {reference.displayName}
                            </option>
                          ))}
                      </select>
                    </label>
                    {library.problem && (
                      <p role="alert">{t('references.load-error')}</p>
                    )}
                    {library.hasMore && (
                      <button
                        type="button"
                        className="btn btn-ghost btn-xs"
                        disabled={library.isLoadingMore}
                        onClick={library.loadMore}
                      >
                        {t('references.load-more')}
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn btn-sm w-full"
                      disabled={!selectedReference || disabled}
                      onClick={() =>
                        void command('add', {
                          reference_version_id: selectedReference,
                        })
                      }
                    >
                      {t('references.add-selected')}
                    </button>
                  </>
                )}
              </fieldset>
              {busy && <p role="status">{t('references.saving')}</p>}
            </>
          )}
        </div>
      )}
    </details>
  );
}
