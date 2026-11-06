import { useAssetActions } from '@/hooks/assets/useAssetActions';

describe('useAssetActions', () => {
  it('resolves review as allowed when present in allowed_actions', () => {
    const result = useAssetActions([
      { action: 'review', allowed: true, reason_code: null },
      { action: 'download', allowed: true, reason_code: null },
    ]);
    expect(result.canReview).toBe(true);
    expect(result.canDownload).toBe(true);
    expect(result.canRegenerate).toBe(false);
  });

  it('resolves regenerate as allowed when present', () => {
    const result = useAssetActions([
      { action: 'regenerate', allowed: true, reason_code: null },
    ]);
    expect(result.canRegenerate).toBe(true);
    expect(result.canReview).toBe(false);
    expect(result.canDownload).toBe(false);
  });

  it('returns all false for empty allowed_actions', () => {
    const result = useAssetActions([]);
    expect(result.canReview).toBe(false);
    expect(result.canRegenerate).toBe(false);
    expect(result.canDownload).toBe(false);
  });

  it('ignores unknown action codes', () => {
    const result = useAssetActions([
      { action: 'review', allowed: true, reason_code: null },
      { action: 'unknown_action', allowed: true, reason_code: null },
    ]);
    expect(result.canReview).toBe(true);
    expect(result.canRegenerate).toBe(false);
  });

  it('does not enable a server-denied action merely because its code is present', () => {
    const result = useAssetActions([
      { action: 'review', allowed: false, reason_code: 'role_not_permitted' },
    ]);

    expect(result.canReview).toBe(false);
  });
});
