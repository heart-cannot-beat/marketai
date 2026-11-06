export const suiteContract = {
  schemaVersion: '1.0',
  area: 'localization',
  contractVersion: '1.0.0',
  workspace: 'activated-ephemeral',
  authentication: 'owner-storage-state',
  businessSeed: {
    command: 'seed_prerequisites.py',
    creates: ['confirmed-winner', 'delivery-target'],
    mechanism: 'application-command-path',
  },
  adapterModes: ['customer_sandbox', 'controlled_mock'],
  shortcuts: ['library', 'generation', 'assets', 'experiments'],
  goldenPathSteps: [7],
  contractRefs: ['docs/product/localization-truth-package-contract.md'],
  specs: [
    'golden-path.spec.ts',
    'partial-retry.spec.ts',
    'isolated-recovery.spec.ts',
    'authorization.spec.ts',
    'recovery.spec.ts',
  ],
} as const;
