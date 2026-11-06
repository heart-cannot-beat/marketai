import type { NextApiRequest, NextApiResponse } from 'next';
import { requireSession } from '@/lib/requireSession';
import { findManySessions } from 'models/session';
import { withRateLimit } from '@/lib/rateLimit';

async function handler(req: NextApiRequest, res: NextApiResponse) {
  try {
    switch (req.method) {
      case 'GET':
        await handleGET(req, res);
        break;
      default:
        res.setHeader('Allow', 'GET');
        res.status(405).json({
          error: { message: `Method ${req.method} Not Allowed` },
        });
    }
  } catch (error: any) {
    const message = error.message || 'Something went wrong';
    const status = error.status || 500;

    res.status(status).json({ error: { message } });
  }
}

// Fetch all sessions for the current user
const handleGET = async (req: NextApiRequest, res: NextApiResponse) => {
  const session = await requireSession(req, res);

  const rows = await findManySessions({
    where: {
      userId: session.user.id,
      // Expired rows are dead weight, never a session someone can use.
      expires: { gt: new Date() },
    },
  });

  // `sessionToken` is never handed to the browser; the current session is the
  // one this request's token is bound to.
  const sessions = rows
    .map((row) => ({
      id: row.id,
      userId: row.userId,
      expires: row.expires,
      isCurrent: row.id === session.sessionId,
    }))
    .sort((a, b) => Number(new Date(b.expires)) - Number(new Date(a.expires)));

  res.json({ data: sessions });
};

export default withRateLimit('mutation', handler);
