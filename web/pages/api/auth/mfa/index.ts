import type { NextApiRequest, NextApiResponse } from 'next';

import { requireSession } from '@/lib/requireSession';
import {
  beginMfaEnrollment,
  confirmMfaEnrollment,
  mfaStatus,
  verifyMfaChallenge,
} from '@/lib/mfa';
import { prisma } from '@/lib/prisma';
import { withRateLimit } from '@/lib/rateLimit';

async function handler(req: NextApiRequest, res: NextApiResponse) {
  try {
    const session = await requireSession(req, res);

    if (req.method === 'GET') {
      res.status(200).json({ data: await mfaStatus(session.user.id) });
      return;
    }

    if (req.method !== 'POST') {
      res.setHeader('Allow', 'GET, POST');
      res.status(405).json({ error: { message: 'Method not allowed.' } });
      return;
    }

    const action = req.body?.action;
    if (action === 'begin') {
      const enrollment = await beginMfaEnrollment(
        session.user.id,
        session.user.email
      );
      res.status(201).json({ data: enrollment });
      return;
    }

    if (action === 'confirm') {
      if (typeof req.body?.code !== 'string') {
        res
          .status(400)
          .json({ error: { message: 'Authenticator code is required.' } });
        return;
      }
      const result = await confirmMfaEnrollment({
        userId: session.user.id,
        sessionId: session.sessionId,
        code: req.body.code,
      });
      res.status(200).json({ data: result });
      return;
    }

    if (action === 'challenge') {
      if (
        typeof req.body?.code !== 'string' ||
        !(await verifyMfaChallenge(session.user.id, req.body.code))
      ) {
        res.status(422).json({ error: { message: 'MFA challenge failed.' } });
        return;
      }
      const now = new Date();
      const updated = await prisma.session.updateMany({
        where: { id: session.sessionId, userId: session.user.id },
        data: { mfaAuthenticatedAt: now },
      });
      if (updated.count !== 1) {
        res
          .status(401)
          .json({ error: { message: 'Session is no longer active.' } });
        return;
      }
      res.status(200).json({ data: { authenticatedAt: now.toISOString() } });
      return;
    }

    res.status(400).json({ error: { message: 'Unknown MFA action.' } });
  } catch (error) {
    const candidate = error as { status?: number; message?: string };
    res.status(candidate.status ?? 500).json({
      error: { message: candidate.message ?? 'Something went wrong.' },
    });
  }
}

export default withRateLimit('auth', handler);
