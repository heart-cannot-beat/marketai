/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { HealthBadge } from '@/components/scale/shared/HealthBadge';
import { MaturityIndicator } from '@/components/scale/shared/MaturityIndicator';
import { LifecycleBadge } from '@/components/scale/shared/LifecycleBadge';

jest.mock('next-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (key: string, opts?: { defaultValue?: string }) =>
      opts?.defaultValue ? opts.defaultValue : `${ns}:${key}`,
  }),
}));

describe('HealthBadge', () => {
  it('renders a "no evidence" state when health is null', () => {
    render(<HealthBadge health={null} />);
    expect(screen.getByText('scale:health.no-evidence')).toBeInTheDocument();
  });

  it('renders "not classified" when an evaluation gave a reason instead', () => {
    render(<HealthBadge health={null} reasonCode="conflicting_signals" />);
    const badge = screen.getByText('scale:health.unclassified');
    expect(badge).not.toHaveAttribute('title');
    expect(screen.queryByText('scale:health.no-evidence')).toBeNull();
  });

  it('renders the health value as its own label', () => {
    render(<HealthBadge health="creative_fatigue" reasonCode="" />);
    expect(screen.getByText('creative_fatigue')).not.toHaveAttribute('title');
  });
});

describe('MaturityIndicator', () => {
  it('renders the maturity value', () => {
    render(<MaturityIndicator maturity="economics_ready" />);
    expect(screen.getByText('economics_ready')).toBeInTheDocument();
  });
});

describe('LifecycleBadge', () => {
  it('renders independently per scope -- enrollment lifecycle', () => {
    render(<LifecycleBadge scope="enrollment" value="paused" />);
    expect(screen.getByText('paused')).toBeInTheDocument();
  });

  it('renders independently per scope -- advice lifecycle', () => {
    render(<LifecycleBadge scope="advice" value="active" />);
    expect(screen.getByText('active')).toBeInTheDocument();
  });

  it('renders independently per scope -- episode phase', () => {
    render(<LifecycleBadge scope="episode" value="collecting" />);
    expect(screen.getByText('collecting')).toBeInTheDocument();
  });
});
