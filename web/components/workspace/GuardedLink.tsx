import { useCallback, type AnchorHTMLAttributes, type ReactNode } from 'react';
import Link from 'next/link';
import { useOptionalNavigationBlocker } from './NavigationBlocker';

interface GuardedLinkProps extends Omit<
  AnchorHTMLAttributes<HTMLAnchorElement>,
  'href'
> {
  href: string;
  children?: ReactNode;
}

export function GuardedLink({
  href,
  children,
  onClick,
  ...rest
}: GuardedLinkProps) {
  const blocker = useOptionalNavigationBlocker();

  const handleClick = useCallback(
    (e: React.MouseEvent<HTMLAnchorElement>) => {
      if (onClick) onClick(e);
      if (e.defaultPrevented) return;

      if (!blocker || !blocker.isBlocked()) {
        return;
      }

      e.preventDefault();
      blocker.navigate(href);
    },
    [blocker, href, onClick]
  );

  return (
    <Link href={href} onClick={handleClick} {...rest}>
      {children}
    </Link>
  );
}
