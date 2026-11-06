import { render } from '@react-email/components';
import { sendEmail } from './sendEmail';
import { WelcomeEmail } from '@/components/emailTemplates';
import app from '../app';

export const sendWelcomeEmail = async (
  name: string,
  email: string,
  team: string
) => {
  const subject = `Welcome to ${app.name}`;
  const html = await render(WelcomeEmail({ name, team, subject }));

  await sendEmail({
    to: email,
    subject,
    html,
  });
};
