import { useTranslation } from 'next-i18next';
import type { VersionActionEligibility } from '@/hooks/library/useResolvedLibraryVersion';
import { ActionToolbar, type ToolbarButton } from './EntryActions';

/**
 * The actions `_version_allowed_actions` in `app/library/v1_router.py` lists
 * for a published version, in toolbar order. Whether each is allowed, and
 * why not, comes only from the server.
 */
export const VERSION_ACTIONS = ['withdraw', 'fork', 'clone_product'] as const;

export type VersionAction = (typeof VERSION_ACTIONS)[number];

interface VersionActionsProps {
  allowedActions: VersionActionEligibility[];
  onWithdraw: () => void;
  onFork: () => void;
  onCloneProduct: () => void;
}

export function VersionActions({
  allowedActions,
  onWithdraw,
  onFork,
  onCloneProduct,
}: VersionActionsProps) {
  const { t } = useTranslation('library');

  const handlers: Record<VersionAction, () => void> = {
    withdraw: onWithdraw,
    fork: onFork,
    clone_product: onCloneProduct,
  };

  const actionMap = new Map(allowedActions.map((a) => [a.action, a]));
  const buttons: ToolbarButton[] = VERSION_ACTIONS.flatMap((action) => {
    const eligibility = actionMap.get(action);
    if (!eligibility) return [];
    return [
      {
        command: action,
        label: t(`commands.${action}`),
        action: {
          allowed: eligibility.allowed,
          reason_code: eligibility.reason_code ?? null,
        },
        onClick: handlers[action],
        variant: action === 'withdraw' ? 'warning' : 'default',
      },
    ];
  });

  if (buttons.length === 0) return null;

  return (
    <ActionToolbar label={t('commands.version-actions')} buttons={buttons} />
  );
}
