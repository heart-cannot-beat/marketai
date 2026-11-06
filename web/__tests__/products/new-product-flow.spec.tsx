/**
 * @jest-environment jsdom
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { NewProductFlow } from '@/components/products/NewProductFlow';

const mockPush = jest.fn();
let mockIntake: any;

jest.mock('next/router', () => ({
  useRouter: () => ({ push: mockPush, query: { slug: 'acme' } }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'OWNER' } }),
}));
jest.mock('@/hooks/products/useProductIntake', () => ({
  useProductIntake: () => mockIntake,
}));
jest.mock('@/hooks/products/useIntakeImageUrl', () => ({
  useIntakeImageUrl: () => ({ blobUrl: null, isLoading: false, failed: false }),
}));

function intake(overrides: Record<string, unknown> = {}) {
  return {
    phase: 'input',
    detail: null,
    uploadProgress: null,
    problem: null,
    lastInput: null,
    submit: jest.fn(),
    confirm: jest.fn(),
    startOver: jest.fn(),
    resumePolling: jest.fn(),
    ...overrides,
  };
}

const READY = {
  intake_id: 'pi_1',
  status: 'ready',
  execution_mode: 'mock',
  input: { url: null, text_chars: 30, upload_count: 0 },
  replayed: false,
  card: {
    name: 'VoltNest 壁挂充电桩',
    description: '家用充电桩',
    specs: {},
    requires_variant_selection: false,
    selling_points: ['快'],
    audiences: [{ who: '新车主', why: '', pain: '' }],
    must_match: [],
    images: [],
    default_image_ids: [],
  },
};

beforeEach(() => {
  mockPush.mockClear();
  window.sessionStorage.clear();
});

describe('NewProductFlow', () => {
  it('submits whatever was pasted', () => {
    mockIntake = intake();
    render(<NewProductFlow />);
    expect(
      screen.getByRole('heading', { level: 1, name: 'Add product' })
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Product link or description'), {
      target: { value: '一款家用充电桩' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Read product' }));
    expect(mockIntake.submit).toHaveBeenCalledWith({
      raw: '一款家用充电桩',
      files: [],
    });
  });

  it('shows upload progress while images go up', () => {
    mockIntake = intake({
      phase: 'uploading',
      uploadProgress: { done: 1, total: 3 },
    });
    render(<NewProductFlow />);
    expect(
      screen.getByRole('button', { name: 'Uploading images 1/3' })
    ).toBeDisabled();
  });

  it('explains a refused upload', () => {
    mockIntake = intake({
      problem: { stage: 'upload', code: 'upload_rejected', problem: null },
    });
    render(<NewProductFlow />);
    expect(screen.getByRole('alert')).toHaveTextContent(
      'An image did not pass its checks'
    );
  });

  it('shows the steps while recognising', () => {
    mockIntake = intake({
      phase: 'working',
      detail: { ...READY, status: 'extracting', card: null },
    });
    render(<NewProductFlow />);
    expect(screen.getByText('Reading the product')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Start over' }));
    expect(mockIntake.startOver).toHaveBeenCalled();
  });

  it('opens the card editor and confirms', () => {
    mockIntake = intake({ phase: 'ready', detail: READY });
    render(<NewProductFlow />);
    fireEvent.click(screen.getByRole('button', { name: 'Confirm product' }));
    expect(mockIntake.confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        card: expect.objectContaining({ name: 'VoltNest 壁挂充电桩' }),
      })
    );
  });

  it('translates a refused confirm into guidance', () => {
    mockIntake = intake({
      phase: 'ready',
      detail: READY,
      problem: {
        stage: 'confirm',
        code: 'image_not_available',
        problem: { code: 'image_not_available', detail: '图片 img_x 不存在' },
      },
    });
    render(<NewProductFlow />);
    expect(screen.getByRole('alert')).toHaveTextContent(
      'An image is no longer available. Choose the images again.'
    );
  });

  it('goes back to the product home with the new product highlighted', async () => {
    mockIntake = intake({
      phase: 'confirmed',
      detail: { ...READY, status: 'confirmed', product_id: 'prod_9' },
    });
    await act(async () => {
      render(<NewProductFlow />);
    });
    expect(mockPush).toHaveBeenCalledWith('/w/acme/products');
    expect(window.sessionStorage.getItem('products:acme:highlight')).toBe(
      'prod_9'
    );
  });
});
