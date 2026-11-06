import useSWR, { mutate } from 'swr';

import fetcher from '@/lib/fetcher';
import type { ApiResponse } from 'types';
import { TeamInvitation } from 'models/invitation';

const useInvitations = ({ slug }: { slug: string }) => {
  const url = `/api/teams/${slug}/invitations`;

  const { data, error, isLoading } = useSWR<ApiResponse<TeamInvitation[]>>(
    url,
    fetcher
  );

  const mutateInvitation = async () => {
    mutate(url);
  };

  return {
    isLoading,
    isError: error,
    invitations: data?.data,
    mutateInvitation,
  };
};

export default useInvitations;
