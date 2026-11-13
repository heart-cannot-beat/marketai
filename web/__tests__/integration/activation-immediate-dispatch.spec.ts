import { createHash, randomUUID } from 'node:crypto';
import http from 'node:http';
import type { AddressInfo } from 'node:net';

export {};

const controlUrl = process.env.CONTROL_PLANE_DATABASE_URL;
const describeIfConfigured = controlUrl ? describe : describe.skip;

const PUSH_TOKEN = 'immediate-dispatch-test-push-token';
const SIGNING = {
  signingKeyId: 'test-key',
  signingKey: Buffer.from('test-signing-key-at-least-32-bytes'),
};

type Delivery = {
  teamId: string;
  authorization: string | undefined;
  body: Buffer;
};

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * #9: the domain has no active row for a Workspace until its Snapshot is
 * delivered. These tests stand a fake domain inbox up over real HTTP and
 * never run the scheduled dispatcher unless a test says so.
 */
describeIfConfigured('immediate entitlement dispatch (PostgreSQL)', () => {
  jest.setTimeout(45_000);

  let prisma: any;
  let provisionFirstOwnerWorkspace: any;
  let activateWorkspaceAndPublish: any;
  let reconcileProvisioningWorkspace: any;
  let claimAndDispatch: any;
  let server: http.Server;
  let domainBaseUrl: string;
  const deliveries: Delivery[] = [];
  // Per test: how the fake inbox answers a delivery for a Workspace.
  let respond: (teamId: string) => Promise<number> = async () => 200;

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on('data', (chunk: Buffer) => chunks.push(chunk));
      req.on('end', () => {
        const match =
          /^\/api\/internal\/domain\/workspaces\/([^/]+)\/entitlement-inbox$/.exec(
            req.url ?? ''
          );
        const teamId = match?.[1] ?? '';
        deliveries.push({
          teamId,
          authorization: req.headers.authorization,
          body: Buffer.concat(chunks),
        });
        void respond(teamId).then((status) => {
          res.statusCode = status;
          res.end();
        });
      });
    });
    await new Promise<void>((resolve) =>
      server.listen(0, '127.0.0.1', resolve)
    );
    domainBaseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

    process.env.DATABASE_URL = controlUrl as string;
    process.env.DOMAIN_API_BASE_URL = domainBaseUrl;
    process.env.CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN = PUSH_TOKEN;
    ({ prisma } = await import('../../lib/prisma'));
    ({
      provisionFirstOwnerWorkspace,
      activateWorkspaceAndPublish,
      reconcileProvisioningWorkspace,
    } = await import('../../lib/onboarding'));
    ({ claimAndDispatch } = await import('../../lib/entitlements/dispatcher'));
  });

  afterAll(async () => {
    server?.closeAllConnections();
    await new Promise((resolve) => server?.close(resolve));
    await prisma?.$disconnect();
  });

  beforeEach(() => {
    respond = async () => 200;
    jest.spyOn(console, 'log').mockImplementation(() => undefined);
    jest.spyOn(console, 'warn').mockImplementation(() => undefined);
  });

  afterEach(() => jest.restoreAllMocks());

  /** The scheduled dispatcher's own call (`scripts/dispatch-entitlements.ts`). */
  const scheduledRun = (id: string) =>
    claimAndDispatch(prisma, id, {
      domainBaseUrl,
      pushToken: PUSH_TOKEN,
      batchSize: 100,
    });

  const provision = async () => {
    const suffix = randomUUID().slice(0, 8);
    const operator = await prisma.user.create({
      data: {
        name: 'Operator',
        email: `operator-${suffix}@acme-charge.example`,
      },
    });
    const entitlements = { controlled_mock: true };
    const plan = await prisma.planVersion.create({
      data: {
        planKey: `beta-dispatch-${suffix}`,
        versionNo: 1,
        displayName: `Beta ${suffix}`,
        state: 'EFFECTIVE',
        entitlements,
        entitlementsHash: createHash('sha256')
          .update(JSON.stringify(entitlements))
          .digest('hex'),
        publishedBy: 'operator-plan',
      },
    });
    const provisioned = await provisionFirstOwnerWorkspace(prisma, {
      name: `Acme Charge ${suffix}`,
      slug: `acme-charge-${suffix}`,
      ownerEmail: `owner-${suffix}@acme-charge.example`,
      operatorUserId: operator.id,
      betaPlanVersionId: plan.id,
      ...SIGNING,
    });
    return { teamId: provisioned.team.id as string, suffix };
  };

  const addVerifiedOwner = async (teamId: string, suffix: string) => {
    const owner = await prisma.user.create({
      data: {
        name: 'First Owner',
        email: `owner-${suffix}@acme-charge.example`,
        emailVerified: new Date(),
      },
    });
    await prisma.teamMember.create({
      data: { teamId, userId: owner.id, role: 'OWNER' },
    });
    await prisma.mfaCredential.create({
      data: {
        userId: owner.id,
        protectedSecret: { ciphertext: 'redacted' },
        verifiedAt: new Date(),
      },
    });
  };

  const outboxOf = (teamId: string) =>
    prisma.entitlementSnapshotOutbox.findMany({
      where: { teamId },
      orderBy: { createdAt: 'asc' },
      include: {
        snapshot: { select: { lifecycle: true, canonicalEnvelope: true } },
      },
    });

  const deliveriesOf = (teamId: string) =>
    deliveries.filter((delivery) => delivery.teamId === teamId);

  /** How many times each of the Workspace's Snapshots reached the inbox. */
  const timesDelivered = async (teamId: string) => {
    const rows = await outboxOf(teamId);
    return rows.map(
      (row: any) =>
        deliveriesOf(teamId).filter((delivery) =>
          delivery.body.equals(Buffer.from(row.snapshot.canonicalEnvelope))
        ).length
    );
  };

  it('delivers the provisioning and the active Snapshot without a dispatcher run', async () => {
    const { teamId, suffix } = await provision();

    // Activation waits for the provisioning Snapshot to have been delivered.
    const [provisioning] = await outboxOf(teamId);
    expect(provisioning).toMatchObject({ deliveryState: 'DELIVERED' });

    await addVerifiedOwner(teamId, suffix);
    // The path MFA enrollment and invitation acceptance take.
    await expect(
      reconcileProvisioningWorkspace(prisma, { teamId, ...SIGNING })
    ).resolves.toBe('activated');

    const rows = await outboxOf(teamId);
    expect(
      rows.map((row: any) => [row.snapshot.lifecycle, row.deliveryState])
    ).toEqual([
      ['PROVISIONING', 'DELIVERED'],
      ['ACTIVE', 'DELIVERED'],
    ]);
    expect(await timesDelivered(teamId)).toEqual([1, 1]);
    expect(
      deliveriesOf(teamId).map((delivery) => delivery.authorization)
    ).toEqual([`Bearer ${PUSH_TOKEN}`, `Bearer ${PUSH_TOKEN}`]);
  });

  it('delivers the active Snapshot right after an operator completes provisioning', async () => {
    const { teamId } = await provision();

    await expect(
      activateWorkspaceAndPublish(prisma, { teamId, ...SIGNING })
    ).resolves.toMatchObject({ team: { lifecycle: 'ACTIVE' } });

    const rows = await outboxOf(teamId);
    expect(rows.map((row: any) => row.deliveryState)).toEqual([
      'DELIVERED',
      'DELIVERED',
    ]);
    expect(await timesDelivered(teamId)).toEqual([1, 1]);
  });

  it('keeps the activation when the inbox refuses, and the next scheduled run delivers', async () => {
    const { teamId } = await provision();
    respond = async (id) => (id === teamId ? 503 : 200);

    await expect(
      activateWorkspaceAndPublish(prisma, { teamId, ...SIGNING })
    ).resolves.toMatchObject({ team: { lifecycle: 'ACTIVE' } });
    await expect(
      prisma.team.findUnique({ where: { id: teamId } })
    ).resolves.toMatchObject({ lifecycle: 'ACTIVE' });
    const [, active] = await outboxOf(teamId);
    expect(active).toMatchObject({ deliveryState: 'FAILED', attempts: 1 });
    expect(active.nextAttemptAt).not.toBeNull();

    respond = async () => 200;
    await sleep(Math.max(0, active.nextAttemptAt.getTime() - Date.now()) + 50);
    await scheduledRun(`scheduled:${teamId}`);

    const [, retried] = await outboxOf(teamId);
    expect(retried).toMatchObject({ deliveryState: 'DELIVERED', attempts: 2 });
  });

  it('does not hold the activation past its deadline when the inbox hangs', async () => {
    const { teamId } = await provision();
    let release: () => void = () => undefined;
    const hung = new Promise<void>((resolve) => {
      release = resolve;
    });
    respond = async (id) => {
      if (id === teamId) await hung;
      return 200;
    };

    try {
      const started = Date.now();
      await expect(
        activateWorkspaceAndPublish(prisma, { teamId, ...SIGNING })
      ).resolves.toMatchObject({ team: { lifecycle: 'ACTIVE' } });
      expect(Date.now() - started).toBeLessThan(3_000 + 1_000);

      // The aborted POST settled as retryable for the scheduled run.
      const [, active] = await outboxOf(teamId);
      expect(active).toMatchObject({ deliveryState: 'FAILED', attempts: 1 });
    } finally {
      release();
    }
  });

  it('delivers each Snapshot once when the immediate delivery races scheduled runs', async () => {
    const { teamId } = await provision();
    // The inbox holds the first delivery of the active Snapshot and runs
    // scheduled cycles while it is in flight; none may claim it again.
    let held = false;
    let runsWhileInFlight = 0;
    respond = async (id) => {
      if (id !== teamId || held) return 200;
      held = true;
      for (let run = 0; run < 3; run++) {
        await scheduledRun(`scheduled:${teamId}:in-flight:${run}`);
        runsWhileInFlight++;
      }
      return 200;
    };

    let activated = false;
    const activation = activateWorkspaceAndPublish(prisma, {
      teamId,
      ...SIGNING,
    }).finally(() => {
      activated = true;
    });
    // Scheduled runs also claim back to back while the activation commits,
    // so either side may win the claim.
    const scheduledRuns = (async () => {
      let runs = 0;
      while (!activated) await scheduledRun(`scheduled:${teamId}:${runs++}`);
    })();
    await Promise.all([activation, scheduledRuns]);
    await scheduledRun(`scheduled:${teamId}:after`);

    expect(runsWhileInFlight).toBe(3);
    const [, active] = await outboxOf(teamId);
    expect(active).toMatchObject({ deliveryState: 'DELIVERED', attempts: 1 });
    expect(await timesDelivered(teamId)).toEqual([1, 1]);
  });
});
