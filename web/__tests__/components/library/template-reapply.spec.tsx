/**
 * @jest-environment jsdom
 */
import { createElement } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

let mockState: string;
let mockDiff: any[];
let mockError: string | null;
let mockLoadDiff: jest.Mock;
let mockSetResolution: jest.Mock;
let mockApply: jest.Mock;
let mockReset: jest.Mock;

jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, string>) => {
      if (opts) {
        let result = `library:${key}`;
        for (const [k, v] of Object.entries(opts)) {
          result = result.replace(`{{${k}}}`, String(v));
        }
        return result;
      }
      return `library:${key}`;
    },
  }),
}));

jest.mock('next/router', () => ({
  useRouter: () => ({
    query: { slug: 'acme' },
    asPath: '/w/acme/library',
    push: jest.fn(),
    replace: jest.fn(),
  }),
}));

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: {
      slug: 'acme',
      workspaceId: 'w1',
      role: 'OWNER',
      membershipId: 'm1',
      displayName: 'Acme',
    },
  }),
}));

jest.mock('@/hooks/library/useTemplateReapply', () => ({
  useTemplateReapply: () => ({
    state: mockState,
    diff: mockDiff,
    error: mockError,
    loadDiff: mockLoadDiff,
    setResolution: mockSetResolution,
    apply: mockApply,
    reset: mockReset,
  }),
}));

import { TemplateReapplyPanel } from '@/components/library/templates/TemplateReapplyPanel';
import { TemplateDiffTable } from '@/components/library/templates/TemplateDiffTable';

beforeEach(() => {
  mockState = 'idle';
  mockDiff = [];
  mockError = null;
  mockLoadDiff = jest.fn();
  mockSetResolution = jest.fn();
  mockApply = jest.fn();
  mockReset = jest.fn();
});

// ─── TemplateReapplyPanel ─────────────────────────────────────────────

describe('TemplateReapplyPanel', () => {
  const defaultProps = {
    templateVersionId: 'tv-1',
    schemaEntryId: 'se-1',
  };

  describe('idle state', () => {
    it('renders description and load-diff button', () => {
      render(createElement(TemplateReapplyPanel, defaultProps));
      expect(
        screen.getByText('library:template-reapply.description')
      ).toBeTruthy();
      expect(
        screen.getByText('library:template-reapply.load-diff')
      ).toBeTruthy();
    });

    it('calls loadDiff on button click', () => {
      render(createElement(TemplateReapplyPanel, defaultProps));
      fireEvent.click(screen.getByText('library:template-reapply.load-diff'));
      expect(mockLoadDiff).toHaveBeenCalledWith('tv-1', 'se-1');
    });
  });

  describe('loading state', () => {
    it('shows loading message', () => {
      mockState = 'loading';
      render(createElement(TemplateReapplyPanel, defaultProps));
      expect(screen.getByText('library:template-reapply.loading')).toBeTruthy();
    });
  });

  describe('complete state', () => {
    it('shows applied message and done button', () => {
      mockState = 'complete';
      render(createElement(TemplateReapplyPanel, defaultProps));
      expect(screen.getByText('library:template-reapply.applied')).toBeTruthy();
      fireEvent.click(screen.getByText('library:template-reapply.done'));
      expect(mockReset).toHaveBeenCalled();
    });
  });

  describe('error state', () => {
    it('shows error and retry button', () => {
      mockState = 'error';
      mockError = 'Something went wrong';
      render(createElement(TemplateReapplyPanel, defaultProps));
      expect(screen.getByText('Something went wrong')).toBeTruthy();
      fireEvent.click(screen.getByText('library:template-reapply.retry'));
      expect(mockReset).toHaveBeenCalled();
    });
  });

  describe('diffing state', () => {
    it('renders diff table and apply/cancel buttons', () => {
      mockState = 'diffing';
      mockDiff = [
        {
          dimensionId: 'd1',
          name: 'Color',
          kind: 'unchanged',
          templateValues: ['red'],
          localValues: ['red'],
          resolution: null,
        },
      ];
      render(createElement(TemplateReapplyPanel, defaultProps));
      expect(
        screen.getByText('library:template-reapply.diff-title')
      ).toBeTruthy();
      expect(screen.getByText('library:template-reapply.apply')).toBeTruthy();
      expect(screen.getByText('library:template-reapply.cancel')).toBeTruthy();
    });

    it('disables apply when conflicts are unresolved', () => {
      mockState = 'diffing';
      mockDiff = [
        {
          dimensionId: 'd1',
          name: 'Color',
          kind: 'conflict',
          templateValues: ['red'],
          localValues: ['blue'],
          resolution: null,
        },
      ];
      render(createElement(TemplateReapplyPanel, defaultProps));
      const applyBtn = screen.getByText('library:template-reapply.apply');
      expect(applyBtn.closest('button')!.hasAttribute('disabled')).toBe(true);
    });

    it('enables apply when all conflicts are resolved', () => {
      mockState = 'diffing';
      mockDiff = [
        {
          dimensionId: 'd1',
          name: 'Color',
          kind: 'conflict',
          templateValues: ['red'],
          localValues: ['blue'],
          resolution: 'accept_template',
        },
      ];
      render(createElement(TemplateReapplyPanel, defaultProps));
      const applyBtn = screen.getByText('library:template-reapply.apply');
      expect(applyBtn.closest('button')!.hasAttribute('disabled')).toBe(false);
    });

    it('calls apply with schemaEntryId', () => {
      mockState = 'diffing';
      mockDiff = [
        {
          dimensionId: 'd1',
          name: 'Color',
          kind: 'unchanged',
          templateValues: ['red'],
          localValues: ['red'],
          resolution: null,
        },
      ];
      render(createElement(TemplateReapplyPanel, defaultProps));
      fireEvent.click(screen.getByText('library:template-reapply.apply'));
      expect(mockApply).toHaveBeenCalledWith('se-1');
    });

    it('calls reset on cancel', () => {
      mockState = 'diffing';
      mockDiff = [];
      render(createElement(TemplateReapplyPanel, defaultProps));
      fireEvent.click(screen.getByText('library:template-reapply.cancel'));
      expect(mockReset).toHaveBeenCalled();
    });
  });

  describe('applying state', () => {
    it('shows applying text and disables button', () => {
      mockState = 'applying';
      mockDiff = [];
      render(createElement(TemplateReapplyPanel, defaultProps));
      const applyBtn = screen.getByText('library:template-reapply.applying');
      expect(applyBtn.closest('button')!.hasAttribute('disabled')).toBe(true);
    });
  });
});

