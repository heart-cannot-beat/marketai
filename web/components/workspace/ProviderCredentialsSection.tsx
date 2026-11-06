import { useTranslation } from 'next-i18next';
import { useCallback, useMemo, useRef, useState } from 'react';
import { Card } from '@/components/shared';
import { newActionId } from '@/components/generation/shared/newActionId';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import {
  formatDate as formatLocaleDate,
  formatNumber,
} from '@/lib/i18n/format';
import { useWorkspace } from './WorkspaceProvider';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { errorMessage } from '@/lib/i18n/domain-errors';

/**
 * Settings → Provider credentials: the Owner's BYOK keys.
 *
 * OpenAI, Gemini and Seedream pay for Generate (`binding_scope=generation`);
 * ScrapeCreators pays for competitor ad scans (`binding_scope=market_data`).
 * All four share the existing protected credential commands. The Workspace
 * keeps one credential lineage per provider (`credential_key` below).
 * A generation key is "ready" when its active version is what the Domain's
 * `generation` binding points at, which is exactly when
 * `get_generation_capabilities` advertises the `byok` mode. A market-data
 * key is checked by the Domain when it is saved (a key that is invalid or has
 * no credits is refused) and is "connected" only while it is bound to
 * `market_data` and its last check found credits; both facts come with the
 * key in `list_credentials`. Every mutation is an Owner-only Domain command;
 * MFA is checked only at login (ADR 0015), so none asks for a code here.
 */

type BindingScope = 'generation' | 'market_data';

/** The copy that depends on what a key pays for:
 * `settings:provider-credentials.<key>`. */
const SCOPE_COPY = {
  generation: {
    hint: 'provider-hint',
    bind: 'bind',
    save: 'save',
    saving: 'saving',
    savedNotBound: 'saved-not-bound',
  },
  market_data: {
    hint: 'market-data-hint',
    bind: 'market-data-bind',
    save: 'market-data-save',
    saving: 'market-data-saving',
    savedNotBound: 'market-data-saved-not-bound',
  },
} as const satisfies Record<BindingScope, Record<string, string>>;

const PROVIDERS = [
  {
    provider: 'openai',
    credentialKey: 'openai_api_key',
    copyKey: 'provider-openai',
    bindingScope: 'generation',
  },
  {
    provider: 'gemini',
    credentialKey: 'gemini_api_key',
    copyKey: 'provider-gemini',
    bindingScope: 'generation',
  },
  {
    provider: 'seedream',
    credentialKey: 'ark_api_key',
    copyKey: 'provider-seedream',
    bindingScope: 'generation',
  },
  {
    provider: 'scrapecreators',
    credentialKey: 'scrapecreators_api_key',
    copyKey: 'provider-scrapecreators',
    bindingScope: 'market_data',
  },
] as const satisfies ReadonlyArray<{
  provider: string;
  credentialKey: string;
  copyKey: string;
  bindingScope: BindingScope;
}>;
type ProviderDefinition = (typeof PROVIDERS)[number];

const REVOKE_REASON = 'removed from workspace settings';

/** Failure banners the section shows: `settings:provider-credentials.<key>`. */
type FailureMessage =
  | 'list-unavailable'
  | 'command-refused'
  | 'command-unresolved'
  | (typeof SCOPE_COPY)[BindingScope]['savedNotBound'];

/** `GET /api/v1/credentials` item (`_credential_metadata_to_dict`,
 * app/entitlements/api.py) — the OpenAPI document types the body as
 * `unknown`, so the shape is pinned here and read defensively. */
export interface CredentialMetadata {
  credential_version_id: string;
  credential_key: string;
  version_no: number;
  provider: string;
  display_label: string | null;
  state: string;
  value_sha256: string;
  created_at: string;
  rotated_at: string | null;
  revoked_at: string | null;
  /** The `(binding_scope, provider)` bindings pointing at this version. */
  bindings?: Array<{ binding_scope: string; provider: string }>;
  /** The newest key check of a market-data key; `null` for other keys. */
  last_check?: {
    outcome: string;
    credit_count: number | null;
    checked_at: string;
  } | null;
}

