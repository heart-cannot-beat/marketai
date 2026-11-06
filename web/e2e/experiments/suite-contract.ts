export const suiteContract = {
  schemaVersion: '1.0',
  area: 'experiments',
  contractVersion: '1.0.0',
  workspace: 'activated-ephemeral',
  authentication: 'owner-storage-state',
  businessSeed: {
    command: 'seed_prerequisites.py',
    creates: ['library-chain', 'generation-run', 'approved-assets'],
    mechanism: 'application-command-path',
  },
  adapterModes: ['customer_sandbox', 'controlled_mock'],
  shortcuts: ['library', 'generation', 'assets'],
  goldenPathSteps: [5, 6],
  contractRefs: ['docs/product/v9-functional-alignment-acceptance.md'],
  specs: [
    'judgement-winner.spec.ts',
    'authorization-mode.spec.ts',
    'duplicate-resolution.spec.ts',
    'closed-state-evidence.spec.ts',
    'launch-recovery.spec.ts',
  ],
} as const;
