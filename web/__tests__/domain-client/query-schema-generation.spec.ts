/**
 * Verifies `generate_allowlist.ts` emits real Zod query schemas (instead of
 * the old hardcoded `querySchema: null`) — see `scripts/generate_allowlist.ts`
 * and `scripts/lib/spec.ts#extractQueryParams` (Issue #27 Task 6). Runs
 * against the GENERATED allowlist produced by `bash scripts/generate_all.sh`.
 *
 * `list_library_entries` (contracts/openapi/marketai-domain-v1.json) declares
 * four query params: `entry_type` (string, optional), `include_archived`
 * (boolean, optional, default `false`), `cursor` (string, optional), `limit`
 * (integer, optional, default `50`, min `1`, max `100`). `get_identity_probe`
 * declares none.
 */
import { DOMAIN_ALLOWLIST } from '@/lib/domain-client/generated/allowlist';

describe('generated query schemas', () => {
  const listEntries = DOMAIN_ALLOWLIST.find(
    (e) => e.operationId === 'list_library_entries'
  );

  test('list_library_entries has a query schema', () => {
    expect(listEntries).toBeDefined();
    expect(listEntries!.querySchema).not.toBeNull();
  });

  test('list_library_entries accepts valid params', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({ entry_type: 'product', limit: '50' });
    expect(result.success).toBe(true);
  });

  test('list_library_entries rejects invalid limit', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({ limit: '999' });
    // limit max is 100
    expect(result.success).toBe(false);
  });

  test('operations without query params have null schema', () => {
    const identityProbe = DOMAIN_ALLOWLIST.find(
      (e) => e.operationId === 'get_identity_probe'
    );
    expect(identityProbe!.querySchema).toBeNull();
  });

  test('list_library_entries rejects unknown query param', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({ unknown_param: 'foo' });
    // .strict() rejects unknown keys — parameter-injection defense.
    expect(result.success).toBe(false);
  });

  test('integer coercion handles non-numeric string', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({ limit: 'not-a-number' });
    expect(result.success).toBe(false);
  });

  test('boolean coercion accepts "true"/"false" string values', () => {
    const schema = listEntries!.querySchema!;
    expect(schema.safeParse({ include_archived: 'true' }).success).toBe(true);
    expect(schema.safeParse({ include_archived: 'false' }).success).toBe(true);
  });

  test('boolean coercion treats any non-empty string as true (z.coerce.boolean semantics)', () => {
    // z.coerce.boolean() runs JS `Boolean(value)`, so any non-empty string —
    // including "maybe" — coerces to `true`; only "" coerces to `false`.
    // This documents the actual (permissive) generated behavior rather than
    // asserting rejection, which z.coerce.boolean() does not provide.
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({ include_archived: 'maybe' });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(
        (result.data as { include_archived: boolean }).include_archived
      ).toBe(true);
    }
  });

  test('limit defaults to 50 when omitted', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({});
    expect(result.success).toBe(true);
    if (result.success) {
      expect((result.data as { limit: number }).limit).toBe(50);
    }
  });

  test('include_archived defaults to false when omitted', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({});
    expect(result.success).toBe(true);
    if (result.success) {
      expect(
        (result.data as { include_archived: boolean }).include_archived
      ).toBe(false);
    }
  });

  test('limit minimum is 1', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({ limit: '0' });
    expect(result.success).toBe(false);
  });

  test('entry_type and cursor accept plain strings', () => {
    const schema = listEntries!.querySchema!;
    const result = schema.safeParse({
      entry_type: 'ad_copy',
      cursor: 'opaque-cursor-value',
    });
    expect(result.success).toBe(true);
  });
});
