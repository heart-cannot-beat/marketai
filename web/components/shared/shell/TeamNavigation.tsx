import {
  ArrowUturnLeftIcon,
  Cog6ToothIcon,
  UserPlusIcon,
} from '@heroicons/react/24/outline';
import { useTranslation } from 'next-i18next';
import useCanAccess from 'hooks/useCanAccess';
import NavigationItems from './NavigationItems';
import { NavigationProps, MenuItem } from './NavigationItems';

interface NavigationItemsProps extends NavigationProps {
  slug: string;
}

/**
 * Workspace navigation.
 *
 * Upstream linked `/teams/{slug}/products`, the Stripe product catalogue. That
 * page is removed with live billing, so the link is gone too: a disabled
 * capability must not survive as a dead navigation item.
 *
 * These pages live in the account shell, outside the Workspace layout, so the
 * first entry leads back to this Workspace's own pages.
 *
 * Member administration is shown only to roles that hold the permission. The
 * server is still the authority — `throwIfNotAllowed` runs on every member
 * route — but an administration entry a Member cannot use does not belong in
 * their navigation either.
 */
const TeamNavigation = ({ slug, activePathname }: NavigationItemsProps) => {
  const { t } = useTranslation('common');
  const { canAccess } = useCanAccess();

  const menus: MenuItem[] = [
    {
      name: t('back-to-workspace'),
      href: `/w/${slug}/generate`,
      icon: ArrowUturnLeftIcon,
      active: false,
      testId: 'back-to-workspace-link',
    },
    {
      name: t('settings'),
      href: `/teams/${slug}/settings`,
      icon: Cog6ToothIcon,
      active: activePathname === `/teams/${slug}/settings`,
    },
  ];

  if (canAccess('team_member', ['create', 'update', 'delete'])) {
    menus.push({
      name: t('members'),
      href: `/teams/${slug}/members`,
      icon: UserPlusIcon,
      active: activePathname === `/teams/${slug}/members`,
    });
  }

  return <NavigationItems menus={menus} />;
};

export default TeamNavigation;
