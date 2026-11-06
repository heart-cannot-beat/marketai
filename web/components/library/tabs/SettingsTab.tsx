import { ConnectionCheckPanel } from '../data-sources/ConnectionCheckPanel';

interface SettingsTabProps {
  entryId: string;
}

export function SettingsTab({ entryId }: SettingsTabProps) {
  return (
    <div className="space-y-6">
      <section>
        <ConnectionCheckPanel dataSourceEntryId={entryId} />
      </section>
    </div>
  );
}
