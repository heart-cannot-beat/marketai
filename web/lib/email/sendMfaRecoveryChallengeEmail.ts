import app from '../app';
import { sendEmail } from './sendEmail';

/** Deliver the challenge without exposing the address or code to operators. */
export const sendMfaRecoveryChallengeEmail = async (params: {
  to: string;
  caseId: string;
  challenge: string;
}) => {
  const subject = `${app.name} MFA recovery verification`;
  const safeCaseId = params.caseId.replace(/[<>&"']/g, '');
  const safeChallenge = params.challenge.replace(/[<>&"']/g, '');
  await sendEmail({
    to: params.to,
    subject,
    html: `<p>Enter this one-time code to continue MFA recovery for case <strong>${safeCaseId}</strong>:</p><p><strong>${safeChallenge}</strong></p><p>If you did not request this, contact support immediately.</p>`,
  });
};