interface CredentialReadiness {
  has_active_credential: boolean;
  credential_version_id: string | null;
}

/** A market-data provider's readiness, read from the bindings that come
 * with each key (generation's comes from `get_generation_capabilities`). */
function marketDataReadiness(
  credentials: CredentialMetadata[],
  provider: string
): CredentialReadiness | undefined {
  const bound = credentials.find(
    (c) =>
      c.provider === provider &&
      c.state === 'active' &&
      (c.bindings ?? []).some(
        (b) => b.binding_scope === 'market_data' && b.provider === provider
      )
  );
  return bound
    ? {
        has_active_credential: true,
        credential_version_id: bound.credential_version_id,
      }
    : undefined;
}

interface Failure {
  message: FailureMessage;
  problem?: ProblemDetails;
}

type KeyFormMode = 'create' | 'rotate';

function isCredentialMetadata(value: unknown): value is CredentialMetadata {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as CredentialMetadata).credential_version_id === 'string' &&
    typeof (value as CredentialMetadata).credential_key === 'string'
  );
}

/** Standard base64 of the UTF-8 bytes — what `plaintext_b64` carries. No
 * `TextEncoder` so the same code runs under jsdom. */
export function utf8ToBase64(value: string): string {
  const bytes = encodeURIComponent(value).replace(
    /%([0-9A-F]{2})/g,
    (_match, hex: string) => String.fromCharCode(parseInt(hex, 16))
  );
  return btoa(bytes);
}

/** The new version's id from a `create_credential` / `rotate_credential`
 * success body (typed `null` by the generated contract, so read as data). */
function createdVersionId(data: unknown): string | null {
  return isCredentialMetadata(data) ? data.credential_version_id : null;
}

function formatDate(iso: string, locale: string): string {
  const parsed = Date.parse(iso);
  return Number.isNaN(parsed) ? iso : formatLocaleDate(locale, parsed);
}

// Code and trace id make a refused request traceable in the web/api logs.
function CommandProblem({
  failure,
  testId,
}: {
  failure: Failure;
  testId: string;
}) {
  const { t } = useTranslation('settings');
  const reference = failure.problem
    ? [
        failure.problem.code,
        failure.problem.trace_id &&
          t('trace', { id: failure.problem.trace_id }),
      ]
        .filter(Boolean)
        .join(' · ')
    : '';
  return (
    <div
      className="alert alert-error items-start break-words text-sm"
      role="alert"
      data-testid={testId}
    >
      <div>
        <div className="font-semibold">
          {t(`provider-credentials.${failure.message}`)}
        </div>
        {failure.problem && (
          <div className="mt-1 text-xs">{errorMessage(t, failure.problem)}</div>
        )}
        {reference && (
          <div className="mt-1 break-all text-xs opacity-75">{reference}</div>
        )}
      </div>
    </div>
  );
}

