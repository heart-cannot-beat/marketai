import path from 'node:path';
import { Readable } from 'node:stream';
import { spawn } from 'node:child_process';

const COMMAND_PATH = path.resolve(
  __dirname,
  '../../../scripts/golden-path-provisioning.ts'
);
const NODE_BIN = process.env.NODE_BIN || 'node';
const TIMEOUT_MS = 60_000;
const MAX_BUFFER = 64 * 1024;

export interface BeginProvisioningInput {
  idempotencyKey: string;
  workspaceName: string;
  workspaceSlug?: string;
  ownerEmail: string;
  operatorIdentifier: string;
  betaPlanVersionId: string;
  namespace: string;
}

export interface SecretInvitationCapability {
  readonly token: string;
  readonly invitationPath: string;
  readonly email: string;
}

export interface BeginProvisioningResult {
  provisioningRef: string;
  workspaceRef: string;
  workspaceSlug: string;
  replay: boolean;
}

export interface CompleteProvisioningResult {
  provisioningRef: string;
  workspaceRef: string;
  workspaceSlug: string;
  ownerMembershipRef: string;
  provisioningSnapshotVersion: number;
  activeSnapshotVersion: number;
}

export class ProvisioningFixture {
  private readonly signingKeyId: string;
  private readonly signingKey: Buffer;

  constructor(opts: { signingKeyId: string; signingKey: Buffer }) {
    this.signingKeyId = opts.signingKeyId;
    this.signingKey = opts.signingKey;
  }

  async begin(input: BeginProvisioningInput): Promise<{
    result: BeginProvisioningResult;
    ownerInvitation: SecretInvitationCapability;
  }> {
    const stdinPayload = JSON.stringify({
      ...input,
      signingKeyId: this.signingKeyId,
      signingKey: this.signingKey.toString('base64'),
    });

    const { stdout, fd3 } = await this.execWithFd3('begin', stdinPayload);

    const result: BeginProvisioningResult = JSON.parse(stdout);
    const ownerInvitation: SecretInvitationCapability = {
      ...(JSON.parse(fd3) as Omit<SecretInvitationCapability, 'email'>),
      email: input.ownerEmail,
    };

    return { result, ownerInvitation };
  }

  async complete(input: {
    provisioningRef: string;
  }): Promise<CompleteProvisioningResult> {
    const stdinPayload = JSON.stringify({
      provisioningRef: input.provisioningRef,
      signingKeyId: this.signingKeyId,
      signingKey: this.signingKey.toString('base64'),
    });

    const { stdout } = await this.execWithFd3('complete', stdinPayload);
    return JSON.parse(stdout);
  }

  private execWithFd3(
    subcommand: string,
    stdinData: string
  ): Promise<{ stdout: string; fd3: string }> {
    return new Promise((resolve, reject) => {
      const child = spawn(
        NODE_BIN,
        [
          '--require',
          'ts-node/register',
          '--eval',
          'require(process.argv[1])',
          COMMAND_PATH,
          subcommand,
        ],
        {
          stdio: ['pipe', 'pipe', 'pipe', 'pipe'],
          timeout: TIMEOUT_MS,
          env: {
            ...process.env,
            TS_NODE_COMPILER_OPTIONS: JSON.stringify({ module: 'commonjs' }),
          },
        }
      );

      let stdout = '';
      let stderr = '';
      let fd3Data = '';

      child.stdout!.on('data', (chunk: Buffer) => {
        if (stdout.length < MAX_BUFFER) {
          stdout += chunk.toString();
        }
      });

      child.stderr!.on('data', (chunk: Buffer) => {
        if (stderr.length < MAX_BUFFER) {
          stderr += chunk.toString();
        }
      });

      const fd3Stream = child.stdio[3] as Readable;
      if (fd3Stream) {
        fd3Stream.on('data', (chunk: Buffer) => {
          if (fd3Data.length < MAX_BUFFER) {
            fd3Data += chunk.toString();
          }
        });
      }

      child.on('close', (code) => {
        if (code !== 0) {
          const errorCode = stderr.trim() || 'error_code:missing';
          reject(
            new Error(
              `Provisioning ${subcommand} failed with code ${code}: ${errorCode}`
            )
          );
          return;
        }
        resolve({ stdout: stdout.trim(), fd3: fd3Data.trim() });
      });

      child.on('error', (err) => {
        reject(new Error(`Provisioning ${subcommand} error: ${err.message}`));
      });

      child.stdin!.write(stdinData);
      child.stdin!.end();
    });
  }
}
