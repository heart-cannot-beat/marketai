import { defaultHeaders } from '@/lib/common';
import { availableRoles } from '@/lib/permissions';
import { Team, TeamMember } from '@prisma/client';
import { useTranslation } from 'next-i18next';
import toast from 'react-hot-toast';
import type { ApiResponse } from 'types';
import { apiErrorMessage } from '@/lib/i18n/domain-errors';

interface UpdateMemberRoleProps {
  team: Team;
  member: TeamMember;
}

const UpdateMemberRole = ({ team, member }: UpdateMemberRoleProps) => {
  const { t } = useTranslation('common');

  const updateRole = async (
    member: TeamMember,
    role: string,
    select: HTMLSelectElement
  ) => {
    const confirmation =
      role === 'OWNER'
        ? window.prompt(
            'Type PROMOTE TO OWNER to confirm this high-risk action.'
          )
        : member.role === 'OWNER'
          ? window.prompt('Type REMOVE OWNER to confirm this high-risk action.')
          : undefined;
    if (role === 'OWNER' && confirmation !== 'PROMOTE TO OWNER') return;
    if (
      member.role === 'OWNER' &&
      role !== 'OWNER' &&
      confirmation !== 'REMOVE OWNER'
    )
      return;
    const response = await fetch(`/api/teams/${team.slug}/members`, {
      method: 'PATCH',
      headers: defaultHeaders,
      body: JSON.stringify({
        memberId: member.userId,
        role,
        confirmation,
      }),
    });

    const json = (await response.json()) as ApiResponse;

    if (!response.ok) {
      // A refused change (a promotion over the seat limit, say) left the
      // role as it was; the dropdown goes back to it.
      select.value = member.role;
      toast.error(apiErrorMessage(t, json.error));
      return;
    }

    toast.success(t('member-role-updated'));
  };

  return (
    <select
      className="select select-bordered select-sm rounded"
      defaultValue={member.role}
      onChange={(e) => updateRole(member, e.target.value, e.target)}
    >
      {availableRoles.map((role) => (
        <option value={role.id} key={role.id}>
          {role.id}
        </option>
      ))}
    </select>
  );
};

export default UpdateMemberRole;
