import { retrySerializableTransaction } from '../../lib/serializable-retry';

/**
 * Invitation acceptance and creation, and role changes, retry an aborted
 * SERIALIZABLE transaction from its outer edge (#157 moved the helper out of
 * `models/invitation.ts` so role changes share it).
 */
describe('SERIALIZABLE transaction retries', () => {
  it('retries the whole operation for Prisma P2034 and PostgreSQL 40001', async () => {
    const operation = jest
      .fn<Promise<string>, []>()
      .mockRejectedValueOnce(
        Object.assign(new Error('transaction write conflict'), {
          code: 'P2034',
        })
      )
      .mockRejectedValueOnce(
        Object.assign(new Error('could not serialize access'), {
          code: 'P2010',
          meta: { code: '40001' },
        })
      )
      .mockResolvedValue('same-membership');

    await expect(retrySerializableTransaction(operation)).resolves.toBe(
      'same-membership'
    );
    expect(operation).toHaveBeenCalledTimes(3);
  });

  it('does not retry non-serialization failures', async () => {
    const failure = new Error('validation failed');
    const operation = jest.fn<Promise<never>, []>().mockRejectedValue(failure);

    await expect(retrySerializableTransaction(operation)).rejects.toBe(failure);
    expect(operation).toHaveBeenCalledTimes(1);
  });

  it('stops after the bounded retry budget', async () => {
    const failure = Object.assign(new Error('transaction write conflict'), {
      code: 'P2034',
    });
    const operation = jest.fn<Promise<never>, []>().mockRejectedValue(failure);

    await expect(retrySerializableTransaction(operation)).rejects.toBe(failure);
    expect(operation).toHaveBeenCalledTimes(5);
  });
});
