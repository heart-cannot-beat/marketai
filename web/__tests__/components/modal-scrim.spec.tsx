/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { MODAL_SCRIM_STYLE, ModalScrim } from '@/components/shared/ModalScrim';

/**
 * Issue #193: the dialog scrim used to sit inside a `space-y-*` page column,
 * whose `margin-top` left a ~23px strip at the top of the page uncovered.
 */
describe('ModalScrim', () => {
  function renderInColumn(onScrimClick?: () => void) {
    return render(
      <main className="space-y-6">
        <p>{'page above'}</p>
        <ModalScrim labelledBy="scrim-title" onScrimClick={onScrimClick}>
          <div className="modal-box">
            <h3 id="scrim-title">{'确认'}</h3>
            <button type="button">{'好的'}</button>
          </div>
        </ModalScrim>
      </main>
    );
  }

  it('renders into <body>, outside any page column, pinned to the viewport', () => {
    const { container } = renderInColumn();
    const dialog = screen.getByRole('dialog', { name: '确认' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog.parentElement).toBe(document.body);
    expect(container.contains(dialog)).toBe(false);
    expect(dialog).toHaveStyle({
      position: 'fixed',
      top: '0px',
      right: '0px',
      bottom: '0px',
      left: '0px',
      margin: '0px',
    });
  });

  it('pads the box clear of the safe areas while the scrim covers them', () => {
    renderInColumn();
    const style = screen.getByRole('dialog').getAttribute('style') ?? '';
    // jsdom drops env() values it cannot parse, so check the declared style.
    expect(MODAL_SCRIM_STYLE.paddingTop).toBe('env(safe-area-inset-top)');
    expect(MODAL_SCRIM_STYLE.paddingBottom).toBe('env(safe-area-inset-bottom)');
    expect(style).toMatch(/position: fixed/);
  });

  it('a click on the scrim itself dismisses; a click in the box does not', () => {
    const onScrimClick = jest.fn();
    renderInColumn(onScrimClick);
    fireEvent.click(screen.getByRole('button', { name: '好的' }));
    expect(onScrimClick).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('dialog'));
    expect(onScrimClick).toHaveBeenCalledTimes(1);
  });
});
