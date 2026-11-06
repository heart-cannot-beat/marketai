jest.mock('@/lib/env', () => ({
  __esModule: true,
  default: { appUrl: 'http://localhost:4002' },
}));
jest.mock('@/lib/onboarding', () => ({
  provisionFirstOwnerWorkspace: jest.fn(),
  activateWorkspaceAndPublish: jest.fn(),
}));
jest.mock('@/lib/server-common', () => ({
  slugify: jest.fn((s: string) => s.toLowerCase().replace(/\s+/g, '-')),
}));
jest.mock('@/lib/errors', () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

import { beginProvisioning } from '@/lib/control-plane-provisioning/begin';
import { completeProvisioning } from '@/lib/control-plane-provisioning/complete';
import {
  provisionFirstOwnerWorkspace,
  activateWorkspaceAndPublish,
} from '@/lib/onboarding';

const BEGIN_INPUT = {
  idempotencyKey: 'test-key',
  workspaceName: 'Test Workspace',
  ownerEmail: 'owner@test.com',
  operatorIdentifier: 'op_1',
  betaPlanVersionId: 'pv_beta_1',
  namespace: 'default',
  signingKeyId: 'beta-v1',
  signingKey: Buffer.from('test-signing-key'),
};

const COMPLETE_INPUT = {
  provisioningRef: 'ws_test',
  signingKeyId: 'beta-v1',
  signingKey: Buffer.from('test-signing-key'),
};

describe('beginProvisioning', () => {
  beforeEach(() => jest.clearAllMocks());

  it('calls provisionFirstOwnerWorkspace and returns structured result', async () => {
    (provisionFirstOwnerWorkspace as jest.Mock).mockResolvedValue({
      team: { id: 'ws_test', slug: 'test-workspace' },
      invitation: { token: 'inv_tok_abc' },
      replay: false,
    });

    const result = await beginProvisioning({} as any, BEGIN_INPUT);

    expect(provisionFirstOwnerWorkspace).toHaveBeenCalledTimes(1);
    expect(result.provisioningRef).toBe('ws_test');
    expect(result.workspaceRef).toBe('ws_test');
    expect(result.workspaceSlug).toBe('test-workspace');
    expect(result.ownerInvitation.token).toBe('inv_tok_abc');
    expect(result.ownerInvitation.invitationPath).toBe(
      '/invitations/inv_tok_abc'
    );
    expect(result.replay).toBe(false);
  });

  it('uses provided slug when given', async () => {
    (provisionFirstOwnerWorkspace as jest.Mock).mockResolvedValue({
      team: { id: 'ws_test', slug: 'custom-slug' },
      invitation: { token: 'inv_tok' },
      replay: false,
    });

    await beginProvisioning({} as any, {
      ...BEGIN_INPUT,
      workspaceSlug: 'Custom Slug',
    });

    const call = (provisionFirstOwnerWorkspace as jest.Mock).mock.calls[0];
    expect(call[1].slug).toBe('custom-slug');
  });

  it('indicates replay when workspace already exists', async () => {
    (provisionFirstOwnerWorkspace as jest.Mock).mockResolvedValue({
      team: { id: 'ws_existing', slug: 'existing' },
      invitation: { token: 'inv_tok_replay' },
      replay: true,
    });

    const result = await beginProvisioning({} as any, BEGIN_INPUT);
    expect(result.replay).toBe(true);
  });
});

describe('completeProvisioning', () => {
  function makeMockPrisma(overrides?: {
    team?: any;
    invitation?: any;
    owner?: any;
    projection?: any;
  }) {
    const has = (key: string) =>
      overrides !== undefined && key in (overrides as Record<string, unknown>);
    return {
      team: {
        findUnique: jest
          .fn()
          .mockResolvedValue(
            has('team')
              ? overrides!.team
              : { id: 'ws_test', lifecycle: 'PROVISIONING', slug: 'test-ws' }
          ),
      },
      invitation: {
        findFirst: jest.fn().mockResolvedValue(
          has('invitation')
            ? overrides!.invitation
            : {
                id: 'inv_1',
                consumedAt: new Date(),
                consumedByUserId: 'user_1',
              }
        ),
      },
      teamMember: {
        findFirst: jest.fn().mockResolvedValue(
          has('owner')
            ? overrides!.owner
            : {
                id: 'tm_1',
                role: 'OWNER',
                user: {
                  id: 'user_1',
                  mfaCredential: { verifiedAt: new Date() },
                },
              }
        ),
      },
      commercialAccessProjection: {
        findUnique: jest
          .fn()
          .mockResolvedValue(
            has('projection')
              ? overrides!.projection
              : { entitlementVersion: 1 }
          ),
      },
    } as any;
  }

  beforeEach(() => {
    jest.clearAllMocks();
    (activateWorkspaceAndPublish as jest.Mock).mockResolvedValue({
      team: { id: 'ws_test', lifecycle: 'ACTIVE' },
      activeSnapshotVersion: 2,
    });
  });

  it('verifies preconditions and activates the workspace', async () => {
    const prisma = makeMockPrisma();
    const result = await completeProvisioning(prisma, COMPLETE_INPUT);

    expect(activateWorkspaceAndPublish).toHaveBeenCalledWith(prisma, {
      teamId: 'ws_test',
      signingKeyId: 'beta-v1',
      signingKey: COMPLETE_INPUT.signingKey,
    });
    expect(result.workspaceRef).toBe('ws_test');
    expect(result.ownerMembershipRef).toBe('tm_1');
    expect(result.provisioningSnapshotVersion).toBe(1);
    expect(result.activeSnapshotVersion).toBe(2);
  });

  it('rejects when workspace not found', async () => {
    const prisma = makeMockPrisma({ team: null });
    await expect(
      completeProvisioning(prisma, COMPLETE_INPUT)
    ).rejects.toMatchObject({ status: 404 });
  });

  it('rejects when workspace is not PROVISIONING', async () => {
    const prisma = makeMockPrisma({
      team: { id: 'ws_test', lifecycle: 'ACTIVE', slug: 'test-ws' },
    });
    await expect(
      completeProvisioning(prisma, COMPLETE_INPUT)
    ).rejects.toMatchObject({ status: 409 });
  });

  it('rejects when invitation not consumed', async () => {
    const prisma = makeMockPrisma({ invitation: null });
    await expect(
      completeProvisioning(prisma, COMPLETE_INPUT)
    ).rejects.toMatchObject({ status: 409 });
  });

  it('rejects when owner membership missing', async () => {
    const prisma = makeMockPrisma({ owner: null });
    await expect(
      completeProvisioning(prisma, COMPLETE_INPUT)
    ).rejects.toMatchObject({ status: 409 });
  });

  it('rejects when MFA not verified', async () => {
    const prisma = makeMockPrisma({
      owner: {
        id: 'tm_1',
        role: 'OWNER',
        user: { id: 'user_1', mfaCredential: null },
      },
    });
    await expect(
      completeProvisioning(prisma, COMPLETE_INPUT)
    ).rejects.toMatchObject({ status: 409 });
  });
});
