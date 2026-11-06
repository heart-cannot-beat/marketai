import { ManualFactImportPanel } from '../data-sources/ManualFactImportPanel';

interface FactsTabProps {
  entryId: string;
}

export function FactsTab({ entryId }: FactsTabProps) {
  return (
    <div className="space-y-4">
      <ManualFactImportPanel dataSourceEntryId={entryId} />
    </div>
  );
}
