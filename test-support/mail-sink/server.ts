import http from 'node:http';
import crypto from 'node:crypto';
import fs from 'node:fs';
import { SMTPServer } from 'smtp-server';
import { simpleParser, ParsedMail } from 'mailparser';

const DEFAULT_SMTP_PORT = parseInt(process.env.SMTP_PORT || '2525', 10);
const DEFAULT_HTTP_PORT = parseInt(process.env.HTTP_PORT || '8025', 10);

function readControlToken(): string {
  const direct = process.env.MAIL_SINK_TOKEN?.trim();
  if (direct) return direct;

  const tokenFile =
    process.env.MAIL_SINK_TOKEN_FILE || '/run/secrets/mail_sink_token';
  try {
    return fs.readFileSync(tokenFile, 'utf8').trim();
  } catch {
    return '';
  }
}

const DEFAULT_TOKEN = readControlToken();
const DEFAULT_LONG_POLL_MS = parseInt(
  process.env.LONG_POLL_TIMEOUT_MS || '30000',
  10
);

export interface StoredMessage {
  to: string;
  invitationPath: string;
  receivedAt: number;
}

type Waiter = {
  recipient: string;
  resolve: (msg: StoredMessage) => void;
  timer: ReturnType<typeof setTimeout>;
};

function constantTimeEqual(a: string, b: string): boolean {
  if (!a || !b) return false;
  const bufA = Buffer.from(a, 'utf8');
  const bufB = Buffer.from(b, 'utf8');
  if (bufA.length !== bufB.length) {
    crypto.timingSafeEqual(bufA, bufA);
    return false;
  }
  return crypto.timingSafeEqual(bufA, bufB);
}

function extractInvitationPath(mail: ParsedMail): string | null {
  const body = mail.text || mail.html || '';
  const match = body.match(/\/invitations\/[A-Za-z0-9_\-]+/);
  return match ? match[0] : null;
}

function normalizeRecipient(addr: string): string {
  return addr.trim().toLowerCase();
}

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

function jsonResponse(
  res: http.ServerResponse,
  status: number,
  body: unknown
) {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

export interface MailSinkInstance {
  smtp: SMTPServer;
  http: http.Server;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  inbox: Map<string, StoredMessage[]>;
  clearInbox: () => void;
}

export function createServer(opts?: {
  smtpPort?: number;
  httpPort?: number;
  controlToken?: string;
  longPollTimeoutMs?: number;
}): MailSinkInstance {
  const smtpPort = opts?.smtpPort ?? DEFAULT_SMTP_PORT;
  const httpPort = opts?.httpPort ?? DEFAULT_HTTP_PORT;
  const controlToken = opts?.controlToken ?? DEFAULT_TOKEN;
  const longPollMs = opts?.longPollTimeoutMs ?? DEFAULT_LONG_POLL_MS;

  const inbox = new Map<string, StoredMessage[]>();
  const waiters: Waiter[] = [];

  function deliverMessage(msg: StoredMessage): void {
    const key = normalizeRecipient(msg.to);
    if (!inbox.has(key)) inbox.set(key, []);
    inbox.get(key)!.push(msg);

    for (let i = waiters.length - 1; i >= 0; i--) {
      if (normalizeRecipient(waiters[i].recipient) === key) {
        const waiter = waiters.splice(i, 1)[0];
        clearTimeout(waiter.timer);
        waiter.resolve(msg);
      }
    }
  }

  function waitForMessage(
    recipient: string,
    timeoutMs: number
  ): Promise<StoredMessage | null> {
    const key = normalizeRecipient(recipient);
    const existing = inbox.get(key);
    if (existing && existing.length > 0) {
      return Promise.resolve(existing[existing.length - 1]);
    }

    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        const idx = waiters.findIndex((w) => w.resolve === resolve);
        if (idx !== -1) waiters.splice(idx, 1);
        resolve(null);
      }, timeoutMs);

      waiters.push({ recipient, resolve, timer });
    });
  }

  const smtp = new SMTPServer({
    authOptional: true,
    disabledCommands: ['STARTTLS'],
    onData(stream, session, callback) {
      const chunks: Buffer[] = [];
      stream.on('data', (chunk) => chunks.push(chunk));
      stream.on('end', async () => {
        try {
          const raw = Buffer.concat(chunks);
          const mail = await simpleParser(raw);
          const invitationPath = extractInvitationPath(mail);
          if (!invitationPath) {
            callback();
            return;
          }
          const recipients = session.envelope.rcptTo.map((r) => r.address);
          for (const to of recipients) {
            deliverMessage({ to, invitationPath, receivedAt: Date.now() });
          }
          callback();
        } catch {
          callback(new Error('Parse error'));
        }
      });
    },
  });

  const httpSrv = http.createServer(async (req, res) => {
    const method = req.method || 'GET';
    const url = (req.url || '/').split('?')[0];

    if (method === 'GET' && url === '/health') {
      return jsonResponse(res, 200, { status: 'ok' });
    }

    if (method === 'POST' && url === '/api/messages/query') {
      const authHeader = req.headers['authorization'] || '';
      const token = authHeader.startsWith('Bearer ')
        ? authHeader.slice(7)
        : '';
      if (!constantTimeEqual(token, controlToken)) {
        return jsonResponse(res, 401, { error: 'unauthorized' });
      }

      let body: { recipient?: string; timeout_ms?: number };
      try {
        body = JSON.parse(await readBody(req));
      } catch {
        return jsonResponse(res, 400, { error: 'invalid json' });
      }

      if (!body.recipient) {
        return jsonResponse(res, 400, { error: 'recipient required' });
      }

      const timeoutMs = Math.min(
        body.timeout_ms ?? longPollMs,
        longPollMs
      );
      const msg = await waitForMessage(body.recipient, timeoutMs);
      if (!msg) {
        return jsonResponse(res, 408, { error: 'timeout' });
      }

      return jsonResponse(res, 200, {
        to: msg.to,
        invitationPath: msg.invitationPath,
      });
    }

    jsonResponse(res, 404, { error: 'not found' });
  });

  return {
    smtp,
    http: httpSrv,
    start: () =>
      new Promise<void>((resolve) => {
        smtp.listen(smtpPort, () => {
          httpSrv.listen(httpPort, () => resolve());
        });
      }),
    stop: () =>
      new Promise<void>((resolve, reject) => {
        smtp.close(() => {
          httpSrv.close((err) => (err ? reject(err) : resolve()));
        });
      }),
    inbox,
    clearInbox: () => inbox.clear(),
  };
}

if (require.main === module) {
  const server = createServer();
  server.start().then(() => {
    console.log(
      `mail-sink: SMTP on ${DEFAULT_SMTP_PORT}, HTTP on ${DEFAULT_HTTP_PORT}`
    );
  });
}