// ─── TemplateDiffTable ─────────────────────────────────────────────────

describe('TemplateDiffTable', () => {
  const onResolve = jest.fn();

  beforeEach(() => {
    onResolve.mockClear();
  });

  it('shows no-changes message for empty diff', () => {
    render(createElement(TemplateDiffTable, { diff: [], onResolve }));
    expect(
      screen.getByText('library:template-reapply.no-changes')
    ).toBeTruthy();
  });

  it('renders dimension rows', () => {
    const diff = [
      {
        dimensionId: 'd1',
        name: 'Color',
        kind: 'unchanged' as const,
        templateValues: ['red', 'blue'],
        localValues: ['red', 'blue'],
        resolution: null,
      },
      {
        dimensionId: 'd2',
        name: 'Size',
        kind: 'template_only' as const,
        templateValues: ['S', 'M'],
        localValues: [] as string[],
        resolution: null,
      },
    ];
    render(createElement(TemplateDiffTable, { diff, onResolve }));
    expect(screen.getByText('Color')).toBeTruthy();
    expect(screen.getByText('Size')).toBeTruthy();
    expect(
      screen.getByText('library:template-reapply.kind.unchanged')
    ).toBeTruthy();
    expect(
      screen.getByText('library:template-reapply.kind.template_only')
    ).toBeTruthy();
  });

  it('shows resolution buttons for conflict rows only', () => {
    const diff = [
      {
        dimensionId: 'd1',
        name: 'Color',
        kind: 'conflict' as const,
        templateValues: ['red'],
        localValues: ['blue'],
        resolution: null,
      },
      {
        dimensionId: 'd2',
        name: 'Size',
        kind: 'unchanged' as const,
        templateValues: ['S'],
        localValues: ['S'],
        resolution: null,
      },
    ];
    render(createElement(TemplateDiffTable, { diff, onResolve }));
    const useTemplateBtns = screen.getAllByText(
      'library:template-reapply.use-template'
    );
    const keepLocalBtns = screen.getAllByText(
      'library:template-reapply.keep-local'
    );
    expect(useTemplateBtns).toHaveLength(1);
    expect(keepLocalBtns).toHaveLength(1);
  });

  it('calls onResolve with accept_template', () => {
    const diff = [
      {
        dimensionId: 'd1',
        name: 'Color',
        kind: 'conflict' as const,
        templateValues: ['red'],
        localValues: ['blue'],
        resolution: null,
      },
    ];
    render(createElement(TemplateDiffTable, { diff, onResolve }));
    fireEvent.click(screen.getByText('library:template-reapply.use-template'));
    expect(onResolve).toHaveBeenCalledWith('d1', 'accept_template');
  });

  it('calls onResolve with keep_local', () => {
    const diff = [
      {
        dimensionId: 'd1',
        name: 'Color',
        kind: 'conflict' as const,
        templateValues: ['red'],
        localValues: ['blue'],
        resolution: null,
      },
    ];
    render(createElement(TemplateDiffTable, { diff, onResolve }));
    fireEvent.click(screen.getByText('library:template-reapply.keep-local'));
    expect(onResolve).toHaveBeenCalledWith('d1', 'keep_local');
  });

  it('highlights selected resolution button', () => {
    const diff = [
      {
        dimensionId: 'd1',
        name: 'Color',
        kind: 'conflict' as const,
        templateValues: ['red'],
        localValues: ['blue'],
        resolution: 'accept_template' as const,
      },
    ];
    render(createElement(TemplateDiffTable, { diff, onResolve }));
    const useTemplateBtn = screen.getByText(
      'library:template-reapply.use-template'
    );
    expect(useTemplateBtn.className).toContain('btn-primary');
    const keepLocalBtn = screen.getByText(
      'library:template-reapply.keep-local'
    );
    expect(keepLocalBtn.className).toContain('btn-ghost');
  });

  it('displays template and local values', () => {
    const diff = [
      {
        dimensionId: 'd1',
        name: 'Color',
        kind: 'conflict' as const,
        templateValues: ['red', 'green'],
        localValues: ['blue'],
        resolution: null,
      },
    ];
    render(createElement(TemplateDiffTable, { diff, onResolve }));
    expect(screen.getByText('red, green')).toBeTruthy();
    expect(screen.getByText('blue')).toBeTruthy();
  });
});
