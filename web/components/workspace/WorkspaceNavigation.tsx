import { useEffect, useState } from 'react';
import { useTranslation } from 'next-i18next';
import {
  CubeIcon,
  ClipboardDocumentCheckIcon,
  SparklesIcon,
  PhotoIcon,
  BeakerIcon,
  GlobeAltIcon,
  ArrowTrendingUpIcon,
  RectangleStackIcon,
  Cog6ToothIcon,
  UserPlusIcon,
  WrenchScrewdriverIcon,
  ChevronDownIcon,
} from '@heroicons/react/24/outline';
import classNames from 'classnames';
import useCanAccess from 'hooks/useCanAccess';
import { NavigationItem } from '@/components/shared/shell/NavigationItems';
import type { MenuItem } from '@/components/shared/shell/NavigationItems';

interface WorkspaceNavigationProps {
  slug: string;
  activePathname: string | null;
}

/** Per-viewer (per browser) memory of the advanced workbench group. */
export const ADVANCED_NAV_STORAGE_KEY = 'workspace-nav:advanced-expanded';

function readExpanded(): boolean {
  try {
    return window.localStorage.getItem(ADVANCED_NAV_STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

function writeExpanded(expanded: boolean): void {
  try {
    if (expanded) window.localStorage.setItem(ADVANCED_NAV_STORAGE_KEY, '1');
    else window.localStorage.removeItem(ADVANCED_NAV_STORAGE_KEY);
  } catch {
    // A preference only.
  }
}

/**
 * Workspace navigation (ADR 0011, Issue #165). The front flow comes first
 * -- Products / Marketing tests / Assets -- and the v9 areas (New generation,
 * Creative library, Delivery experiments, Localization, Scale) sit in a
 * collapsible "Advanced workbench" group: still reachable by everyone and by
 * URL, folded by default. The group opens by itself while one of its areas
 * is active, and remembers a viewer's explicit choice.
 */
export function WorkspaceNavigation({
  slug,
  activePathname,
}: WorkspaceNavigationProps) {
  const { t } = useTranslation('common');
  const { canAccess } = useCanAccess();
  const isActive = (area: string) => {
    if (!activePathname) return false;
    const pathOnly = activePathname.split('?')[0].split('#')[0];
    return (
      pathOnly === `/w/${slug}/${area}` ||
      pathOnly.startsWith(`/w/${slug}/${area}/`)
    );
  };

  const front: MenuItem[] = [
    {
      name: t('workspace-nav-products'),
      href: `/w/${slug}/products`,
      icon: CubeIcon,
      active: isActive('products'),
    },
    {
      name: t('workspace-nav-tests'),
      href: `/w/${slug}/experiments`,
      icon: ClipboardDocumentCheckIcon,
      active: isActive('experiments'),
    },
    {
      name: t('workspace-nav-assets'),
      href: `/w/${slug}/assets`,
      icon: PhotoIcon,
      active: isActive('assets'),
    },
  ];

  const advanced: MenuItem[] = [
    {
      name: t('workspace-nav-generate'),
      href: `/w/${slug}/generate`,
      icon: SparklesIcon,
      active: isActive('generate'),
    },
    {
      name: t('workspace-nav-library'),
      href: `/w/${slug}/library`,
      icon: RectangleStackIcon,
      active: isActive('library'),
    },
    {
      name: t('workspace-nav-experiments'),
      href: `/w/${slug}/legacy-experiments`,
      icon: BeakerIcon,
      active: isActive('legacy-experiments'),
    },
    {
      name: t('workspace-nav-localization'),
      href: `/w/${slug}/localization`,
      icon: GlobeAltIcon,
      active: isActive('localization'),
    },
    {
      name: t('workspace-nav-scale'),
      href: `/w/${slug}/scale`,
      icon: ArrowTrendingUpIcon,
      active: isActive('scale'),
    },
  ];
  const advancedActive = advanced.some((item) => item.active);

  // Server render and first client render agree (active area only); the
  // stored preference applies after mount.
  const [expanded, setExpanded] = useState(advancedActive);
  useEffect(() => {
    if (readExpanded()) setExpanded(true);
  }, []);
  // Landing on an advanced area opens the group; leaving it does not close
  // it again, so moving between areas never hides the link just used.
  useEffect(() => {
    if (advancedActive) setExpanded(true);
  }, [advancedActive]);

  // Settings is every member's: a Member reads the spending limits there
  // (#126); everything else on it, and Members, stays the Owner's.
  const admin: MenuItem[] = [
    {
      name: t('workspace-nav-settings'),
      href: `/w/${slug}/settings`,
      icon: Cog6ToothIcon,
      active: isActive('settings'),
      testId: 'workspace-settings-link',
    },
  ];
  if (canAccess('team_member', ['create', 'update', 'delete'])) {
    admin.push({
      name: t('workspace-nav-members'),
      href: `/w/${slug}/members`,
      icon: UserPlusIcon,
      active: isActive('members'),
    });
  }

  const groupId = 'workspace-nav-advanced';

  return (
    <ul role="list" className="flex flex-1 flex-col gap-1">
      {front.map((menu) => (
        <li key={menu.href}>
          <NavigationItem menu={menu} className="" />
        </li>
      ))}
      <li>
        <button
          type="button"
          className={classNames(
            'group flex w-full items-center gap-2 rounded p-2 px-2 text-left text-sm text-gray-900 hover:bg-gray-100 dark:text-gray-100 dark:hover:bg-gray-800',
            advancedActive && !expanded && 'font-semibold'
          )}
          aria-expanded={expanded}
          aria-controls={groupId}
          data-testid="workspace-nav-advanced-toggle"
          onClick={() => {
            const next = !expanded;
            setExpanded(next);
            writeExpanded(next);
          }}
        >
          <WrenchScrewdriverIcon
            className="h-5 w-5 shrink-0"
            aria-hidden="true"
          />
          <span className="flex-1">{t('workspace-nav-advanced')}</span>
          <ChevronDownIcon
            className={classNames(
              'h-4 w-4 shrink-0 transition-transform',
              expanded && 'rotate-180'
            )}
            aria-hidden="true"
          />
        </button>
        {expanded && (
          <ul id={groupId} role="list" className="mt-1 flex flex-col gap-1">
            {advanced.map((menu) => (
              <li key={menu.href}>
                <NavigationItem menu={menu} className=" pl-6" />
              </li>
            ))}
          </ul>
        )}
      </li>
      {admin.map((menu) => (
        <li key={menu.href}>
          <NavigationItem menu={menu} className="" />
        </li>
      ))}
    </ul>
  );
}
