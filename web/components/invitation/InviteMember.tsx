import React from 'react';
import { useTranslation } from 'next-i18next';

import Modal from '../shared/Modal';
import type { Team } from '@prisma/client';
import InviteViaEmail from './InviteViaEmail';

interface InviteMemberProps {
  team: Team;
  visible: boolean;
  setVisible: (visible: boolean) => void;
}

const InviteMember = ({ visible, setVisible, team }: InviteMemberProps) => {
  const { t } = useTranslation('common');

  return (
    <Modal open={visible} close={() => setVisible(!visible)}>
      <Modal.Header>{t('invite-new-member')}</Modal.Header>
      <Modal.Body>
        <div className="grid grid-cols-1 py-2">
          <InviteViaEmail setVisible={setVisible} team={team} />
        </div>
      </Modal.Body>
    </Modal>
  );
};

export default InviteMember;
