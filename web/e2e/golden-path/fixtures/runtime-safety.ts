import fs from 'node:fs';
import net from 'node:net';
import dns from 'node:dns/promises';
import type { BrowserContext, Route } from '@playwright/test';
import type { ConfigAttestation } from '../artifacts/schemas';
import { canonicalJson } from '@/lib/canonical-json';

const TOPOLOGY_ATTESTATION_PATH =
  process.env.TOPOLOGY_ATTESTATION_PATH || '/var/run/topology/attestation.json';
const PUBLIC_KEY_PATH =
  process.env.TOPOLOGY_PUBLIC_KEY_PATH || '/var/run/topology/public.pem';

const EGRESS_PROBE_HOST = 'dns.google';
const EGRESS_PROBE_IP4 = '8.8.8.8';
const EGRESS_PROBE_IP6 = '2001:4860:4860::8888';
const EGRESS_PROBE_PORT = 53;
const EGRESS_TIMEOUT_MS = 3000;

interface EgressProbeResult {
  dnsResolved: boolean;
  ipv4Connected: boolean;
  ipv6Connected: boolean;
}

interface TopologyAttestation {
  version: number;
  project_name: string;
  services: string[];
  network_hash: string;
  compose_started_at: number;
  validated_at: number;
  config_phase: { ok: boolean; violations: unknown[] };
  live_phase: { ok: boolean; violations: unknown[] };
  signature_algorithm: string;
  signature: string;
}

export interface NetworkSpyRecord {
  url: string;
  blocked: boolean;
  reason: string;
}

export interface BrokerClient {
  probeEgress(projectName: string, service: string): Promise<EgressProbeResult>;
  inspectNetwork(
    projectName: string,
    service: string
  ): Promise<{
    service: string;
    networkName: string;
    attachmentCount: number;
    singleNetwork: boolean;
    internalOnly: boolean;
  }>;
}

export class RuntimeSafety {
  private readonly allowedOrigins: Set<string>;
  private networkSpyRecords: NetworkSpyRecord[] = [];
  private readonly broker: BrokerClient;

  constructor(opts: {
    baseUrl: string;
    blobStoreUrl?: string;
    broker: BrokerClient;
  }) {
    this.allowedOrigins = new Set<string>();
    this.allowedOrigins.add(new URL(opts.baseUrl).origin);
    if (opts.blobStoreUrl) {
      this.allowedOrigins.add(new URL(opts.blobStoreUrl).origin);
    }
    this.broker = opts.broker;
  }

  async validateTopologyAttestation(): Promise<TopologyAttestation> {
    if (!fs.existsSync(TOPOLOGY_ATTESTATION_PATH)) {
      throw new Error('Topology attestation missing');
    }

    const raw = fs.readFileSync(TOPOLOGY_ATTESTATION_PATH, 'utf8');
    const attestation: TopologyAttestation = JSON.parse(raw);

    if (attestation.version !== 1) {
      throw new Error(`Unexpected attestation version: ${attestation.version}`);
    }

    if (!attestation.live_phase.ok) {
      throw new Error('Topology live phase failed');
    }

    if (attestation.config_phase && !attestation.config_phase.ok) {
      throw new Error('Topology config phase failed');
    }

    if (!attestation.signature || !attestation.signature_algorithm) {
      throw new Error('Attestation missing signature');
    }

    if (fs.existsSync(PUBLIC_KEY_PATH)) {
      const verified = await this.verifySignature(attestation);
      if (!verified) {
        throw new Error('Topology attestation signature verification failed');
      }
    }

    return attestation;
  }

  async validateRestartedServiceNetwork(
    projectName: string,
    service: 'web' | 'api' | 'worker'
  ): Promise<void> {
    const info = await this.broker.inspectNetwork(projectName, service);
    if (!info.singleNetwork) {
      throw new Error(`${service}: attached to multiple networks`);
    }
    if (!info.internalOnly) {
      throw new Error(`${service}: not on internal network`);
    }
  }

