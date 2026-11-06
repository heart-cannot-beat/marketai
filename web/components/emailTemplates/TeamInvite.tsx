import {
  Button,
  Container,
  Head,
  Html,
  Preview,
  Text,
} from '@react-email/components';
import EmailLayout from './EmailLayout';
import { Team } from '@prisma/client';
import app from '@/lib/app';

interface TeamInviteEmailProps {
  team: Team;
  invitationLink: string;
  subject: string;
}

const TeamInviteEmail = ({
  team,
  invitationLink,
  subject,
}: TeamInviteEmailProps) => {
  return (
    <Html>
      <Head />
      <Preview>{subject}</Preview>
      <EmailLayout>
        <Text>
          You have been invited to join the {team.name} Workspace on {app.name}.
        </Text>
        <Text>
          This invitation is for your email address only and can be used once.
          Click the button below to accept it.
        </Text>
        <Container className="text-center">
          <Button
            href={invitationLink}
            className="bg-brand text-white font-medium py-2 px-4 rounded"
          >
            Join the Workspace
          </Button>
        </Container>
        <Text>
          You have 7 days to accept this invitation before it expires.
        </Text>
      </EmailLayout>
    </Html>
  );
};

export default TeamInviteEmail;
