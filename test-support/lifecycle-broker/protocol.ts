export type RuntimeService = 'web' | 'api' | 'worker';
export type EgressProbeService = RuntimeService | 'controlled-provider';

export const RUNTIME_SERVICES: ReadonlySet<string> = new Set([
  'web',
  'api',
  'worker',
]);
export const EGRESS_PROBE_SERVICES: ReadonlySet<string> = new Set([
  'web',
  'api',
  'worker',
  'controlled-provider',
]);
export const ACTIONS: ReadonlySet<string> = new Set([
  'stop',
  'start',
  'kill',
  'restart',
  'status',
  'inspect_network',
  'probe_egress',
]);
const PROJECT_NAME_PATTERN = /^gp-[a-z0-9]+-\d+$/;

export interface BrokerRequest {
  action: string;
  service: string;
  signal?: string;
  projectName: string;
}

export interface NetworkAttestation {
  service: string;
  networkName: string;
  networkHash: string;
  attachmentCount: number;
  singleNetwork: boolean;
  internalOnly: boolean;
  noPublishedPorts: boolean;
}

export interface EgressAttestation {
  service: string;
  dnsResolved: boolean;
  ipv4Connected: boolean;
  ipv6Connected: boolean;
}

export interface BrokerResponse {
  ok: boolean;
  service: string;
  state?: string;
  network_attestation?: NetworkAttestation;
  egress_attestation?: EgressAttestation;
  error_code?: string;
}

export function validateRequest(req: BrokerRequest): string | null {
  if (!PROJECT_NAME_PATTERN.test(req.projectName)) {
    return 'invalid_project_name';
  }
  if (!ACTIONS.has(req.action)) {
    return 'unknown_action';
  }
  if (req.action === 'probe_egress') {
    if (!EGRESS_PROBE_SERVICES.has(req.service)) {
      return 'unknown_service';
    }
  } else {
    if (!RUNTIME_SERVICES.has(req.service)) {
      return 'unknown_service';
    }
  }
  if (req.action === 'kill' && req.signal !== 'SIGKILL') {
    return 'invalid_signal';
  }
  return null;
}
