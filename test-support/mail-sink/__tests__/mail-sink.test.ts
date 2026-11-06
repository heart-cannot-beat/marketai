import http from 'node:http';
import nodemailer from 'nodemailer';
import { createServer, MailSinkInstance } from '../server';

const TEST_TOKEN = 'test-secret-token-12345';

let server: MailSinkInstance;
let smtpPort: number;
let httpPort: number;

function httpRequest(
  method: string,
  path: string,
  body?: unknown,
  headers?: Record<string, string>
): Promise<{ status: number; body: unknown }> {
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        hostname: '127.0.0.1',
        port: httpPort,
        path,
        method,
        headers: {
          'Content-Type': 'application/json',
          ...headers,
        },
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const raw = Buffer.concat(chunks).toString('utf8');
          resolve({
            status: res.statusCode || 0,
            body: raw ? JSON.parse(raw) : null,
          });
        });
      }
    );
    req.on('error', reject);
    if (body !== undefined) req.write(JSON.stringify(body));
    req.end();
  });
}

async function sendTestEmail(to: string, bodyText: string): Promise<void> {
  const transport = nodemailer.createTransport({
    host: '127.0.0.1',
    port: smtpPort,
    secure: false,
    tls: { rejectUnauthorized: false },
  });
  await transport.sendMail({
    from: 'test@example.com',
    to,
    subject: 'Invitation',
    text: bodyText,
  });
  transport.close();
}

beforeAll(async () => {
  smtpPort = 12525 + Math.floor(Math.random() * 1000);
  httpPort = 18025 + Math.floor(Math.random() * 1000);
  server = createServer({
    smtpPort,
    httpPort,
    controlToken: TEST_TOKEN,
    longPollTimeoutMs: 2000,
  });
  await server.start();
});

afterAll(async () => {
  await server.stop();
});

afterEach(() => {
  server.clearInbox();
});

describe('mail-sink', () => {
  test('returns 401 on bad token', async () => {
    const res = await httpRequest(
      'POST',
      '/api/messages/query',
      { recipient: 'user@example.com' },
      { Authorization: 'Bearer wrong-token' }
    );
    expect(res.status).toBe(401);
    expect(res.body).toEqual({ error: 'unauthorized' });
  });

  test('returns 401 with no token', async () => {
    const res = await httpRequest('POST', '/api/messages/query', {
      recipient: 'user@example.com',
    });
    expect(res.status).toBe(401);
  });

  test('long-poll returns invitation path after SMTP delivery', async () => {
    const recipient = 'owner@workspace.test';
    const invitePath = '/invitations/abc123_XYZ-test';

    const queryPromise = httpRequest(
      'POST',
      '/api/messages/query',
      { recipient, timeout_ms: 2000 },
      { Authorization: `Bearer ${TEST_TOKEN}` }
    );

    await new Promise((r) => setTimeout(r, 100));
    await sendTestEmail(
      recipient,
      `Welcome! Accept your invitation: https://app.example.com${invitePath}`
    );

    const res = await queryPromise;
    expect(res.status).toBe(200);
    expect(res.body).toEqual({
      to: recipient,
      invitationPath: invitePath,
    });
  });

  test('recipient data stays out of request URLs', async () => {
    const res = await httpRequest(
      'POST',
      '/api/messages/query',
      { recipient: 'sensitive@example.com', timeout_ms: 100 },
      { Authorization: `Bearer ${TEST_TOKEN}` }
    );
    expect(res.status).toBe(408);
  });

  test('health endpoint returns ok', async () => {
    const res = await httpRequest('GET', '/health');
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ status: 'ok' });
  });

  test('returns already-delivered message without waiting', async () => {
    const recipient = 'already@example.com';
    await sendTestEmail(
      recipient,
      'Your link: https://app.example.com/invitations/predelivered'
    );
    await new Promise((r) => setTimeout(r, 200));

    const res = await httpRequest(
      'POST',
      '/api/messages/query',
      { recipient },
      { Authorization: `Bearer ${TEST_TOKEN}` }
    );
    expect(res.status).toBe(200);
    expect((res.body as Record<string, string>).invitationPath).toBe(
      '/invitations/predelivered'
    );
  });

  test('ignores email without invitation path', async () => {
    const recipient = 'nopath@example.com';
    await sendTestEmail(recipient, 'Hello, no links here.');
    await new Promise((r) => setTimeout(r, 200));

    const res = await httpRequest(
      'POST',
      '/api/messages/query',
      { recipient, timeout_ms: 200 },
      { Authorization: `Bearer ${TEST_TOKEN}` }
    );
    expect(res.status).toBe(408);
  });
});
