/** @jest-environment jsdom */
import { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import {
  ImageSelectionCanvas,
  type ImageRegion,
} from '@/components/workspace/ImageSelectionCanvas';

jest.mock('next-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

test('scaled rectangle draw, move, corner resize and cancel preserve original coordinates', () => {
  window.PointerEvent = MouseEvent as typeof PointerEvent;
  let selected: ImageRegion;
  function Canvas() {
    const [region, setRegion] = useState({ x: 0, y: 0, width: 1, height: 1 });
    selected = region;
    return (
      <ImageSelectionCanvas
        src="blob:source"
        mode="rectangle"
        region={region}
        onRegion={setRegion}
      />
    );
  }
  render(<Canvas />);
  const canvas = screen.getByRole('group');
  canvas.getBoundingClientRect = () => ({
    x: 0,
    y: 0,
    left: 0,
    top: 0,
    right: 400,
    bottom: 300,
    width: 400,
    height: 300,
    toJSON: () => ({}),
  });
  const image = screen.getByRole('img');
  Object.defineProperty(image, 'naturalWidth', { value: 1600 });
  Object.defineProperty(image, 'naturalHeight', { value: 1200 });
  fireEvent.load(image);
  fireEvent.pointerDown(canvas, { clientX: 300, clientY: 225, button: 0 });
  fireEvent.pointerMove(canvas, { clientX: 100, clientY: 75 });
  fireEvent.pointerUp(canvas, { clientX: 100, clientY: 75 });
  expect(selected!).toEqual({ x: 0.25, y: 0.25, width: 0.5, height: 0.5 });
  const selection = screen.getByLabelText('marketing-image-tools-selection');
  fireEvent.pointerDown(selection, { clientX: 200, clientY: 150, button: 0 });
  fireEvent.pointerMove(canvas, { clientX: 500, clientY: 500 });
  fireEvent.pointerUp(canvas, { clientX: 500, clientY: 500 });
  expect(selected!).toEqual({ x: 0.5, y: 0.5, width: 0.5, height: 0.5 });
  fireEvent.pointerDown(selection.querySelector('[data-handle="nw"]')!, {
    clientX: 200,
    clientY: 150,
    button: 0,
  });
  fireEvent.pointerUp(canvas, { clientX: 100, clientY: 75 });
  expect(selected!).toEqual({ x: 0.25, y: 0.25, width: 0.75, height: 0.75 });
  fireEvent.pointerDown(canvas, { clientX: 0, clientY: 0, button: 0 });
  fireEvent.pointerMove(canvas, { clientX: 100, clientY: 75 });
  fireEvent.pointerCancel(canvas);
  expect(selected!).toEqual({ x: 0.25, y: 0.25, width: 0.75, height: 0.75 });
});
