jest.mock('@/lib/env', () => ({
  __esModule: true,
  default: { appUrl: 'http://localhost:4002' },
}));
jest.mock('@/lib/logger', () => ({ logger: { warn: jest.fn() } }));
jest.mock('@/lib/onboarding', () => ({
  reconcileProvisioningWorkspaceFromRuntime: jest.fn(),
}));
jest.mock('@/lib/prisma', () => ({ prisma: {} }));

import { retryInvitationSerializableTransaction } from '../../models/invitation';

describe('invitation SERIALIZABLE transaction retries', () => {
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

    await expect(
      retryInvitationSerializableTransaction(operation)
    ).resolves.toBe('same-membership');
    expect(operation).toHaveBeenCalledTimes(3);
  });

  it('does not retry non-serialization failures', async () => {
    const failure = new Error('validation failed');
    const operation = jest.fn<Promise<never>, []>().mockRejectedValue(failure);

    await expect(
      retryInvitationSerializableTransaction(operation)
    ).rejects.toBe(failure);
    expect(operation).toHaveBeenCalledTimes(1);
  });

  it('stops after the bounded retry budget', async () => {
    const failure = Object.assign(new Error('transaction write conflict'), {
      code: 'P2034',
    });
    const operation = jest.fn<Promise<never>, []>().mockRejectedValue(failure);

    await expect(
      retryInvitationSerializableTransaction(operation)
    ).rejects.toBe(failure);
    expect(operation).toHaveBeenCalledTimes(5);
  });
});
