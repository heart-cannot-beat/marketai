export const suiteContract = {
  schemaVersion: '1.0',
  area: 'generation',
  contractVersion: '1.0.0',
  workspace: 'activated-ephemeral',
  authentication: 'owner-storage-state',
  businessSeed: {
    command: 'seed_prerequisites.py',
    creates: ['library-chain'],
    mechanism: 'application-command-path',
  },
  adapterModes: ['controlled_mock'],
  shortcuts: ['library'],
  goldenPathSteps: [2, 3],
  contractRefs: ['docs/product/v9-functional-alignment-acceptance.md'],
  specs: [
    'golden-path.spec.ts',
    'draft-preview.spec.ts',
    'recovery.spec.ts',
    'submission-progress.spec.ts',
  ],
} as const;