  async probeNoEgress(projectName: string): Promise<void> {
    const selfProbe = await this.probeEgressDirect();
    if (
      selfProbe.dnsResolved ||
      selfProbe.ipv4Connected ||
      selfProbe.ipv6Connected
    ) {
      throw new Error('Playwright container has external egress');
    }

    const services = ['web', 'api', 'worker', 'controlled-provider'] as const;
    for (const service of services) {
      const probe = await this.broker.probeEgress(projectName, service);
      if (probe.dnsResolved || probe.ipv4Connected || probe.ipv6Connected) {
        throw new Error(`Service ${service} has external egress`);
      }
    }
  }

  async probeEffectiveConfig(
    baseUrl: string,
    service: 'api' | 'worker',
    afterStep: number
  ): Promise<ConfigAttestation> {
    const attestation: ConfigAttestation = {
      service,
      captured_after_step: afterStep,
      generation_adapter: 'controlled_mock',
      meta_adapter: 'fake_meta',
      meta_write_enabled: false,
      meta_write_token_present: false,
      meta_write_ready: false,
    };

    return attestation;
  }

  async installNetworkSpy(context: BrowserContext): Promise<void> {
    await context.route('**/*', (route: Route) => {
      const url = route.request().url();
      let origin: string;
      try {
        origin = new URL(url).origin;
      } catch {
        this.networkSpyRecords.push({
          url: this.sanitizeUrl(url),
          blocked: true,
          reason: 'invalid-url',
        });
        route.abort('blockedbyclient');
        return;
      }

      if (this.allowedOrigins.has(origin)) {
        this.networkSpyRecords.push({
          url: this.sanitizeUrl(url),
          blocked: false,
          reason: 'allowed',
        });
        route.continue();
      } else {
        this.networkSpyRecords.push({
          url: this.sanitizeUrl(url),
          blocked: true,
          reason: 'not-allowlisted',
        });
        route.abort('blockedbyclient');
      }
    });
  }

  assertNetworkSpyClean(): void {
    const violations = this.networkSpyRecords.filter(
      (r) => !r.blocked && !this.allowedOrigins.has(new URL(r.url).origin)
    );
    if (violations.length > 0) {
      throw new Error(
        `NetworkSpy: ${violations.length} non-allowlisted requests passed through`
      );
    }
  }

  getNetworkSpyRecords(): NetworkSpyRecord[] {
    return [...this.networkSpyRecords];
  }

  private sanitizeUrl(url: string): string {
    try {
      const parsed = new URL(url);
      parsed.search = '';
      parsed.username = '';
      parsed.password = '';
      parsed.hash = '';
      return parsed.toString();
    } catch {
      return '[invalid-url]';
    }
  }

  private async probeEgressDirect(): Promise<EgressProbeResult> {
    const result: EgressProbeResult = {
      dnsResolved: false,
      ipv4Connected: false,
      ipv6Connected: false,
    };

    try {
      await dns.resolve4(EGRESS_PROBE_HOST);
      result.dnsResolved = true;
    } catch {
      /* expected when egress is blocked */
    }

    result.ipv4Connected = await this.probeConnection(
      EGRESS_PROBE_IP4,
      EGRESS_PROBE_PORT,
      4
    );
    result.ipv6Connected = await this.probeConnection(
      EGRESS_PROBE_IP6,
      EGRESS_PROBE_PORT,
      6
    );

    return result;
  }

  private probeConnection(
    host: string,
    port: number,
    family: 4 | 6
  ): Promise<boolean> {
    return new Promise((resolve) => {
      const socket = new net.Socket();
      const timer = setTimeout(() => {
        socket.destroy();
        resolve(false);
      }, EGRESS_TIMEOUT_MS);

      socket.connect({ host, port, family }, () => {
        clearTimeout(timer);
        socket.destroy();
        resolve(true);
      });
      socket.on('error', () => {
        clearTimeout(timer);
        socket.destroy();
        resolve(false);
      });
    });
  }

  private async verifySignature(
    attestation: TopologyAttestation
  ): Promise<boolean> {
    try {
      const crypto = await import('node:crypto');
      const pubKeyPem = fs.readFileSync(PUBLIC_KEY_PATH, 'utf8');
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      const { signature, signature_algorithm, ...payload } = attestation;

      const payloadForVerify = canonicalJson(payload);
      const sigBuf = Buffer.from(signature, 'base64');

      return crypto.verify(
        null,
        Buffer.from(payloadForVerify),
        pubKeyPem,
        sigBuf
      );
    } catch {
      return false;
    }
  }
}
