import type { NextApiRequest, NextApiResponse } from 'next';
import { prisma } from '@/lib/prisma';
import { requireSession } from '@/lib/requireSession';
import { getTokenIssuer } from '@/lib/delegated-identity/instances';

const META_ADAPTER = process.env.META_ADAPTER || '';
const DOMAIN_API_URL = process.env.DOMAIN_API_URL || 'http://api:8787';

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse
): Promise<void> {
  if (META_ADAPTER !== 'fake_meta') {
    res.status(404).json({ error: 'not_found' });
    return;
  }

  if (req.method !== 'POST') {
    res.status(405).json({ error: 'method_not_allowed' });
    return;
  }

  const slug = req.query.slug;
  if (typeof slug !== 'string') {
    res.status(400).json({ error: 'invalid_slug' });
    return;
  }

  let session;
  try {
    session = await requireSession(req, res);
  } catch {
    res.status(401).json({ error: 'unauthorized' });
    return;
  }

  const workspace = await prisma.team.findUnique({
    where: { slug },
    select: { id: true },
  });
  if (!workspace) {
    res.status(404).json({ error: 'workspace_not_found' });
    return;
  }

  const membership = await prisma.teamMember.findFirst({
    where: {
      teamId: workspace.id,
      userId: session.user.id,
      role: 'OWNER',
    },
  });
  if (!membership) {
    res.status(403).json({ error: 'forbidden' });
    return;
  }

  const tokenIssuer = getTokenIssuer();
  const delegatedToken = await tokenIssuer.issueForRequest({
    req,
    res,
    workspaceSlug: slug,
    signal: AbortSignal.timeout(10_000),
  });

  const body = req.body;
  if (!body || typeof body !== 'object') {
    res.status(400).json({ error: 'invalid_body' });
    return;
  }

  const upstreamUrl = `${DOMAIN_API_URL}/api/v1/workspaces/${workspace.id}/test-support/meta-observation/advance`;

  try {
    const upstreamRes = await fetch(upstreamUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${delegatedToken}`,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(15_000),
    });

    if (!upstreamRes.ok) {
      const errorText = await upstreamRes.text();
      res
        .status(upstreamRes.status)
        .json({ error: 'upstream_error', detail: errorText });
      return;
    }

    const result = await upstreamRes.json();
    res.status(200).json(result);
  } catch {
    res.status(502).json({ error: 'upstream_unavailable' });
  }
}
