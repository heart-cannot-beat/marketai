import type { NextApiRequest, NextApiResponse } from 'next';

import { requireSession } from '@/lib/requireSession';
import { prisma } from '@/lib/prisma';

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse
) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    res.status(405).json({ error: { message: 'Method not allowed.' } });
    return;
  }
  try {
    const session = await requireSession(req, res);
    const slug = typeof req.query.slug === 'string' ? req.query.slug : '';
    const team = await prisma.team.findUnique({
      where: { slug },
      include: { onboardingProgress: true },
    });
    if (!team) {
      res.status(404).json({ error: { message: 'Workspace not found.' } });
      return;
    }
    const membership = await prisma.teamMember.findUnique({
      where: { teamId_userId: { teamId: team.id, userId: session.user.id } },
    });
    if (!membership) {
      res
        .status(403)
        .json({ error: { message: 'Workspace membership required.' } });
      return;
    }
    const progress = team.onboardingProgress;
    const ownerItems = [
      {
        key: 'workspace',
        labelKey: 'onboarding-workspace-prepared',
        completedAt: progress?.workspacePreparedAt,
      },
      {
        key: 'owner',
        labelKey: 'onboarding-owner-accepted',
        completedAt: progress?.ownerAcceptedAt,
      },
    ];
    const valueItems = [
      {
        key: 'preview',
        labelKey: 'onboarding-valid-preview',
        completedAt: progress?.validPreviewAt,
      },
      {
        key: 'mock-recipe',
        labelKey: 'onboarding-mock-recipe-snapshot',
        completedAt: progress?.mockRecipeSnapshotAt,
      },
      {
        key: 'asset-version',
        labelKey: 'onboarding-qualified-asset-version',
        completedAt: progress?.assetQualifiedAt,
      },
      {
        key: 'human-review',
        labelKey: 'onboarding-human-review',
        completedAt: progress?.humanReviewAt,
      },
    ];
    const items = [
      ...(membership.role === 'OWNER' ? ownerItems : []),
      ...valueItems,
    ].map((item) => ({
      key: item.key,
      labelKey: item.labelKey,
      complete: Boolean(item.completedAt),
      completedAt: item.completedAt?.toISOString() ?? null,
    }));
    res.status(200).json({
      data: {
        lifecycle: team.lifecycle,
        commandsEnabled: team.lifecycle === 'ACTIVE',
        version: progress?.version ?? 0,
        role: membership.role,
        items,
      },
    });
  } catch (error) {
    const candidate = error as { status?: number; message?: string };
    res.status(candidate.status ?? 500).json({
      error: { message: candidate.message ?? 'Something went wrong.' },
    });
  }
}
