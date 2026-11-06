/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { MetaModeSelect } from '@/components/scale/shared/MetaModeSelect';

jest.mock('next-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (key: string, opts?: { defaultValue?: string }) =>
      opts?.defaultValue ? opts.defaultValue : `${ns}:${key}`,
  }),
}));

const mockUseDomainQuery = jest.fn();
jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (...args: unknown[]) => mockUseDomainQuery(...args),
}));

describe('MetaModeSelect', () => {
  beforeEach(() => {
    mockUseDomainQuery.mockReset();
  });

  it('renders only the modes returned by the server', () => {
    mockUseDomainQuery.mockReturnValue({
      data: { modes: ['customer_sandbox', 'controlled_mock'] },
    });
    const onChange = jest.fn();

    render(<MetaModeSelect value="customer_sandbox" onChange={onChange} />);

    const select = screen.getByRole('combobox', {
      name: 'scale:meta-mode.label',
    });
    expect(screen.getAllByRole('option')).toHaveLength(2);
    expect(
      screen.queryByRole('option', {
        name: 'scale:meta-mode.internal_validation',
      })
    ).not.toBeInTheDocument();

    fireEvent.change(select, { target: { value: 'controlled_mock' } });
    expect(onChange).toHaveBeenCalledWith('controlled_mock');
  });

  it('keeps the current mode selectable while the server response is pending', () => {
    mockUseDomainQuery.mockReturnValue({ data: undefined });

    render(<MetaModeSelect value="customer_sandbox" onChange={jest.fn()} />);

    expect(screen.getByRole('option')).toHaveValue('customer_sandbox');
  });
});
