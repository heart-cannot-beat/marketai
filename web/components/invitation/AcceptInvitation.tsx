import toast from 'react-hot-toast';
import { Button } from 'react-daisyui';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';

import type { ApiResponse } from 'types';
import { apiErrorMessage } from '@/lib/i18n/domain-errors';
import { defaultHeaders } from '@/lib/common';
import { Invitation, Team } from '@prisma/client';

interface AcceptInvitationProps {
  invitation: Invitation & { team: Team };
}

const AcceptInvitation = ({ invitation }: AcceptInvitationProps) => {
  const router = useRouter();
  const { t } = useTranslation('common');

  const acceptInvitation = async () => {
    const response = await fetch(
      `/api/teams/${invitation.team.slug}/invitations`,
      {
        method: 'PUT',
        headers: defaultHeaders,
        body: JSON.stringify({ inviteToken: invitation.token }),
      }
    );

    if (!response.ok) {
      const result = (await response.json()) as ApiResponse;
      toast.error(apiErrorMessage(t, result.error));
      return;
    }

    router.push(
      invitation.kind === 'FIRST_OWNER'
        ? `/settings/security?onboarding=${encodeURIComponent(invitation.team.slug)}`
        : '/dashboard'
    );
  };

  return (
    <>
      <h3 className="text-center">{t('accept-invite')}</h3>
      <Button onClick={acceptInvitation} fullWidth color="primary" size="md">
        {t('accept-invitation')}
      </Button>
    </>
  );
};

export default AcceptInvitation;
