/**
 * @jest-environment jsdom
 */
import { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { PasteBox } from '@/components/products/PasteBox';

beforeAll(() => {
  // jsdom has no object URLs; previews fall back to an icon.
  (URL as any).createObjectURL = undefined;
});

function Harness({ onSubmit }: { onSubmit: (v: string, f: File[]) => void }) {
  const [value, setValue] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  return (
    <PasteBox
      id="paste"
      label="Product link or description"
      value={value}
      onValueChange={setValue}
      files={files}
      onFilesChange={setFiles}
      onSubmit={() => onSubmit(value, files)}
      submitLabel="Read product"
    />
  );
}

function png(name: string) {
  return new File(['x'], name, { type: 'image/png' });
}

describe('PasteBox', () => {
  it('cannot submit while empty', () => {
    render(<Harness onSubmit={jest.fn()} />);
    expect(screen.getByRole('button', { name: 'Read product' })).toBeDisabled();
  });

  it('submits text alone', () => {
    const onSubmit = jest.fn();
    render(<Harness onSubmit={onSubmit} />);
    fireEvent.change(screen.getByLabelText('Product link or description'), {
      target: { value: '一款家用充电桩' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Read product' }));
    expect(onSubmit).toHaveBeenCalledWith('一款家用充电桩', []);
  });

  it('shows which site a pasted link will be read from', () => {
    render(<Harness onSubmit={jest.fn()} />);
    fireEvent.change(screen.getByLabelText('Product link or description'), {
      target: { value: 'https://shop.example/p/wallbox' },
    });
    expect(
      screen.getByText('Will read the link: shop.example')
    ).toBeInTheDocument();
  });

  it('takes images alone, from the picker or a drop, and can remove them', () => {
    const onSubmit = jest.fn();
    render(<Harness onSubmit={onSubmit} />);
    fireEvent.change(screen.getByTestId('paste-file-input'), {
      target: { files: [png('front.png')] },
    });
    fireEvent.drop(screen.getByTestId('paste-dropzone'), {
      dataTransfer: { files: [png('side.png')] },
    });
    expect(screen.getByLabelText('2 images added')).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole('button', { name: 'Remove image side.png' })
    );
    fireEvent.click(screen.getByRole('button', { name: 'Read product' }));
    expect(onSubmit).toHaveBeenCalledWith('', [expect.any(File)]);
    expect(onSubmit.mock.calls[0][1][0].name).toBe('front.png');
  });

  it('submits a link, text and images together', () => {
    const onSubmit = jest.fn();
    render(<Harness onSubmit={onSubmit} />);
    fireEvent.change(screen.getByLabelText('Product link or description'), {
      target: { value: 'https://shop.example/p 白色款' },
    });
    fireEvent.change(screen.getByTestId('paste-file-input'), {
      target: { files: [png('front.png')] },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Read product' }));
    expect(onSubmit).toHaveBeenCalledWith('https://shop.example/p 白色款', [
      expect.any(File),
    ]);
  });

  it('says why a file was not added', () => {
    render(<Harness onSubmit={jest.fn()} />);
    fireEvent.change(screen.getByTestId('paste-file-input'), {
      target: {
        files: [new File(['x'], 'anim.gif', { type: 'image/gif' })],
      },
    });
    expect(screen.getByRole('alert')).toHaveTextContent(
      'anim.gif is not a PNG, JPEG or WebP image'
    );
  });
});
