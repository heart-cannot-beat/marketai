import { ConnectionCheckPanel } from '../data-sources/ConnectionCheckPanel';

export function SettingsTab() {
  return (
    <div className="space-y-6">
      <section>
        <ConnectionCheckPanel />
      </section>
    </div>
  );
}
