import type { NextApiRequest, NextApiResponse } from 'next';

import { sendMfaRecoveryChallengeEmail } from '@/lib/email/sendMfaRecoveryChallengeEmail';
import {
  beginMfaRecoveryEvidence,
  verifyMfaRecoveryEmailChallenge,
} from '@/lib/mfa-reset';
import { withRateLimit } from '@/lib/rateLimit';

async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    res.status(405).json({ error: { message: 'Method not allowed.' } });
    return;
  }

  try {
    if (req.body?.action === 'start') {
      if (
        typeof req.body?.caseId !== 'string' ||
        typeof req.body?.email !== 'string' ||
        typeof req.body?.password !== 'string'
      ) {
        res
          .status(400)
          .json({ error: { message: 'Recovery evidence is required.' } });
        return;
      }
      const evidence = await beginMfaRecoveryEvidence({
        caseId: req.body.caseId,
        email: req.body.email,
        password: req.body.password,
      });
      await sendMfaRecoveryChallengeEmail({
        to: evidence.verifiedEmail,
        caseId: req.body.caseId,
        challenge: evidence.challengeForDelivery,
      });
      res.status(201).json({
        data: {
          recoveryToken: evidence.recoveryToken,
          expiresAt: evidence.expiresAt.toISOString(),
        },
      });
      return;
    }

    if (req.body?.action === 'verify') {
      if (
        typeof req.body?.caseId !== 'string' ||
        typeof req.body?.recoveryToken !== 'string' ||
        typeof req.body?.challenge !== 'string'
      ) {
        res
          .status(400)
          .json({ error: { message: 'Recovery challenge is required.' } });
        return;
      }
      await verifyMfaRecoveryEmailChallenge({
        caseId: req.body.caseId,
        recoveryToken: req.body.recoveryToken,
        challenge: req.body.challenge,
      });
      res.status(200).json({ data: { evidenceVerified: true } });
      return;
    }

    res.status(400).json({ error: { message: 'Unknown recovery action.' } });
  } catch (error) {
    const candidate = error as { status?: number; message?: string };
    res.status(candidate.status ?? 500).json({
      error: { message: candidate.message ?? 'Something went wrong.' },
    });
  }
}

export default withRateLimit('auth', handler);
