export interface BeginProvisioningInput {
  idempotencyKey: string;
  workspaceName: string;
  workspaceSlug?: string;
  ownerEmail: string;
  operatorIdentifier: string;
  betaPlanVersionId: string;
  namespace: string;
  signingKeyId: string;
  signingKey: Buffer;
}

export interface SecretInvitationCapability {
  readonly token: string;
  readonly invitationPath: string;
}

export interface BeginProvisioningResult {
  provisioningRef: string;
  workspaceRef: string;
  workspaceSlug: string;
  ownerInvitation: SecretInvitationCapability;
  replay: boolean;
}

export interface CompleteProvisioningInput {
  provisioningRef: string;
  signingKeyId: string;
  signingKey: Buffer;
}

export interface CompleteProvisioningResult {
  provisioningRef: string;
  workspaceRef: string;
  workspaceSlug: string;
  ownerMembershipRef: string;
  provisioningSnapshotVersion: number;
  activeSnapshotVersion: number;
}