export function ProviderCredentialsSection() {
  const { t } = useTranslation('settings');
  const { current } = useWorkspace();
  const {
    data: listData,
    problem: listProblem,
    isLoading: listLoading,
    refresh: refreshList,
  } = useDomainQuery('list_credentials');
  const {
    data: capabilities,
    problem: capabilitiesProblem,
    isLoading: capabilitiesLoading,
    refresh: refreshCapabilities,
  } = useDomainQuery('get_generation_capabilities');

  const credentials = useMemo(
    () =>
      Array.isArray(listData)
        ? (listData as unknown[]).filter(isCredentialMetadata)
        : [],
    [listData]
  );
  const readiness = (
    capabilities as
      | { credential_readiness?: Record<string, CredentialReadiness> }
      | undefined
  )?.credential_readiness;
  const refreshAll = useCallback(async () => {
    await Promise.all([refreshList(), refreshCapabilities()]);
  }, [refreshList, refreshCapabilities]);
  return (
    <div data-testid="provider-credentials-section">
      <Card>
        <Card.Body>
          <Card.Header>
            <Card.Title>{t('provider-credentials.title')}</Card.Title>
            <Card.Description>
              {t('provider-credentials.description')}
            </Card.Description>
          </Card.Header>
          {listLoading && (
            <p className="text-sm text-gray-500">
              {t('provider-credentials.loading')}
            </p>
          )}
          {!listLoading && listProblem && (
            <div className="space-y-2">
              <CommandProblem
                failure={{
                  message: 'list-unavailable',
                  problem: listProblem,
                }}
                testId="provider-credentials-list-problem"
              />
              <button
                type="button"
                className="btn btn-outline btn-sm"
                onClick={() => void refreshAll()}
              >
                {t('provider-credentials.retry')}
              </button>
            </div>
          )}
          {!listLoading &&
            !listProblem &&
            PROVIDERS.map((definition) => (
              <ProviderCredentialEditor
                key={`${current.workspaceId ?? current.slug}:${definition.provider}`}
                definition={definition}
                credentials={credentials}
                readiness={
                  definition.bindingScope === 'market_data'
                    ? marketDataReadiness(credentials, definition.provider)
                    : readiness?.[definition.provider]
                }
                bindingKnown={
                  definition.bindingScope === 'market_data' ||
                  (!!capabilities &&
                    !capabilitiesProblem &&
                    !capabilitiesLoading)
                }
                refreshAll={refreshAll}
              />
            ))}
        </Card.Body>
      </Card>
    </div>
  );
}

