/**
 * Minimal hand-rolled Prisma mock for unit tests that need to assert on
 * `prisma.<model>.<method>` calls without a live database.
 *
 * The control-plane test suite has no deep-mock helper (no jest-mock-extended
 * dependency); this mock only stubs the surface actually exercised by tests
 * that import it. Extend it as new call sites need mocking.
 */

export const prismaMock = {
  team: {
    update: jest.fn(),
  },
  $transaction: jest.fn(),
};

beforeEach(() => {
  jest.clearAllMocks();
});
