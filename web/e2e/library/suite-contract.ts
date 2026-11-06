export const suiteContract = {
  schemaVersion: '1.0',
  area: 'library',
  contractVersion: '1.0.0',
  workspace: 'activated-ephemeral',
  authentication: 'owner-storage-state',
  businessSeed: {
    command: 'seed_prerequisites.py',
    creates: [],
    mechanism: 'application-command-path',
  },
  adapterModes: ['customer_sandbox'],
  shortcuts: [],
  goldenPathSteps: [1],
  contractRefs: [
    'docs/product/creative-library-ownership-and-reuse-contract.md',
  ],
  specs: [
    'golden-path.spec.ts',
    'reference-transfer.spec.ts',
    'cross-workspace-security.spec.ts',
    'owner-member.spec.ts',
    'template-datasource.spec.ts',
    'version-history.spec.ts',
    'command-recovery.spec.ts',
    'accessibility-responsive.spec.ts',
  ],
} as const;
