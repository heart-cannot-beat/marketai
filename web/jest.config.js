// jest.config.js
const nextJest = require('next/jest');

const createJestConfig = nextJest({
  dir: './',
});

/** @type {import('jest').Config} */
const customJestConfig = {
  moduleDirectories: ['node_modules', '<rootDir>/'],
  // Next's SWC transform rewrites `@/...` alias imports at the AST level, so
  // plain `import ... from '@/lib/x'` resolves without this. `jest.mock('@/lib/x',
  // ...)` passes a bare string literal that the transform never touches, so
  // without an explicit mapper here module resolution fails at the literal
  // "@/lib/x" specifier. This mirrors the tsconfig `@/*` alias for both import
  // forms.
  moduleNameMapper: {
    '^@/(.*)$': '<rootDir>/$1',
  },
  setupFiles: ['<rootDir>/jest.env.js'],
  setupFilesAfterEnv: ['<rootDir>/jest.setup.js'],
  // Control-plane tests exercise API handlers, middleware helpers and the
  // hardening manifest; none of them need a DOM.
  testEnvironment: 'node',
  // `__tests__/**/*.spec.ts(x)` is the pre-existing and Workspace shell suite.
  // `tests/**/*.test.ts` is the entitlements suite (ticket #14 Task 3 onward),
  // kept in its own top-level directory and naming convention per the
  // implementation plan rather than folded into `__tests__`.
  testMatch: [
    '<rootDir>/__tests__/**/*.spec.ts',
    '<rootDir>/__tests__/**/*.spec.tsx',
    '<rootDir>/tests/**/*.test.ts',
    '<rootDir>/tests/**/*.test.tsx',
  ],
};

module.exports = createJestConfig(customJestConfig);
