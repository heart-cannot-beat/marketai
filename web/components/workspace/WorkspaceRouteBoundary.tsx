import React from 'react';
import { useTranslation } from 'next-i18next';

interface Props {
  resetKey: string;
  children: React.ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
  lastResetKey?: string;
}

function WorkspaceRouteError({ onRetry }: { onRetry: () => void }) {
  const { t } = useTranslation('common');

  return (
    <div
      className="flex flex-col items-center justify-center py-16"
      role="alert"
    >
      <h2 className="text-lg font-semibold">
        {t('workspace-render-error-title')}
      </h2>
      <p className="mt-2 text-sm text-gray-600">
        {t('workspace-render-error-description')}
      </p>
      <button className="btn btn-primary btn-sm mt-4" onClick={onRetry}>
        {t('workspace-render-error-retry')}
      </button>
    </div>
  );
}

export class WorkspaceRouteBoundary extends React.Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromProps(props: Props, state: State) {
    if (state.hasError && props.resetKey !== state.lastResetKey) {
      return { hasError: false, error: null, lastResetKey: props.resetKey };
    }
    return { lastResetKey: props.resetKey };
  }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  render() {
    if (this.state.hasError) {
      return (
        <WorkspaceRouteError
          onRetry={() => this.setState({ hasError: false, error: null })}
        />
      );
    }
    return this.props.children;
  }
}
