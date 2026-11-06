export const suiteContract = {
  schemaVersion: '1.0',
  area: 'assets',
  contractVersion: '1.0.0',
  workspace: 'activated-ephemeral',
  authentication: 'owner-storage-state',
  businessSeed: {
    command: 'seed_prerequisites.py',
    creates: ['library-chain', 'generation-run'],
    mechanism: 'application-command-path',
  },
  adapterModes: ['controlled_mock'],
  shortcuts: ['library', 'generation'],
  goldenPathSteps: [4],
  contractRefs: ['docs/product/v9-functional-alignment-acceptance.md'],
  specs: [
    'cross-workspace.spec.ts',
    'gallery-navigation.spec.ts',
    'review-mode.spec.ts',
    'export.spec.ts',
  ],
} as const;
