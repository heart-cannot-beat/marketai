import crypto from 'node:crypto';
import type { NextApiRequest, NextApiResponse } from 'next';

import env from '@/lib/env';
import { ApiError } from '@/lib/errors';
import {
  recordOnboardingFact,
  type OnboardingDomainFactType,
} from '@/lib/onboarding';
import { prisma } from '@/lib/prisma';

const ALLOWED = new Set<OnboardingDomainFactType>([
  'resolution_preview.validated',
  'generation_recipe_snapshot.mock_created',
  'asset_version.qualified',
  'quality_review.human_recorded',
]);

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse
) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    res.status(405).json({ code: 'method_not_allowed' });
    return;
  }
  const expected = env.domainToControlPlaneSyncToken;
  const supplied = req.headers.authorization?.replace(/^Bearer\s+/i, '') ?? '';
  if (
    !expected ||
    supplied.length !== expected.length ||
    !crypto.timingSafeEqual(Buffer.from(supplied), Buffer.from(expected))
  ) {
    res.status(403).json({ code: 'service_auth_required' });
    return;
  }
  const { workspaceId, sourceEventId, factType, sourceResourceId, observedAt } =
    req.body ?? {};
  if (
    typeof workspaceId !== 'string' ||
    typeof sourceEventId !== 'string' ||
    typeof sourceResourceId !== 'string' ||
    !ALLOWED.has(factType as OnboardingDomainFactType) ||
    typeof observedAt !== 'string' ||
    !Number.isFinite(Date.parse(observedAt))
  ) {
    res.status(400).json({ code: 'invalid_onboarding_fact' });
    return;
  }
  try {
    const progress = await prisma.$transaction(async (tx) => {
      const result = await recordOnboardingFact(tx, {
        teamId: workspaceId,
        sourceEventId,
        factType: factType as OnboardingDomainFactType,
        sourceResourceId,
        observedAt: new Date(observedAt),
      });
      if (result.receiptCreated) {
        await tx.controlPlaneAuditEvent.create({
          data: {
            scopeKind: 'WORKSPACE',
            teamId: workspaceId,
            actorKind: 'SERVICE',
            actorId: 'domain-projection',
            executor: 'control-plane',
            eventType: 'onboarding.domain_fact_observed',
            resourceType: 'OnboardingProgress',
            resourceId: result.progress.id,
            details: { sourceEventId, factType, sourceResourceId, observedAt },
          },
        });
      }
      return result.progress;
    });
    res.status(200).json({ data: { version: progress.version } });
  } catch (error) {
    if (error instanceof ApiError) {
      res.status(error.status).json({ code: 'onboarding_fact_conflict' });
      return;
    }
    const candidate = error as { code?: string };
    if (candidate.code === 'P2003' || candidate.code === 'P2025') {
      res.status(404).json({ code: 'workspace_not_found' });
      return;
    }
    res.status(500).json({ code: 'onboarding_projection_failed' });
  }
}
