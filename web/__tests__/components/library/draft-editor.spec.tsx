/**
 * @jest-environment jsdom
 */
import { createElement } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

jest.mock('next-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (key: string, opts?: Record<string, string>) => {
      if (opts?.version)
        return `${ns}:${key}`.replace('{{version}}', opts.version);
      if (opts?.type) return `${ns}:${key}`.replace('{{type}}', opts.type);
      if (opts?.count) return `${ns}:${key}`.replace('{{count}}', opts.count);
      if (opts?.code) return `${ns}:${key}`.replace('{{code}}', opts.code);
      if (opts?.revision)
        return `${ns}:${key}`.replace('{{revision}}', opts.revision);
      return `${ns}:${key}`;
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

import { DraftEditor } from '@/components/library/draft/DraftEditor';
import type {
  UseDraftSessionResult,
  ConflictState,
  ValidationResult,
} from '@/hooks/library/useDraftSession';

function makeSession(
  overrides?: Partial<UseDraftSessionResult>
): UseDraftSessionResult {
  return {
    isSupported: true,
    buffer: { fields: { display_name: 'Test' }, _unknown: {} },
    isDirty: false,
    isSaving: false,
    saveProblem: undefined,
    isValidating: false,
    validation: null,
    conflict: null,
    updateField: jest.fn(),
    updateFields: jest.fn(),
    replaceContent: jest.fn(),
    save: jest.fn(async () => true),
    validate: jest.fn(async () => true),
    acknowledgeWarning: jest.fn(),
    resolveConflictWithServer: jest.fn(),
    resolveConflictWithLocal: jest.fn(),
    cancelConflict: jest.fn(),
    discard: jest.fn(),
    canPublish: jest.fn(() => false),
    ...overrides,
  };
}

describe('DraftEditor', () => {
  it('shows unsupported schema state when not supported', () => {
    const session = makeSession({ isSupported: false });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 99,
        session,
      })
    );

    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(
      screen.getByText(/library:draft\.unsupported-schema$/)
    ).toBeInTheDocument();
  });

  it('renders editor with toolbar when supported', () => {
    const session = makeSession();
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    expect(screen.getByText('library:draft.editor-title')).toBeInTheDocument();
    expect(screen.getByText('library:draft.save')).toBeInTheDocument();
    expect(screen.getByText('library:draft.validate')).toBeInTheDocument();
    expect(screen.getByText('library:draft.discard')).toBeInTheDocument();
  });

  it('shows unsaved badge when dirty', () => {
    const session = makeSession({ isDirty: true });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    expect(screen.getByText('library:draft.unsaved')).toBeInTheDocument();
  });

  it('save button is disabled when not dirty', () => {
    const session = makeSession({ isDirty: false });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    const saveBtn = screen.getByText('library:draft.save');
    expect(saveBtn).toBeDisabled();
  });

  it('save button is enabled when dirty', () => {
    const session = makeSession({ isDirty: true });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    const saveBtn = screen.getByText('library:draft.save');
    expect(saveBtn).not.toBeDisabled();
  });

  it('shows saving state', () => {
    const session = makeSession({ isSaving: true, isDirty: true });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    expect(screen.getByText('library:draft.saving')).toBeInTheDocument();
  });

  it('discard button calls session.discard', () => {
    const session = makeSession({ isDirty: true });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    fireEvent.click(screen.getByText('library:draft.discard'));
    expect(session.discard).toHaveBeenCalled();
  });

  it('discard button is disabled when not dirty', () => {
    const session = makeSession({ isDirty: false });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    expect(screen.getByText('library:draft.discard')).toBeDisabled();
  });

  it('shows save error alert', () => {
    const session = makeSession({
      saveProblem: {
        type: 'error',
        title: 'Error',
        status: 500,
        detail: 'Internal server error',
        instance: '',
        code: 'internal_error',
        trace_id: '',
        retryable: true,
        field_errors: [],
      },
    });

    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('library:draft.save-error')).toBeInTheDocument();
    expect(
      screen.getByText('library:error-internal_error')
    ).toBeInTheDocument();
    expect(screen.queryByText('Internal server error')).toBeNull();
  });

  it('shows validation panel with errors', () => {
    const validation: ValidationResult = {
      findings: [
        {
          code: 'missing',
          severity: 'error',
          field: 'audience',
          message: 'Required field',
        },
      ],
      revision: 1,
      contentHash: 'h1',
      acknowledged: new Set(),
    };

    const session = makeSession({ validation });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    expect(
      screen.getByText(/library:draft\.validation-errors/)
    ).toBeInTheDocument();
    expect(screen.getByText('Required field')).toBeInTheDocument();
  });

  it('shows validation panel with warnings and acknowledge button', () => {
    const validation: ValidationResult = {
      findings: [
        {
          code: 'short_desc',
          severity: 'warning',
          field: 'description',
          message: 'Consider more detail',
        },
      ],
      revision: 1,
      contentHash: 'h1',
      acknowledged: new Set(),
    };

    const session = makeSession({ validation });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    expect(
      screen.getByText(/library:draft\.validation-warnings/)
    ).toBeInTheDocument();
    expect(screen.getByText('Consider more detail')).toBeInTheDocument();

    const ackBtn = screen.getByLabelText(/library:draft\.acknowledge-warning/);
    fireEvent.click(ackBtn);
    expect(session.acknowledgeWarning).toHaveBeenCalledWith('short_desc');
  });

  it('shows acknowledged badge for acknowledged warnings', () => {
    const validation: ValidationResult = {
      findings: [
        { code: 'w1', severity: 'warning', field: null, message: 'A warning' },
      ],
      revision: 1,
      contentHash: 'h1',
      acknowledged: new Set(['w1']),
    };

    const session = makeSession({ validation });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    expect(screen.getByText('library:draft.acknowledged')).toBeInTheDocument();
  });

  it('shows validation passed when no findings', () => {
    const validation: ValidationResult = {
      findings: [],
      revision: 1,
      contentHash: 'h1',
      acknowledged: new Set(),
    };

    const session = makeSession({ validation });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    expect(
      screen.getByText('library:draft.validation-passed')
    ).toBeInTheDocument();
  });

  it('shows conflict dialog when conflict exists', () => {
    const conflict: ConflictState = {
      serverRevision: 5,
      serverContent: { display_name: 'Server' },
      attemptedContent: { display_name: 'Local' },
      baseContent: { display_name: 'Original' },
    };

    const session = makeSession({ conflict });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    expect(
      screen.getByText('library:draft.conflict-title')
    ).toBeInTheDocument();
  });

  it('conflict dialog calls resolveConflictWithServer', () => {
    const conflict: ConflictState = {
      serverRevision: 5,
      serverContent: {},
      attemptedContent: {},
      baseContent: {},
    };

    const session = makeSession({ conflict });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    fireEvent.click(screen.getByText('library:draft.conflict-reload-server'));
    expect(session.resolveConflictWithServer).toHaveBeenCalled();
  });

  it('conflict dialog calls resolveConflictWithLocal', () => {
    const conflict: ConflictState = {
      serverRevision: 5,
      serverContent: {},
      attemptedContent: {},
      baseContent: {},
    };

    const session = makeSession({ conflict });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    fireEvent.click(screen.getByText('library:draft.conflict-keep-local'));
    expect(session.resolveConflictWithLocal).toHaveBeenCalled();
  });

  it('validate button disabled when dirty', () => {
    const session = makeSession({ isDirty: true });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    expect(screen.getByText('library:draft.validate')).toBeDisabled();
  });

  it('validate button enabled when not dirty', () => {
    const session = makeSession({ isDirty: false });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    expect(screen.getByText('library:draft.validate')).not.toBeDisabled();
  });

  it('mounts the product form and keeps the JSON editor behind a toggle', () => {
    const session = makeSession({
      buffer: {
        fields: {
          name: 'Widget Pro',
          description: '',
          audience: '',
          selling_points: [],
        },
        _unknown: {},
      },
    });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'product',
        schemaVersion: 1,
        session,
      })
    );

    expect(screen.getByDisplayValue('Widget Pro')).toBeInTheDocument();
    expect(
      screen.queryByLabelText('library:draft.json-label')
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('library:draft.show-json'));
    const json = screen.getByLabelText(
      'library:draft.json-label'
    ) as HTMLTextAreaElement;
    expect(JSON.parse(json.value)).toEqual({
      name: 'Widget Pro',
      description: '',
      audience: '',
      selling_points: [],
    });
  });

  it('edits entry types without an aligned form as JSON only', () => {
    const session = makeSession({
      buffer: {
        fields: { display_name: 'Jane', person_type: 'fictional' },
        _unknown: { rights: { regions: ['US'] } },
      },
    });
    render(
      createElement(DraftEditor, {
        entryId: 'e1',
        entryType: 'person',
        schemaVersion: 1,
        session,
      })
    );

    expect(
      screen.getByText(/library:draft\.json-only-hint/)
    ).toBeInTheDocument();
    expect(
      screen.queryByText('library:draft.show-json')
    ).not.toBeInTheDocument();
    const json = screen.getByLabelText(
      'library:draft.json-label'
    ) as HTMLTextAreaElement;
    expect(JSON.parse(json.value)).toMatchObject({
      display_name: 'Jane',
      rights: { regions: ['US'] },
    });

    fireEvent.change(json, {
      target: { value: '{"name": "Jane", "rights": {"regions": ["US"]}}' },
    });
    expect(session.replaceContent).toHaveBeenCalledWith({
      name: 'Jane',
      rights: { regions: ['US'] },
    });

    fireEvent.change(json, { target: { value: '{"name": ' } });
    expect(screen.getByRole('alert')).toHaveTextContent(
      /library:draft\.json-parse-error/
    );
    // Unparsable text never reaches the session.
    expect(session.replaceContent).toHaveBeenCalledTimes(1);
  });
});
