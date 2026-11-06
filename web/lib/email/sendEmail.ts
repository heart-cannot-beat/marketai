import nodemailer from 'nodemailer';

import env from '../env';

// Credentials are optional. A relay such as the golden-path mail sink
// (test-support/mail-sink) or a local mail catcher advertises AUTH but accepts
// unauthenticated mail; an `auth` object without credentials makes nodemailer
// attempt AUTH anyway and fail every send with `Missing credentials for
// "PLAIN"`. Setting either value keeps authentication on, so a half-configured
// production relay still fails loudly.
const transporter = nodemailer.createTransport({
  host: env.smtp.host,
  port: env.smtp.port,
  secure: false,
  auth:
    env.smtp.user || env.smtp.password
      ? { user: env.smtp.user, pass: env.smtp.password }
      : undefined,
});

interface EmailData {
  to: string;
  subject: string;
  html: string;
  text?: string;
}

export const sendEmail = async (data: EmailData) => {
  if (!env.smtp.host) {
    return;
  }

  const emailDefaults = {
    from: env.smtp.from,
  };

  await transporter.sendMail({ ...emailDefaults, ...data });
};
