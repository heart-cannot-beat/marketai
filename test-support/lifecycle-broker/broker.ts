import net from 'node:net';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import {
  validateRequest,
  type BrokerRequest,
  type BrokerResponse,
  type NetworkAttestation,
  type EgressAttestation,
} from './protocol';

const exec = promisify(execFile);

async function dockerCompose(
  projectName: string,
  ...args: string[]
): Promise<string> {
  const { stdout } = await exec('docker', [
    'compose',
    '-p',
    projectName,
    ...args,
  ]);
  return stdout.trim();
}

async function getContainerId(
  projectName: string,
  service: string
): Promise<string> {
  const id = await dockerCompose(projectName, 'ps', '-q', service);
  if (!id) throw new Error(`Container for ${service} not found`);
  return id;
}

async function handleAction(
  req: BrokerRequest
): Promise<BrokerResponse> {
  const { action, service, projectName } = req;

  switch (action) {
    case 'stop':
      await dockerCompose(projectName, 'stop', service);
      return { ok: true, service, state: 'stopped' };

    case 'start':
      await dockerCompose(projectName, 'start', service);
      return { ok: true, service, state: 'running' };

    case 'kill': {
      const containerId = await getContainerId(projectName, service);
      await exec('docker', ['kill', '--signal', 'SIGKILL', containerId]);
      return { ok: true, service, state: 'killed' };
    }

    case 'restart':
      await dockerCompose(projectName, 'restart', service);
      return { ok: true, service, state: 'running' };

    case 'status': {
      const raw = await dockerCompose(
        projectName,
        'ps',
        '--format',
        'json',
        service
      );
      const info = JSON.parse(raw);
      return { ok: true, service, state: info.State || info.status || 'unknown' };
    }

    case 'inspect_network': {
      const containerId = await getContainerId(projectName, service);
      const { stdout: inspectOut } = await exec('docker', [
        'inspect',
        '--format',
        '{{json .NetworkSettings}}',
        containerId,
      ]);
      const netSettings = JSON.parse(inspectOut);
      const networks = netSettings.Networks || {};
      const networkNames = Object.keys(networks);
      const firstNet = networks[networkNames[0]] || {};
      const { stdout: portOut } = await exec('docker', [
        'inspect',
        '--format',
        '{{json .NetworkSettings.Ports}}',
        containerId,
      ]);
      const ports = JSON.parse(portOut) || {};
      const hasPublishedPorts = Object.values(ports).some(
        (bindings: any) =>
          Array.isArray(bindings) &&
          bindings.some((b: any) => b.HostPort)
      );

      const attestation: NetworkAttestation = {
        service,
        networkName: networkNames[0] || '',
        networkHash: firstNet.NetworkID?.slice(0, 12) || '',
        attachmentCount: networkNames.length,
        singleNetwork: networkNames.length === 1,
        internalOnly: !hasPublishedPorts && networkNames.length === 1,
        noPublishedPorts: !hasPublishedPorts,
      };
      return { ok: true, service, network_attestation: attestation };
    }

    case 'probe_egress': {
      const containerId = await getContainerId(projectName, service);
      const probe = async (cmd: string): Promise<boolean> => {
        try {
          await exec('docker', [
            'exec',
            containerId,
            'sh',
            '-c',
            cmd,
          ]);
          return true;
        } catch {
          return false;
        }
      };
      const attestation: EgressAttestation = {
        service,
        dnsResolved: await probe(
          'getent hosts dns.google 2>/dev/null && echo ok'
        ),
        ipv4Connected: await probe(
          'wget -q -O /dev/null --timeout=2 http://1.1.1.1/ 2>/dev/null'
        ),
        ipv6Connected: await probe(
          'wget -q -O /dev/null --timeout=2 http://[2606:4700:4700::1111]/ 2>/dev/null'
        ),
      };
      return { ok: true, service, egress_attestation: attestation };
    }

    default:
      return { ok: false, service, error_code: 'unknown_action' };
  }
}

export function createBroker(socketPath: string): net.Server {
  const server = net.createServer((socket) => {
    let buffer = '';
    socket.on('data', (chunk) => {
      buffer += chunk.toString();
      const lines = buffer.split('\n');
      buffer = lines.pop()!;
      for (const line of lines) {
        if (!line.trim()) continue;
        void (async () => {
          try {
            const req = JSON.parse(line) as BrokerRequest;
            const validationError = validateRequest(req);
            if (validationError) {
              socket.write(
                JSON.stringify({
                  ok: false,
                  service: req.service,
                  error_code: validationError,
                }) + '\n'
              );
              return;
            }
            const response = await handleAction(req);
            socket.write(JSON.stringify(response) + '\n');
          } catch (err) {
            socket.write(
              JSON.stringify({
                ok: false,
                service: 'unknown',
                error_code: 'internal_error',
              }) + '\n'
            );
          }
        })();
      }
    });
  });
  server.listen(socketPath);
  return server;
}

if (require.main === module) {
  const socketPath =
    process.env.BROKER_SOCKET || '/tmp/lifecycle-broker.sock';
  const server = createBroker(socketPath);
  process.stdout.write(`lifecycle-broker listening on ${socketPath}\n`);
  process.on('SIGTERM', () => server.close());
}
