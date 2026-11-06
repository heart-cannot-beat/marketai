function recursivelySortObjectKeys(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(recursivelySortObjectKeys);
  }
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return Object.fromEntries(
      Object.keys(record)
        .sort()
        .map((key) => [key, recursivelySortObjectKeys(record[key])])
    );
  }
  return value;
}

export function canonicalJson(value: unknown): string {
  return JSON.stringify(recursivelySortObjectKeys(value));
}
