import type { useTranslation } from 'next-i18next';

type Translate = ReturnType<typeof useTranslation>['t'];

/**
 * What a domain refusal or failure carries for the interface (#253): the
 * stable `code`, and the `params` its copy interpolates. `detail` and `title`
 * are diagnostics for logs and audit, in whatever language the service wrote
 * them; nothing here ever renders them.
 */
export type DomainProblem = {
  code?: string | null;
  params?: Record<string, unknown> | null;
  field_errors?: DomainFieldError[] | null;
  /** The service will answer differently later; a read may retry (#309). */
  retryable?: boolean;
  detail?: string;
  title?: string;
};

export type DomainFieldError = {
  field: string;
  code?: string | null;
  params?: Record<string, unknown> | null;
  message?: string;
  detail?: string;
};

/** Thrown where a refused command has to cross an `async` boundary. */
export class DomainProblemError extends Error {
  constructor(readonly problem: DomainProblem = {}) {
    super(problem.code || 'unknown');
  }
}

/**
 * Codes the server builds at run time (a provider's HTTP status, a Meta Graph
 * error number) cannot each have copy; they read as their family.
 */
const CODE_FAMILIES: ReadonlyArray<readonly [RegExp, string]> = [
  [/^(?:openai|gemini|seedream)_\d{3}$/, 'provider_http_error'],
  [/^meta_\d+(?:_\d+)?$/, 'meta_api_error'],
  [/^[a-z][a-z0-9_]*_dispatch_failed$/, 'dispatch_failed'],
  [/^[a-z][a-z0-9_]*_dispatch_uncertain$/, 'dispatch_uncertain'],
];

/** The family a run-time code reads as, if it has one. */
export function familyOf(code: string): string | undefined {
  return CODE_FAMILIES.find(([pattern]) => pattern.test(code))?.[1];
}

const MISSING = '\u0000missing';

let missingErrorCode: (code: string) => void = () => undefined;

/**
 * Called with a code that has no `error-<code>` copy. The interface then
 * shows the code itself; the Jest setup makes it fail the test instead.
 */
export function onMissingErrorCode(handler: (code: string) => void) {
  missingErrorCode = handler;
}

const lookup = (
  t: Translate,
  code: string,
  params?: Record<string, unknown> | null
): string | null => {
  const message = t(`error-${code}`, {
    ns: 'common',
    defaultValue: MISSING,
    // `replace` keeps params apart from i18next's own options (`count`, ...).
    replace: params ?? {},
  });
  // A record written before its code carried params leaves the placeholder
  // unfilled; never show the template itself.
  return message === MISSING ? null : message.replace(/{{\s*[^}]+}}/g, '…');
};

/** The interface's sentence for one code. */
export function errorCodeMessage(
  t: Translate,
  code: string | null | undefined,
  params?: Record<string, unknown> | null
): string {
  if (!code) return t('error-unknown', { ns: 'common' });
  const message = lookup(t, code, params);
  if (message !== null) return message;
  const family = familyOf(code);
  const familyMessage = family ? lookup(t, family, params) : null;
  if (familyMessage !== null) return familyMessage;
  missingErrorCode(code);
  return code;
}

/** One field's message: its own code, else the request's. */
export function fieldErrorMessage(t: Translate, error: DomainFieldError) {
  return errorCodeMessage(t, error.code || 'validation_error', error.params);
}

/**
 * The interface's sentence for a problem. A request that failed validation
 * reads as its first field's reason (`string_too_long` with its limit), not
 * the generic "check the fields".
 */
export function errorMessage(
  t: Translate,
  problem: DomainProblem | null | undefined
): string {
  if (!problem) return t('error-unknown', { ns: 'common' });
  const first =
    problem.code === 'validation_error' ? problem.field_errors?.[0] : null;
  if (first?.code) return fieldErrorMessage(t, first);
  return errorCodeMessage(t, problem.code, problem.params);
}

const KEY = /^[a-z0-9][a-z0-9.-]*$/;

/**
 * The sentence for anything a `catch` received: a refused command by its
 * code, an `Error` whose message is one of the page's own locale keys, and
 * anything else as "could not be completed".
 */
export function errorMessageOf(t: Translate, error: unknown): string {
  if (error instanceof DomainProblemError)
    return errorMessage(t, error.problem);
  if (error instanceof Error && KEY.test(error.message))
    return t(error.message, {
      defaultValue: t('error-unknown', { ns: 'common' }),
    });
  return t('error-unknown', { ns: 'common' });
}

/**
 * A control-plane route's refusal (`{ message, code?, params? }`): its code's
 * copy when it carries one (#157), else the route's own message.
 */
export function apiErrorMessage(
  t: Translate,
  error:
    | {
        message?: string;
        code?: string;
        params?: Record<string, unknown>;
      }
    | null
    | undefined
): string {
  if (error?.code) return errorCodeMessage(t, error.code, error.params);
  return error?.message || t('error-unknown', { ns: 'common' });
}

/** `body.understanding.audience` -> `understanding.audience`. */
export function fieldPath(path: string) {
  return path.replace(/^body\./, '');
}
