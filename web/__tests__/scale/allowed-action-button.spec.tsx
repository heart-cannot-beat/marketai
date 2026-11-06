/**
 * @jest-environment jsdom
 */
import { render, screen, fireEvent } from '@testing-library/react';
import {
  AllowedActionButton,
  type ActionEligibility,
} from '@/components/scale/shared/AllowedActionButton';

jest.mock('next-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (key: string, opts?: { defaultValue?: string }) =>
      opts?.defaultValue ? opts.defaultValue : `${ns}:${key}`,
  }),
}));

/**
 * Verifies the spec's central invariant for every Scale command surface:
 * "server-driven allowed_actions ... never inferred client-side" -- this
 * button's enabled/disabled state and click-through come entirely from the
 * `ActionEligibility` object the server sent, never from any local guess
 * about role or lifecycle.
 */
describe('AllowedActionButton', () => {
  it('renders nothing when the server did not list this action at all', () => {
    const onClick = jest.fn();
    const { container } = render(
      <AllowedActionButton
        action={undefined}
        label="Activate"
        onClick={onClick}
      />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders enabled and fires onClick when the server allows it', () => {
    const onClick = jest.fn();
    const action: ActionEligibility = {
      action: 'activate',
      allowed: true,
      reason_code: null,
    };
    render(
      <AllowedActionButton action={action} label="Activate" onClick={onClick} />
    );

    const button = screen.getByRole('button', { name: 'Activate' });
    expect(button).not.toBeDisabled();
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('renders disabled and never fires onClick when the server denies it', () => {
    const onClick = jest.fn();
    const action: ActionEligibility = {
      action: 'pause',
      allowed: false,
      reason_code: 'invalid_lifecycle',
    };
    render(
      <AllowedActionButton action={action} label="Pause" onClick={onClick} />
    );

    const button = screen.getByRole('button', { name: 'Pause' });
    expect(button).toHaveAttribute('aria-disabled', 'true');
    expect(button).toHaveAttribute('title', 'invalid_lifecycle');
    fireEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('renders disabled while a mutation is in flight, independent of server eligibility', () => {
    const onClick = jest.fn();
    const action: ActionEligibility = {
      action: 'close',
      allowed: true,
      reason_code: null,
    };
    render(
      <AllowedActionButton
        action={action}
        label="Close"
        onClick={onClick}
        busy
      />
    );
    const button = screen.getByRole('button', { name: 'Close' });
    expect(button).toBeDisabled();
  });
});
