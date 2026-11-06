import { useEffect, useState } from 'react';
import { useRouter } from 'next/router';
import TeamNavigation from './TeamNavigation';
import UserNavigation from './UserNavigation';
import { WorkspaceNavigation } from '@/components/workspace/WorkspaceNavigation';

const Navigation = () => {
  const { asPath, isReady, query } = useRouter();
  const [activePathname, setActivePathname] = useState<null | string>(null);

  const { slug } = query as { slug: string };
  const isWorkspaceRoute = asPath.startsWith('/w/');

  useEffect(() => {
    if (isReady && asPath) {
      const activePathname = new URL(asPath, location.href).pathname;
      setActivePathname(activePathname);
    }
  }, [asPath, isReady]);

  const Navigation = () => {
    if (isWorkspaceRoute && slug) {
      return (
        <WorkspaceNavigation slug={slug} activePathname={activePathname} />
      );
    } else if (slug) {
      return <TeamNavigation activePathname={activePathname} slug={slug} />;
    } else {
      return <UserNavigation activePathname={activePathname} />;
    }
  };

  return (
    <nav className="flex flex-1 flex-col">
      <Navigation />
    </nav>
  );
};

export default Navigation;
