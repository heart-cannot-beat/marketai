/**
 * Structured logging with mandatory redaction.
 *
 * The integration contract forbids session cookies, delegated domain JWTs,
 * BYOK provider material and signed URLs from reaching structured logs. This
 * module is the only sanctioned way for the control plane to emit logs that
 * carry request-derived data.
 */

export const REDACTED = '[redacted]';

/** Key names whose value is never printable, matched case-insensitively. */
const SENSITIVE_KEY_PATTERNS = [
  /cookie/i,
  /authorization/i,
  /^auth$/i,
  /password/i,
  /passwd/i,
  /token/i,
  /secret/i,
  /credential/i,
  /api[-_]?key/i,
  /session[-_]?id/i,
  /private[-_]?key/i,
  /signed[-_]?url/i,
  /jwt/i,
  /bearer/i,
  /database[-_]?url/i,
  /conn(ection)?[-_]?string/i,
];

/** Value shapes that must be scrubbed even when the key looks innocent. */
const SENSITIVE_VALUE_PATTERNS: RegExp[] = [
  // Bearer / Basic authorization values
  /\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}/g,
  // Compact JWT
  /\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b/g,
  // Cookie pairs for NextAuth session cookies
  /\b(__Secure-)?next-auth\.[A-Za-z.-]*token=[^;\s]+/g,
  // URLs carrying credentials, e.g. postgres://user:pass@host/db
  /\b([a-z][a-z0-9+.-]*):\/\/[^\s:/@]+:[^\s@]+@/gi,
  // Known provider credential prefixes (BYOK material must never be logged)
  /\b(sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,}|EAA[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|ghu_[A-Za-z0-9]{20,})\b/g,
  // Query-string secrets, e.g. ?X-Amz-Signature=... or &token=...
  /([?&](?:[A-Za-z0-9_-]*(?:signature|token|secret|key|credential)[A-Za-z0-9_-]*)=)[^&\s]+/gi,
];

const isSensitiveKey = (key: string) =>
  SENSITIVE_KEY_PATTERNS.some((pattern) => pattern.test(key));

export const redactString = (value: string): string =>
  SENSITIVE_VALUE_PATTERNS.reduce(
    (acc, pattern) =>
      acc.replace(pattern, (match, ...groups) => {
        // Patterns with a capturing prefix keep the prefix so the shape of the
        // log line survives; everything else is replaced wholesale.
        const prefix = typeof groups[0] === 'string' ? groups[0] : '';
        if (prefix && match.startsWith(prefix) && /[?&=]$/.test(prefix)) {
          return `${prefix}${REDACTED}`;
        }
        return REDACTED;
      }),
    value
  );

export const redact = (value: unknown, depth = 0): unknown => {
  if (depth > 8) {
    return REDACTED;
  }

  if (typeof value === 'string') {
    return redactString(value);
  }

  if (value === null || typeof value !== 'object') {
    return value;
  }

  if (Array.isArray(value)) {
    return value.map((item) => redact(item, depth + 1));
  }

  if (value instanceof Error) {
    return {
      name: value.name,
      message: redactString(value.message),
    };
  }

  const source = value as Record<string, unknown>;
  const output: Record<string, unknown> = {};

  for (const key of Object.keys(source)) {
    output[key] = isSensitiveKey(key)
      ? REDACTED
      : redact(source[key], depth + 1);
  }

  return output;
};

type Level = 'debug' | 'info' | 'warn' | 'error';

export type LogFields = Record<string, unknown>;

const emit = (level: Level, event: string, fields: LogFields = {}) => {
  const line = JSON.stringify({
    level,
    event,
    service: 'control-plane',
    ...(redact(fields) as LogFields),
  });

  if (level === 'error') {
    console.error(line);
    return;
  }

  if (level === 'warn') {
    console.warn(line);
    return;
  }

  console.log(line);
};

export const logger = {
  debug: (event: string, fields?: LogFields) => emit('debug', event, fields),
  info: (event: string, fields?: LogFields) => emit('info', event, fields),
  warn: (event: string, fields?: LogFields) => emit('warn', event, fields),
  error: (event: string, fields?: LogFields) => emit('error', event, fields),
};

export default logger;
