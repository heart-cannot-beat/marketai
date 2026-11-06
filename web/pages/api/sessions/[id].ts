import type { NextApiRequest, NextApiResponse } from 'next';
import { requireSession } from '@/lib/requireSession';
import { deleteSession, findFirstSessionOrThrown } from 'models/session';
import { validateWithSchema, deleteSessionSchema } from '@/lib/zod';
import { withRateLimit } from '@/lib/rateLimit';

async function handler(req: NextApiRequest, res: NextApiResponse) {
  try {
    switch (req.method) {
      case 'DELETE':
        await handleDELETE(req, res);
        break;
      default:
        res.setHeader('Allow', 'DELETE');
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

// Delete a session for the current user
const handleDELETE = async (req: NextApiRequest, res: NextApiResponse) => {
  const { id } = validateWithSchema(deleteSessionSchema, req.query);

  const session = await requireSession(req, res);

  await findFirstSessionOrThrown({
    where: {
      id,
      userId: session.user.id,
    },
  });

  await deleteSession({
    where: {
      id,
    },
  });

  res.status(204).end();
};

export default withRateLimit('mutation', handler);
