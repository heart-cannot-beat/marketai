/* eslint-disable react-hooks/rules-of-hooks, no-empty-pattern */
import { test as base, expect } from '@playwright/test';
import { RuntimeSafety } from './runtime-safety';
import { ServiceControl } from './service-control';
import { EvidenceInspector } from './evidence-inspector';
import { EvidenceRecorder } from '../artifacts/evidence-recorder';
import * as crypto from 'node:crypto';
import { ProvisioningFixture } from './provisioning';
import { OnboardingHarness } from './onboarding-harness';
import { WorkspaceSetup } from './workspace-setup';
import { ProviderControlFixture } from './controlled-provider-fixtures';
import { MetaScenarioFixture } from './meta-scenario-fixtures';
import { LibraryDriver } from './area-drivers/library';
import { GenerationDriver } from './area-drivers/generation';
import { AssetsDriver } from './area-drivers/assets';
import { ExperimentsDriver } from './area-drivers/experiments';
import { LocalizationDriver } from './area-drivers/localization';
import { ScaleDriver } from './area-drivers/scale';

const BASE_URL = process.env.BASE_URL || 'http://web:4002';
const API_BASE_URL = process.env.API_BASE_URL || 'http://api:8787';
const DOMAIN_DB_URL =
  process.env.DOMAIN_DATABASE_URL ||
  'postgresql://marketai_domain_readonly:compose_domain_readonly@postgres:5432/marketai_domain';
const PROJECT_NAME = process.env.COMPOSE_PROJECT_NAME || '';
const ARTIFACT_DIR = process.env.ARTIFACT_DIR || '/results';
const WORKSPACE_SLUG = process.env.WORKSPACE_SLUG || '';

type GoldenPathFixtures = {
  runtimeSafety: RuntimeSafety;
  serviceControl: ServiceControl;
  evidenceInspector: EvidenceInspector;
  evidenceRecorder: EvidenceRecorder;
  provisioning: ProvisioningFixture;
  onboarding: OnboardingHarness;
  workspaceSetup: WorkspaceSetup;
  providerControl: ProviderControlFixture;
  metaScenarios: MetaScenarioFixture;
  library: LibraryDriver;
  generation: GenerationDriver;
  assets: AssetsDriver;
  experiments: ExperimentsDriver;
  localization: LocalizationDriver;
  scale: ScaleDriver;
  projectName: string;
  baseUrl: string;
  apiBaseUrl: string;
};

export const test = base.extend<GoldenPathFixtures>({
  projectName: [PROJECT_NAME, { option: true }],
  baseUrl: [BASE_URL, { option: true }],
  apiBaseUrl: [API_BASE_URL, { option: true }],

  serviceControl: async ({ projectName }, use) => {
    const control = new ServiceControl(projectName);
    await control.waitForBroker();
    await use(control);
  },

  runtimeSafety: async ({ baseUrl, serviceControl }, use) => {
    const safety = new RuntimeSafety({
      baseUrl,
      broker: {
        probeEgress: (pn, svc) => serviceControl.probeEgress(svc),
        inspectNetwork: (pn, svc) =>
          serviceControl.inspectNetwork(svc as 'web' | 'api' | 'worker'),
      },
    });
    await use(safety);
  },

  evidenceInspector: async ({}, use) => {
    const inspector = new EvidenceInspector(DOMAIN_DB_URL);
    await use(inspector);
  },

  evidenceRecorder: async ({}, use) => {
    const commitSha = process.env.COMMIT_SHA || 'unknown';
    const runId = `gp-${Date.now()}`;
    const recorder = new EvidenceRecorder({
      runId,
      commitSha,
      outputDir: ARTIFACT_DIR,
    });
    await use(recorder);
    recorder.finalize();
  },

  provisioning: async ({}, use) => {
    const signingKeyId = `gp-${Date.now()}`;
    const signingKey = crypto.randomBytes(32);
    const fixture = new ProvisioningFixture({ signingKeyId, signingKey });
    await use(fixture);
  },

  onboarding: async ({ browser, baseUrl, runtimeSafety }, use) => {
    const harness = new OnboardingHarness({
      browser,
      baseUrl,
      runtimeSafety,
    });
    await use(harness);
    await harness.closeAll();
  },

  workspaceSetup: async ({ baseUrl }, use) => {
    await use(new WorkspaceSetup(baseUrl));
  },

  providerControl: async ({}, use) => {
    const fixture = new ProviderControlFixture();
    await use(fixture);
  },

  metaScenarios: async ({}, use) => {
    await use(new MetaScenarioFixture(WORKSPACE_SLUG));
  },

  library: async ({}, use) => {
    await use(new LibraryDriver());
  },

  generation: async ({}, use) => {
    await use(new GenerationDriver(WORKSPACE_SLUG));
  },

  assets: async ({}, use) => {
    await use(new AssetsDriver(WORKSPACE_SLUG));
  },

  experiments: async ({}, use) => {
    await use(new ExperimentsDriver(WORKSPACE_SLUG));
  },

  localization: async ({}, use) => {
    await use(new LocalizationDriver(WORKSPACE_SLUG));
  },

  scale: async ({}, use) => {
    await use(new ScaleDriver(WORKSPACE_SLUG));
  },
});

export { expect };
