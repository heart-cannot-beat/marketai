import net from 'node:net';

type SendEmail = typeof import('../../../lib/email/sendEmail').sendEmail;

interface RelayTranscript {
  authCommands: string[];
  recipients: string[];
  messages: string[];
}

/**
 * A minimal SMTP relay shaped like the golden-path mail sink
 * (test-support/mail-sink): it advertises AUTH, never requires it, and keeps
 * what it is sent.
 */
async function startRelay() {
  const transcript: RelayTranscript = {
    authCommands: [],
    recipients: [],
    messages: [],
  };

  const server = net.createServer((socket) => {
    let pending = '';
    let message: string[] | null = null;
    const reply = (...lines: string[]) =>
      socket.write(lines.map((line) => `${line}\r\n`).join(''));

    reply('220 relay.test ESMTP');
    socket.on('data', (chunk) => {
      pending += chunk.toString('utf8');
      let end = pending.indexOf('\r\n');
      while (end !== -1) {
        const line = pending.slice(0, end);
        pending = pending.slice(end + 2);
        end = pending.indexOf('\r\n');

        if (message) {
          if (line === '.') {
            transcript.messages.push(message.join('\n'));
            message = null;
            reply('250 OK: message queued');
          } else {
            message.push(line);
          }
          continue;
        }

        const verb = line.split(' ')[0].toUpperCase();
        if (verb === 'EHLO') {
          reply('250-relay.test', '250-AUTH PLAIN LOGIN', '250 8BITMIME');
        } else if (verb === 'AUTH') {
          transcript.authCommands.push(line);
          reply('235 Authentication successful');
        } else if (verb === 'RCPT') {
          transcript.recipients.push(
            line.replace(/^RCPT TO:<([^>]*)>.*$/i, '$1')
          );
          reply('250 Accepted');
        } else if (verb === 'DATA') {
          message = [];
          reply('354 End data with <CR><LF>.<CR><LF>');
        } else if (verb === 'QUIT') {
          reply('221 Bye');
          socket.end();
        } else {
          reply('250 OK');
        }
      }
    });
  });

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as net.AddressInfo;

  return {
    port,
    transcript,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

describe('Lib - sendEmail', () => {
  const smtpKeys = [
    'SMTP_HOST',
    'SMTP_PORT',
    'SMTP_USER',
    'SMTP_PASSWORD',
    'SMTP_FROM',
  ] as const;
  const original = Object.fromEntries(
    smtpKeys.map((key) => [key, process.env[key]])
  );
  let relay: Awaited<ReturnType<typeof startRelay>>;

  /** Re-evaluates lib/env.ts and the transport against the given SMTP settings. */
  const loadSendEmail = async (
    settings: Partial<Record<(typeof smtpKeys)[number], string>>
  ): Promise<SendEmail> => {
    for (const key of smtpKeys) {
      if (settings[key] === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = settings[key];
      }
    }

    let loaded: SendEmail | undefined;
    await jest.isolateModulesAsync(async () => {
      loaded = (await import('../../../lib/email/sendEmail')).sendEmail;
    });
    return loaded as SendEmail;
  };

  beforeEach(async () => {
    relay = await startRelay();
  });

  afterEach(async () => {
    await relay.close();
    for (const key of smtpKeys) {
      if (original[key] === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = original[key];
      }
    }
  });

  it('delivers through a relay that does not require AUTH when no SMTP credentials are configured', async () => {
    // The golden-path Compose topology sets only SMTP_HOST and SMTP_PORT.
    const sendEmail = await loadSendEmail({
      SMTP_HOST: '127.0.0.1',
      SMTP_PORT: String(relay.port),
    });

    await sendEmail({
      to: 'member@golden-path.test',
      subject: 'You have been invited',
      html: '<a href="http://web:4002/invitations/invitation-token">Join</a>',
    });

    expect(relay.transcript.authCommands).toEqual([]);
    expect(relay.transcript.recipients).toEqual(['member@golden-path.test']);
    expect(relay.transcript.messages).toHaveLength(1);
    expect(relay.transcript.messages[0]).toContain(
      '/invitations/invitation-token'
    );
  });

  it('authenticates when SMTP credentials are configured', async () => {
    const sendEmail = await loadSendEmail({
      SMTP_HOST: '127.0.0.1',
      SMTP_PORT: String(relay.port),
      SMTP_USER: 'mailer',
      SMTP_PASSWORD: 'ci-only-relay-password',
      SMTP_FROM: 'MarketAI <no-reply@marketai.test>',
    });

    await sendEmail({
      to: 'member@golden-path.test',
      subject: 'You have been invited',
      html: '<p>Invitation</p>',
    });

    const plain = Buffer.from('\u0000mailer\u0000ci-only-relay-password');
    expect(relay.transcript.authCommands).toEqual([
      `AUTH PLAIN ${plain.toString('base64')}`,
    ]);
    expect(relay.transcript.recipients).toEqual(['member@golden-path.test']);
  });
});
