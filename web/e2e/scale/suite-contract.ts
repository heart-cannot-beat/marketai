export const suiteContract = {
  schemaVersion: '1.0',
  area: 'scale',
  contractVersion: '1.0.0',
  workspace: 'activated-ephemeral',
  authentication: 'owner-storage-state',
  businessSeed: {
    command: 'seed_prerequisites.py',
    creates: ['confirmed-winner', 'delivery-target', 'policy-chain'],
    mechanism: 'application-command-path',
  },
  adapterModes: ['customer_sandbox', 'controlled_mock'],
  shortcuts: ['library', 'generation', 'assets', 'experiments'],
  goldenPathSteps: [8, 10],
  contractRefs: ['docs/product/scale-advice-semantics-contract.md'],
  specs: ['controlled-mock-journey.spec.ts', 'customer-sandbox-smoke.spec.ts'],
} as const;
