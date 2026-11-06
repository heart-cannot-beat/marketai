import net from 'node:net';

type RuntimeService = 'web' | 'api' | 'worker';

const BROKER_SOCKET_PATH =
  process.env.BROKER_SOCKET_PATH || '/var/run/lifecycle-broker/broker.sock';
const CONNECT_TIMEOUT_MS = 10_000;
const REQUEST_TIMEOUT_MS = 30_000;

interface BrokerResponse {
  ok: boolean;
  service?: string;
  state?: string;
  network_attestation?: {
    service: string;
    networkName: string;
    networkId: string;
    attachmentCount: number;
    singleNetwork: boolean;
    internalOnly: boolean;
  };
  egress_attestation?: {
    service: string;
    dnsResolved: boolean;
    ipv4Connected: boolean;
    ipv6Connected: boolean;
  };
  error_code?: string;
}

function sendBrokerRequest(
  socketPath: string,
  request: Record<string, unknown>
): Promise<BrokerResponse> {
  return new Promise((resolve, reject) => {
    const socket = new net.Socket();
    const timer = setTimeout(() => {
      socket.destroy();
      reject(new Error('Broker request timeout'));
    }, REQUEST_TIMEOUT_MS);

    socket.connect(socketPath, () => {
      socket.write(JSON.stringify(request) + '\n');
    });

    let data = '';
    socket.on('data', (chunk) => {
      data += chunk.toString();
      if (data.includes('\n')) {
        clearTimeout(timer);
        socket.destroy();
        resolve(JSON.parse(data.trim()));
      }
    });
    socket.on('error', (err) => {
      clearTimeout(timer);
      reject(err);
    });
  });
}

export class ServiceControl {
  private readonly projectName: string;
  private readonly socketPath: string;

  constructor(projectName: string, socketPath?: string) {
    this.projectName = projectName;
    this.socketPath = socketPath || BROKER_SOCKET_PATH;
  }

  async waitForBroker(): Promise<void> {
    const deadline = Date.now() + CONNECT_TIMEOUT_MS;
    while (Date.now() < deadline) {
      try {
        await sendBrokerRequest(this.socketPath, {
          action: 'status',
          service: 'web',
          projectName: this.projectName,
        });
        return;
      } catch {
        await new Promise((r) => setTimeout(r, 500));
      }
    }
    throw new Error('Broker not reachable within timeout');
  }

  async stop(service: RuntimeService): Promise<void> {
    const res = await sendBrokerRequest(this.socketPath, {
      action: 'stop',
      service,
      projectName: this.projectName,
    });
    if (!res.ok) {
      throw new Error(`Failed to stop ${service}: ${res.error_code}`);
    }
  }

  async start(service: RuntimeService): Promise<void> {
    const res = await sendBrokerRequest(this.socketPath, {
      action: 'start',
      service,
      projectName: this.projectName,
    });
    if (!res.ok) {
      throw new Error(`Failed to start ${service}: ${res.error_code}`);
    }
  }

  async kill(service: 'worker', signal: 'SIGKILL'): Promise<void> {
    const res = await sendBrokerRequest(this.socketPath, {
      action: 'kill',
      service,
      signal,
      projectName: this.projectName,
    });
    if (!res.ok) {
      throw new Error(`Failed to kill ${service}: ${res.error_code}`);
    }
  }

  async restart(service: RuntimeService): Promise<void> {
    const res = await sendBrokerRequest(this.socketPath, {
      action: 'restart',
      service,
      projectName: this.projectName,
    });
    if (!res.ok) {
      throw new Error(`Failed to restart ${service}: ${res.error_code}`);
    }
  }

  async status(
    service: RuntimeService
  ): Promise<{ ok: boolean; state?: string }> {
    return sendBrokerRequest(this.socketPath, {
      action: 'status',
      service,
      projectName: this.projectName,
    });
  }

  async inspectNetwork(service: RuntimeService): Promise<{
    service: string;
    networkName: string;
    attachmentCount: number;
    singleNetwork: boolean;
    internalOnly: boolean;
  }> {
    const res = await sendBrokerRequest(this.socketPath, {
      action: 'inspect_network',
      service,
      projectName: this.projectName,
    });
    if (!res.ok || !res.network_attestation) {
      throw new Error(
        `Failed to inspect network for ${service}: ${res.error_code}`
      );
    }
    return res.network_attestation;
  }

  async probeEgress(service: string): Promise<{
    dnsResolved: boolean;
    ipv4Connected: boolean;
    ipv6Connected: boolean;
  }> {
    const res = await sendBrokerRequest(this.socketPath, {
      action: 'probe_egress',
      service,
      projectName: this.projectName,
    });
    if (!res.ok || !res.egress_attestation) {
      throw new Error(
        `Failed to probe egress for ${service}: ${res.error_code}`
      );
    }
    return res.egress_attestation;
  }
}
