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
  PlusIcon,
  Squares2X2Icon,
  EyeIcon,
  EllipsisHorizontalCircleIcon,
  ArchiveBoxIcon,
  ClockIcon,
} from '@heroicons/react/24/outline';
import classNames from 'classnames';
import useCanAccess from 'hooks/useCanAccess';
import env from '@/lib/env';
import { newProductHref } from '@/lib/workspace-navigator';
import {
  STEP_COPY,
  productViewHref,
  type WorkspaceProduct,
} from '@/lib/products/workspace-products';
import { useWorkspaceProducts } from '@/hooks/products/useWorkspaceProducts';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { NavigationItem } from '@/components/shared/shell/NavigationItems';
import type { MenuItem } from '@/components/shared/shell/NavigationItems';

interface WorkspaceNavigationProps {
  slug: string;
  activePathname: string | null;
  /** The phone menu renders a second copy; ids stay unique per copy. */
  instance?: 'sidebar' | 'menu';
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

const pathOf = (activePathname: string | null) =>
  activePathname ? activePathname.split('?')[0].split('#')[0] : null;

function areaMatcher(slug: string, activePathname: string | null) {
  const pathOnly = pathOf(activePathname);
  return (area: string) =>
    pathOnly !== null &&
    (pathOnly === `/w/${slug}/${area}` ||
      pathOnly.startsWith(`/w/${slug}/${area}/`));
}

/**
 * The folded group: it opens by itself while one of its areas is active, and
 * remembers a viewer's explicit choice (`ADVANCED_NAV_STORAGE_KEY`).
 */
function FoldedGroup({
  label,
  icon: Icon,
  items,
  groupId,
}: {
  label: string;
  icon: typeof WrenchScrewdriverIcon;
  items: MenuItem[];
  groupId: string;
}) {
  const groupActive = items.some((item) => item.active);

  // Server render and first client render agree (active area only); the
  // stored preference applies after mount.
  const [expanded, setExpanded] = useState(groupActive);
  useEffect(() => {
    if (readExpanded()) setExpanded(true);
  }, []);
  // Landing on an area of the group opens it; leaving it does not close it
  // again, so moving between areas never hides the link just used.
  useEffect(() => {
    if (groupActive) setExpanded(true);
  }, [groupActive]);

  return (
    <>
      <button
        type="button"
        className={classNames(
          'group flex w-full items-center gap-2 rounded p-2 px-2 text-left text-sm text-gray-900 hover:bg-gray-100 dark:text-gray-100 dark:hover:bg-gray-800',
          groupActive && !expanded && 'font-semibold'
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
        <Icon className="h-5 w-5 shrink-0" aria-hidden="true" />
        <span className="flex-1">{label}</span>
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
          {items.map((menu) => (
            <li key={menu.href}>
              <NavigationItem menu={menu} className=" pl-6" />
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

/**
 * Settings is every member's: a Member reads the spending limits there
 * (#126); everything else on it, and Members, stays the Owner's. Members
 * stays in the navigation in both layouts (the Owner/Member e2e reads it
 * there).
 */
function useAdminItems(isActive: (area: string) => boolean, slug: string) {
  const { t } = useTranslation('common');
  const { canAccess } = useCanAccess();
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
  return admin;
}

/**
 * Workspace navigation. With the ads-first switch off it is the feature list
 * of ADR 0011 / #165, unchanged; with it on, the product sidebar of ADR 0018.
 */
export function WorkspaceNavigation(props: WorkspaceNavigationProps) {
  return env.adsFirstEnabled ? (
    <ProductNavigation {...props} />
  ) : (
    <FeatureNavigation {...props} />
  );
}

/**
 * The front flow comes first -- Products / Marketing tests / Assets -- and
 * the v9 areas (New generation, Creative library, Delivery experiments,
 * Localization, Scale) sit in a collapsible "Advanced workbench" group:
 * still reachable by everyone and by URL, folded by default.
 */
function FeatureNavigation({
  slug,
  activePathname,
  instance = 'sidebar',
}: WorkspaceNavigationProps) {
  const { t } = useTranslation('common');
  const isActive = areaMatcher(slug, activePathname);

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
  const admin = useAdminItems(isActive, slug);

  return (
    <ul role="list" className="flex flex-1 flex-col gap-1">
      {front.map((menu) => (
        <li key={menu.href}>
          <NavigationItem menu={menu} className="" />
        </li>
      ))}
      <li>
        <FoldedGroup
          label={t('workspace-nav-advanced')}
          icon={WrenchScrewdriverIcon}
          items={advanced}
          groupId={
            instance === 'menu'
              ? 'workspace-menu-nav-advanced'
              : 'workspace-nav-advanced'
          }
        />
      </li>
      {admin.map((menu) => (
        <li key={menu.href}>
          <NavigationItem menu={menu} className="" />
        </li>
      ))}
    </ul>
  );
}

function ProductLink({
  slug,
  product,
  active,
}: {
  slug: string;
  product: WorkspaceProduct;
  active: boolean;
}) {
  const { t } = useTranslation('common');
  return (
    <GuardedLink
      href={productViewHref(slug, product.productId, 'ideas')}
      aria-current={active ? 'true' : undefined}
      data-testid="workspace-nav-product"
      className={classNames(
        'flex min-w-0 flex-col rounded px-2 py-1.5 text-sm',
        active
          ? 'bg-gray-800 text-white'
          : 'text-gray-900 hover:bg-gray-100 dark:text-gray-100 dark:hover:bg-gray-800'
      )}
    >
      <span className="truncate font-medium">
        {product.name ?? t('workspace-unnamed-product')}
      </span>
      <span
        className={classNames(
          'truncate text-xs',
          active ? 'text-gray-300' : 'text-gray-500 dark:text-gray-400'
        )}
      >
        {t(STEP_COPY[product.step].status, { count: product.stepCount })}
      </span>
    </GuardedLink>
  );
}

/**
 * The ads-first sidebar (ADR 0018, design §5.6): New product, Overview, the
 * Workspace's products with one status line each, Asset library,
 * Competitors, the folded "More" group with every older area, Settings.
 */
function ProductNavigation({
  slug,
  activePathname,
  instance = 'sidebar',
}: WorkspaceNavigationProps) {
  const { t } = useTranslation('common');
  const isActive = areaMatcher(slug, activePathname);
  const pathOnly = pathOf(activePathname);
  const { products, isLoading, problem, refresh } = useWorkspaceProducts();
  const productActive = (productId: string) => {
    const base = `/w/${slug}/p/${encodeURIComponent(productId)}`;
    return (
      pathOnly !== null &&
      (pathOnly === base || pathOnly.startsWith(`${base}/`))
    );
  };

  const library: MenuItem[] = [
    {
      name: t('workspace-nav-asset-library'),
      href: `/w/${slug}/assets`,
      icon: PhotoIcon,
      active: isActive('assets'),
      testId: 'workspace-nav-assets',
    },
    {
      name: t('workspace-nav-competitors'),
      href: `/w/${slug}/competitors`,
      icon: EyeIcon,
      active: isActive('competitors'),
      testId: 'workspace-nav-competitors',
    },
  ];

  const more: MenuItem[] = [
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
    {
      name: t('workspace-nav-library'),
      href: `/w/${slug}/library`,
      icon: RectangleStackIcon,
      active: isActive('library'),
    },
    {
      name: t('workspace-nav-generate'),
      href: `/w/${slug}/generate`,
      icon: SparklesIcon,
      active: isActive('generate'),
    },
    {
      name: t('workspace-nav-legacy-experiments'),
      href: `/w/${slug}/legacy-experiments`,
      icon: BeakerIcon,
      active: isActive('legacy-experiments'),
    },
    {
      name: t('workspace-nav-legacy-tests'),
      href: `/w/${slug}/tests`,
      icon: ArchiveBoxIcon,
      active: isActive('tests'),
    },
    {
      name: t('workspace-nav-earlier-tests'),
      href: `/w/${slug}/experiments`,
      icon: ClockIcon,
      active: isActive('experiments'),
    },
  ];
  const admin = useAdminItems(isActive, slug);
  const ids =
    instance === 'menu'
      ? {
          group: 'workspace-menu-nav-advanced',
          products: 'workspace-menu-nav-products',
        }
      : { group: 'workspace-nav-advanced', products: 'workspace-nav-products' };

  return (
    <div className="flex flex-1 flex-col gap-4">
      <GuardedLink
        href={newProductHref(slug)}
        data-testid="workspace-nav-new-product"
        className="btn btn-outline btn-sm w-full gap-2"
      >
        <PlusIcon className="h-4 w-4" aria-hidden="true" />
        {t('workspace-nav-new-product')}
      </GuardedLink>

      <ul role="list" className="flex flex-col gap-1">
        <li>
          <NavigationItem
            menu={{
              name: t('workspace-nav-overview'),
              href: `/w/${slug}/overview`,
              icon: Squares2X2Icon,
              active: isActive('overview'),
              testId: 'workspace-nav-overview',
            }}
            className=""
          />
        </li>
      </ul>

      <div className="flex flex-col gap-1">
        <p
          id={ids.products}
          className="px-2 text-xs font-semibold text-gray-500 dark:text-gray-400"
        >
          {t('workspace-nav-product-list')}
        </p>
        {problem ? (
          <div className="px-2 text-sm" role="alert">
            <p className="text-gray-700 dark:text-gray-300">
              {t('workspace-nav-products-problem')}
            </p>
            <button
              type="button"
              className="link text-sm"
              onClick={() => void refresh()}
            >
              {t('workspace-retry')}
            </button>
          </div>
        ) : isLoading ? (
          <p className="px-2 text-sm text-gray-500" role="status">
            {t('workspace-nav-products-loading')}
          </p>
        ) : products.length === 0 ? (
          <p className="px-2 text-sm text-gray-500 dark:text-gray-400">
            {t('workspace-nav-products-empty')}
          </p>
        ) : (
          <ul
            role="list"
            aria-labelledby={ids.products}
            className="flex flex-col gap-1"
          >
            {products.map((product) => (
              <li key={product.productId}>
                <ProductLink
                  slug={slug}
                  product={product}
                  active={productActive(product.productId)}
                />
              </li>
            ))}
          </ul>
        )}
      </div>

      <ul
        role="list"
        className="flex flex-col gap-1 border-t border-gray-200 pt-3 dark:border-gray-700"
      >
        {library.map((menu) => (
          <li key={menu.href}>
            <NavigationItem menu={menu} className="" />
          </li>
        ))}
      </ul>

      <div className="border-t border-gray-200 pt-3 dark:border-gray-700">
        <FoldedGroup
          label={t('workspace-nav-more')}
          icon={EllipsisHorizontalCircleIcon}
          items={more}
          groupId={ids.group}
        />
      </div>

      <ul
        role="list"
        className="flex flex-col gap-1 border-t border-gray-200 pt-3 dark:border-gray-700"
      >
        {admin.map((menu) => (
          <li key={menu.href}>
            <NavigationItem menu={menu} className="" />
          </li>
        ))}
      </ul>
    </div>
  );
}