function ProviderCredentialEditor({
  definition,
  credentials,
  readiness,
  bindingKnown,
  refreshAll,
}: {
  definition: ProviderDefinition;
  credentials: CredentialMetadata[];
  readiness?: CredentialReadiness;
  bindingKnown: boolean;
  refreshAll: () => Promise<void>;
}) {
  const { t, i18n } = useTranslation('settings');
  const copy = SCOPE_COPY[definition.bindingScope];
  const isMarketData = definition.bindingScope === 'market_data';
  const createCommand = useDomainCommand('create_credential');
  const rotateCommand = useDomainCommand('rotate_credential');
  const bindCommand = useDomainCommand('bind_credential');
  const revokeCommand = useDomainCommand('revoke_credential');

  const [formMode, setFormMode] = useState<KeyFormMode | null>(null);
  const [keyValue, setKeyValue] = useState('');
  const [labelValue, setLabelValue] = useState('');
  const [failure, setFailure] = useState<Failure | null>(null);
  const [busy, setBusy] = useState<'save' | 'bind' | 'remove' | null>(null);
  type ActionName = 'save' | 'enable' | 'remove';

  const credential = useMemo(() => {
    const active = credentials.filter(
      (c) => c.provider === definition.provider && c.state === 'active'
    );
    return (
      active.find(
        (c) => c.credential_version_id === readiness?.credential_version_id
      ) ??
      active.find((c) => c.credential_key === definition.credentialKey) ??
      active[0] ??
      null
    );
  }, [credentials, definition, readiness]);
  const isBound =
    bindingKnown &&
    !!credential &&
    !!readiness?.has_active_credential &&
    readiness.credential_version_id === credential.credential_version_id;
  // A market-data key reads as connected only with a check that found
  // credits; a bound key without one is "saved, not verified".
  const checkedCredits =
    credential?.last_check?.outcome === 'valid'
      ? (credential.last_check.credit_count ?? null)
      : null;
  const isConnected = isBound && (!isMarketData || checkedCredits !== null);
  const pendingVersion = useRef<string | null>(null);
  const actionInFlight = useRef(false);
  const [unresolved, setUnresolved] = useState(false);
  const forgetUncertainSecret = useCallback(() => {
    setKeyValue('');
    setLabelValue('');
    setFormMode(null);
    setUnresolved(true);
  }, []);

  const bindVersion = useCallback(
    async (credentialVersionId: string): Promise<boolean> => {
      const outcome = await bindCommand
        .execute({
          actionInstanceId: newActionId(),
          pathParams: {
            credential_version_id: credentialVersionId,
            scope: definition.bindingScope,
          },
          body: { provider: definition.provider },
        })
        .catch(() => null);
      if (outcome === null || outcome.kind === 'reconciliation') {
        forgetUncertainSecret();
        setFailure({ message: 'command-unresolved' });
        return false;
      }
      if (outcome.kind === 'problem') {
        setFailure({ message: 'command-refused', problem: outcome.problem });
        return false;
      }
      pendingVersion.current = null;
      return true;
    },
    [
      bindCommand,
      definition.provider,
      definition.bindingScope,
      forgetUncertainSecret,
    ]
  );

  const saveKey = useCallback(async () => {
    const plaintext = keyValue.trim();
    if (!plaintext) return;
    setFailure(null);
    setBusy('save');
    try {
      let versionId: string | null = null;
      if (formMode === 'rotate' && credential) {
        const outcome = await rotateCommand
          .execute({
            actionInstanceId: newActionId(),
            pathParams: { credential_key: credential.credential_key },
            body: { new_plaintext_b64: utf8ToBase64(plaintext) },
          })
          .catch(() => null)
          .finally(() => rotateCommand.reset());
        if (outcome === null || outcome.kind === 'reconciliation') {
          forgetUncertainSecret();
          setFailure({ message: 'command-unresolved' });
          await refreshAll();
          return;
        }
        if (outcome.kind === 'problem') {
          setFailure({
            message: 'command-refused',
            problem: outcome.problem,
          });
          return;
        }
        versionId = createdVersionId(outcome.data);
      } else {
        const label = labelValue.trim();
        const outcome = await createCommand
          .execute({
            actionInstanceId: newActionId(),
            body: {
              credential_key: definition.credentialKey,
              provider: definition.provider,
              plaintext_b64: utf8ToBase64(plaintext),
              display_label: label === '' ? null : label,
            },
          })
          .catch(() => null)
          .finally(() => createCommand.reset());
        if (outcome === null || outcome.kind === 'reconciliation') {
          forgetUncertainSecret();
          setFailure({ message: 'command-unresolved' });
          await refreshAll();
          return;
        }
        if (outcome.kind === 'problem') {
          setFailure({
            message: 'command-refused',
            problem: outcome.problem,
          });
          return;
        }
        versionId = createdVersionId(outcome.data);
      }

      // The secret is stored; forget it here whatever happens next.
      setKeyValue('');
      setLabelValue('');
      setFormMode(null);
      pendingVersion.current = versionId;
      if (versionId === null || !(await bindVersion(versionId))) {
        setFailure((current) => current ?? { message: copy.savedNotBound });
      }
      await refreshAll();
    } finally {
      setBusy(null);
    }
  }, [
    bindVersion,
    copy,
    createCommand,
    formMode,
    keyValue,
    labelValue,
    credential,
    refreshAll,
    rotateCommand,
    definition,
    forgetUncertainSecret,
  ]);

  const enableExisting = useCallback(async () => {
    const versionId =
      pendingVersion.current ?? credential?.credential_version_id;
    if (!versionId) return;
    setFailure(null);
    setBusy('bind');
    try {
      if (await bindVersion(versionId)) {
        await refreshAll();
      }
    } finally {
      setBusy(null);
    }
  }, [bindVersion, credential, refreshAll]);

  const removeKey = useCallback(async () => {
    if (!credential) return;
    setFailure(null);
    setBusy('remove');
    try {
      const outcome = await revokeCommand
        .execute({
          actionInstanceId: newActionId(),
          pathParams: {
            credential_version_id: credential.credential_version_id,
          },
          body: { reason: REVOKE_REASON },
        })
        .catch(() => null);
      if (outcome === null || outcome.kind === 'reconciliation') {
        forgetUncertainSecret();
        setFailure({ message: 'command-unresolved' });
        await refreshAll();
        return;
      }
      if (outcome.kind === 'problem') {
        setFailure({ message: 'command-refused', problem: outcome.problem });
        return;
      }
      setFormMode(null);
      await refreshAll();
    } finally {
      setBusy(null);
    }
  }, [credential, refreshAll, revokeCommand, forgetUncertainSecret]);

  const actionsRef = useRef<Record<ActionName, () => Promise<void>>>({
    save: saveKey,
    enable: enableExisting,
    remove: removeKey,
  });
  actionsRef.current = {
    save: saveKey,
    enable: enableExisting,
    remove: removeKey,
  };

  const runAction = useCallback(
    async (name: ActionName) => {
      if (actionInFlight.current) return;
      actionInFlight.current = true;
      try {
        await actionsRef.current[name]();
      } catch {
        forgetUncertainSecret();
        setFailure({ message: 'command-unresolved' });
      } finally {
        actionInFlight.current = false;
      }
    },
    [forgetUncertainSecret]
  );

  const openForm = (mode: KeyFormMode) => {
    setFailure(null);
    setFormMode(mode);
  };
  const closeForm = () => {
    setFormMode(null);
    setKeyValue('');
    setLabelValue('');
  };

  const actionsDisabled = busy !== null || unresolved;

  return (
    <section
      aria-label={t(`provider-credentials.${definition.copyKey}`)}
      data-testid={`${definition.provider}-credential-section`}
      className="mt-5 space-y-3 border-t border-base-300 pt-5"
    >
      <div>
        <h3 className="text-sm font-semibold">
          {t(`provider-credentials.${definition.copyKey}`)}
        </h3>
        <p className="text-xs text-gray-500">
          {t(`provider-credentials.${copy.hint}`)}
        </p>
        {/* Spec §10: said when the Workspace first configures the vendor. */}
        {isMarketData && !credential && (
          <p
            className="mt-1 text-xs text-gray-500"
            data-testid={`${definition.provider}-credential-terms`}
          >
            {t('provider-credentials.market-data-terms')}
          </p>
        )}
      </div>
      {credential && (
        <div className="flex flex-col gap-3">
          <div
            className="flex items-center gap-2"
            data-testid={`${definition.provider}-credential-status`}
            data-bound={isBound ? 'true' : 'false'}
            data-connected={isConnected ? 'true' : 'false'}
          >
            <span
              className={`badge badge-sm ${
                isConnected ? 'badge-success' : 'badge-warning'
              }${isMarketData ? ' h-auto min-h-5 break-words' : ''}`}
            >
              {!bindingKnown
                ? t('provider-credentials.unknown-badge')
                : !isBound
                  ? t('provider-credentials.not-bound-badge')
                  : !isMarketData
                    ? t('provider-credentials.ready-badge')
                    : isConnected
                      ? t('provider-credentials.market-data-ready-badge', {
                          credits: formatNumber(
                            i18n.language,
                            checkedCredits ?? 0
                          ),
                        })
                      : t('provider-credentials.market-data-unverified-badge')}
            </span>
          </div>
          {isMarketData && isBound && !isConnected && (
            <p className="text-xs text-gray-500">
              {t('provider-credentials.market-data-unverified-hint')}
            </p>
          )}
          <dl className="grid grid-cols-2 gap-2 text-sm">
            <dt className="text-gray-500">{t('provider-credentials.label')}</dt>
            <dd className="min-w-0 break-words">
              {credential.display_label || t('provider-credentials.no-label')}
            </dd>
            <dt className="text-gray-500">
              {t('provider-credentials.version')}
            </dt>
            <dd className="font-mono text-xs">{`v${credential.version_no}`}</dd>
            <dt className="text-gray-500">
              {t('provider-credentials.fingerprint')}
            </dt>
            <dd className="font-mono text-xs">
              {credential.value_sha256.slice(0, 16)}
            </dd>
            <dt className="text-gray-500">{t('provider-credentials.added')}</dt>
            <dd className="text-xs">
              {formatDate(credential.created_at, i18n.language)}
            </dd>
            {isMarketData && credential.last_check && (
              <>
                <dt className="text-gray-500">
                  {t('provider-credentials.market-data-last-check')}
                </dt>
                <dd className="text-xs">
                  {formatDate(credential.last_check.checked_at, i18n.language)}
                </dd>
              </>
            )}
          </dl>
          {formMode === null && (
            <div className="flex flex-wrap gap-2">
              {!isBound && bindingKnown && (
                <button
                  type="button"
                  data-testid={`bind-${definition.provider}-credential`}
                  className="btn btn-primary btn-sm"
                  disabled={actionsDisabled}
                  onClick={() => void runAction('enable')}
                >
                  {busy === 'bind'
                    ? t('provider-credentials.binding')
                    : t(`provider-credentials.${copy.bind}`)}
                </button>
              )}
              <button
                type="button"
                data-testid={`replace-${definition.provider}-credential`}
                className="btn btn-outline btn-sm"
                disabled={actionsDisabled}
                onClick={() => openForm('rotate')}
              >
                {t('provider-credentials.replace')}
              </button>
              <button
                type="button"
                data-testid={`remove-${definition.provider}-credential`}
                className="btn btn-ghost btn-sm text-error"
                disabled={actionsDisabled}
                onClick={() => void runAction('remove')}
              >
                {busy === 'remove'
                  ? t('provider-credentials.removing')
                  : t('provider-credentials.remove')}
              </button>
            </div>
          )}
        </div>
      )}

      {!credential && formMode === null && (
        <div>
          <button
            type="button"
            data-testid={`add-${definition.provider}-credential`}
            className="btn btn-primary btn-sm"
            disabled={actionsDisabled}
            onClick={() => openForm('create')}
          >
            {t(`provider-credentials.${definition.copyKey}-add`)}
          </button>
        </div>
      )}

      {formMode !== null && (
        <form
          className="flex flex-col gap-3"
          data-testid={`${definition.provider}-credential-form`}
          onSubmit={(event) => {
            event.preventDefault();
            void runAction('save');
          }}
        >
          {formMode === 'create' && (
            <label className="form-control w-full max-w-md">
              <span className="label-text mb-1 text-sm font-medium">
                {t('provider-credentials.label-field')}
              </span>
              <input
                data-testid={`${definition.provider}-credential-label`}
                className="input input-bordered input-sm"
                type="text"
                maxLength={120}
                value={labelValue}
                onChange={(event) => setLabelValue(event.target.value)}
              />
            </label>
          )}
          <label className="form-control w-full max-w-md">
            <span className="label-text mb-1 text-sm font-medium">
              {t('provider-credentials.key-field')}
            </span>
            <input
              data-testid={`${definition.provider}-credential-key`}
              name={`${definition.provider}-provider-api-key`}
              className="input input-bordered input-sm font-mono"
              type="password"
              autoComplete="new-password"
              data-1p-ignore
              data-lpignore="true"
              spellCheck={false}
              required
              value={keyValue}
              onChange={(event) => setKeyValue(event.target.value)}
            />
            <span className="label-text-alt mt-1 text-xs text-gray-500">
              {t('provider-credentials.key-hint', {
                source: t(`provider-credentials.${definition.copyKey}-source`),
              })}
            </span>
          </label>
          <div className="flex flex-wrap gap-2">
            <button
              type="submit"
              data-testid={`save-${definition.provider}-credential`}
              className="btn btn-primary btn-sm"
              disabled={actionsDisabled || keyValue.trim() === ''}
            >
              {busy === 'save'
                ? t(`provider-credentials.${copy.saving}`)
                : t(`provider-credentials.${copy.save}`)}
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={busy === 'save'}
              onClick={closeForm}
            >
              {t('provider-credentials.cancel')}
            </button>
          </div>
        </form>
      )}

      {(!bindingKnown || unresolved) && (
        <button
          type="button"
          className="btn btn-outline btn-sm"
          disabled={busy !== null}
          onClick={() => {
            if (unresolved) window.location.reload();
            else void refreshAll();
          }}
        >
          {t('provider-credentials.refresh-status')}
        </button>
      )}
      {failure && (
        <CommandProblem
          failure={failure}
          testId={`${definition.provider}-credential-problem`}
        />
      )}
    </section>
  );
}
