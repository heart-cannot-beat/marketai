/**
 * @jest-environment jsdom
 */
import { render, screen, within } from '@testing-library/react';
import { FindingList } from '@/components/ideas/FindingList';
import en from '../../../locales/en/ideas.json';
import { finding } from './fixtures';

/**
 * Words an ad image shows besides its copy (#265): the check lists them on its
 * "facts" finding, named by what is wrong, in the ad's own language, next to
 * any number or price among them that the facts don't state.
 */
describe('FindingList and words besides the copy (#265)', () => {
  it('names an unsourced number in the extra words and lists every extra word', () => {
    render(
      <FindingList
        language="en-US"
        findings={[
          finding('facts_supported', {
            gate_reason: 'extra_text_unsupported',
            extra_text: ['Electricity bill $68.25', 'Range 250 mi'],
            unsupported_claims: ['$68.25'],
          }),
        ]}
      />
    );
    const item = screen.getByTestId('finding');
    expect(
      within(item).getByText(en['finding-gate-extra_text_unsupported'], {
        exact: false,
      })
    ).toBeInTheDocument();
    expect(
      within(item).getByText(en['finding-extra-text'])
    ).toBeInTheDocument();
    const words = within(item).getByText('Range 250 mi').closest('ul');
    expect(words).toHaveAttribute('lang', 'en-US');
    expect(
      within(words as HTMLElement)
        .getAllByRole('listitem')
        .map((li) => li.textContent)
    ).toEqual(['Electricity bill $68.25', 'Range 250 mi']);
    expect(within(item).getByText('$68.25')).toBeInTheDocument();
  });

  it('asks a person to look at extra words the facts cannot settle', () => {
    render(
      <FindingList
        language="en-US"
        findings={[
          finding('facts_supported', {
            status: 'unknown',
            gate_reason: 'extra_text',
            extra_text: ['Meets all standards'],
          }),
        ]}
      />
    );
    const item = screen.getByTestId('finding');
    expect(
      within(item).getByText(en['finding-gate-extra_text'], { exact: false })
    ).toBeInTheDocument();
    expect(within(item).getByText('Meets all standards')).toBeInTheDocument();
    expect(
      within(item).queryByText(en['finding-unsupported'])
    ).not.toBeInTheDocument();
  });

  it('shows no extra-words list for a finding without any', () => {
    render(<FindingList findings={[finding('facts_supported')]} />);
    expect(
      screen.queryByText(en['finding-extra-text'])
    ).not.toBeInTheDocument();
  });
});
