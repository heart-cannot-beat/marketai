import { EmptyState } from '@/components/shared';

interface FeatureEmptyStateProps {
  title: string;
  description: string;
  action?: React.ReactNode;
}

export function FeatureEmptyState({
  title,
  description,
  action,
}: FeatureEmptyStateProps) {
  return (
    <div className="mt-8">
      <EmptyState title={title} description={description} action={action} />
    </div>
  );
}
