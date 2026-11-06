/**
 * @jest-environment jsdom
 */
import { createElement } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

let mockUpload: jest.Mock;
let mockResetUpload: jest.Mock;
let mockDownload: jest.Mock;
let mockResetDownload: jest.Mock;

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

jest.mock('@/hooks/library/useLibraryReferenceUpload', () => ({
  CONTENT_SAFETY_UNAVAILABLE: 'content_safety_unavailable',
  REFERENCE_GATE_REJECTED: 'reference_gate_rejected',
  useLibraryReferenceUpload: () => ({
    upload: mockUpload,
    reset: mockResetUpload,
    progress: mockProgress,
  }),
}));

jest.mock('@/hooks/library/useReferenceImageUrl', () => ({
  useReferenceImageUrl: () => ({
    blobUrl: null,
    isLoading: false,
    problem: undefined,
  }),
}));

jest.mock('@/hooks/library/useLibraryReferenceDownload', () => ({
  useLibraryReferenceDownload: () => ({
    download: mockDownload,
    state: mockDownloadState,
    error: mockDownloadError,
    reset: mockResetDownload,
  }),
}));

jest.mock('@/hooks/library/useLibraryBindings', () => ({
  useLibraryBindings: () => ({
    items: mockBindings,
    isLoading: mockBindingsLoading,
    problem: mockBindingsProblem,
  }),
}));

import { ReferenceUploadZone } from '@/components/library/references/ReferenceUploadZone';
import { ReferenceGateProgress } from '@/components/library/references/ReferenceGateProgress';
import { ReferenceBindingList } from '@/components/library/references/ReferenceBindingList';
import { DraftReferenceBindings } from '@/components/library/references/DraftReferenceBindings';
import type { UploadProgress } from '@/hooks/library/useLibraryReferenceUpload';

let mockProgress: UploadProgress;
let mockDownloadState: string;
let mockDownloadError: string | null;
let mockBindings: Record<string, unknown>[] | null;
let mockBindingsLoading: boolean;
let mockBindingsProblem: unknown;

beforeEach(() => {
  mockUpload = jest.fn();
  mockResetUpload = jest.fn();
  mockDownload = jest.fn();
  mockResetDownload = jest.fn();
  mockProgress = {
    phase: 'idle',
    fileName: '',
    bytesTransferred: 0,
    totalBytes: 0,
    referenceObjectId: null,
    referenceVersionId: null,
    referenceEntryId: null,
    boundEntryId: null,
    failureCode: null,
    problem: null,
  };
  mockDownloadState = 'idle';
  mockDownloadError = null;
  mockBindings = null;
  mockBindingsLoading = false;
  mockBindingsProblem = undefined;
});

// ── ReferenceUploadZone ────────────────────────────────────────────

describe('ReferenceUploadZone', () => {
  it('pastes clipboard images through the governed uploader only after rights confirmation', () => {
    render(
      <ReferenceUploadZone
        contextEntryId="entry-1"
        maxFileBytes={10 * 1024 * 1024}
      />
    );
    const region = screen.getByRole('region');
    const file = new File(['photo'], 'pasted.png', { type: 'image/png' });
    fireEvent.paste(region, { clipboardData: { files: [file] } });
    expect(mockUpload).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.paste(region, { clipboardData: { files: [file] } });
    expect(mockUpload).toHaveBeenCalledWith(
      file,
      expect.objectContaining({
        contextEntryId: 'entry-1',
        bindToContext: false,
      })
    );
  });

  it('rejects oversize and unsupported product files before upload', () => {
    render(<ReferenceUploadZone contextEntryId="entry-1" maxFileBytes={10} />);
    fireEvent.click(screen.getByRole('checkbox'));
    const input = screen.getByLabelText('library:references.file-input');
    fireEvent.change(input, {
      target: {
        files: [
          new File([new ArrayBuffer(11)], 'large.png', { type: 'image/png' }),
        ],
      },
    });
    expect(mockUpload).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'library:references.file-too-large'
    );
    fireEvent.change(input, {
      target: {
        files: [new File(['x'], 'script.svg', { type: 'image/svg+xml' })],
      },
    });
    expect(mockUpload).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'library:references.file-type-not-supported'
    );
  });
  it('renders upload zone in idle state, gated on the rights declaration', () => {
    render(
      createElement(ReferenceUploadZone, {
        contextEntryId: 'entry-1',
      })
    );

    // Until the operator confirms the rights declaration the drop zone is
    // inert (contract §7.2: rights state, scope, validity and declaration).
    expect(
      screen.getByText('library:references.confirm-rights-first')
    ).toBeInTheDocument();
    expect(screen.getByText('library:references.browse-files')).toBeDisabled();

    fireEvent.click(screen.getByRole('checkbox'));

    expect(
      screen.getByText('library:references.drop-or-browse')
    ).toBeInTheDocument();
    expect(screen.getByText('library:references.browse-files')).toBeEnabled();
  });

  it('uploads with the rights declaration and binds to a bindable context', () => {
    render(
      createElement(ReferenceUploadZone, {
        contextEntryId: 'entry-1',
        bindToContext: true,
      })
    );
    fireEvent.click(screen.getByRole('checkbox'));
    const file = new File([new ArrayBuffer(16)], 'charger.png', {
      type: 'image/png',
    });
    fireEvent.change(screen.getByLabelText('library:references.file-input'), {
      target: { files: [file] },
    });

    expect(mockUpload).toHaveBeenCalledWith(
      file,
      expect.objectContaining({
        contextEntryId: 'entry-1',
        bindToContext: true,
        rights: expect.objectContaining({
          scope: 'owned_product_photo',
          valid_to: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
        }),
      })
    );
  });

  it('does not upload before the rights declaration is confirmed', () => {
    render(
      createElement(ReferenceUploadZone, {
        contextEntryId: 'entry-1',
      })
    );
    const file = new File([new ArrayBuffer(16)], 'charger.png', {
      type: 'image/png',
    });
    fireEvent.change(screen.getByLabelText('library:references.file-input'), {
      target: { files: [file] },
    });
    expect(mockUpload).not.toHaveBeenCalled();
  });

  it('renders released state with upload-another button', () => {
    mockProgress = {
      phase: 'released',
      fileName: 'hero.png',
      bytesTransferred: 1024,
      totalBytes: 1024,
      referenceObjectId: 'ref-1',
      referenceVersionId: null,
      referenceEntryId: null,
      boundEntryId: null,
      failureCode: null,
      problem: null,
    };

    render(
      createElement(ReferenceUploadZone, {
        contextEntryId: 'entry-1',
      })
    );

    expect(
      screen.getByText('library:references.upload-complete')
    ).toBeInTheDocument();
    expect(
      screen.getByText('library:references.upload-another')
    ).toBeInTheDocument();
  });

  it('renders progress during transfer', () => {
    mockProgress = {
      phase: 'transferring',
      fileName: 'hero.png',
      bytesTransferred: 512,
      totalBytes: 1024,
      referenceObjectId: null,
      referenceVersionId: null,
      referenceEntryId: null,
      boundEntryId: null,
      failureCode: null,
      problem: null,
    };

    render(
      createElement(ReferenceUploadZone, {
        contextEntryId: 'entry-1',
      })
    );

    expect(screen.getByText('hero.png')).toBeInTheDocument();
  });

  it('renders try again on failure', () => {
    mockProgress = {
      phase: 'failed',
      fileName: 'bad.exe',
      bytesTransferred: 0,
      totalBytes: 100,
      referenceObjectId: null,
      referenceVersionId: null,
      referenceEntryId: null,
      boundEntryId: null,
      failureCode: null,
      problem: { code: 'reference_mime_not_allowed' },
    };

    render(
      createElement(ReferenceUploadZone, {
        contextEntryId: 'entry-1',
      })
    );

    expect(
      screen.getByText('library:references.try-again')
    ).toBeInTheDocument();
  });

  it('renders try again on rejection', () => {
    mockProgress = {
      phase: 'rejected',
      fileName: 'malware.png',
      bytesTransferred: 1024,
      totalBytes: 1024,
      referenceObjectId: null,
      referenceVersionId: null,
      referenceEntryId: null,
      boundEntryId: null,
      failureCode: null,
      problem: { code: 'reference_gate_rejected' },
    };

    render(
      createElement(ReferenceUploadZone, {
        contextEntryId: 'entry-1',
      })
    );

    expect(
      screen.getByText('library:references.try-again')
    ).toBeInTheDocument();
  });

  it('calls onComplete when released with referenceObjectId', () => {
    const onComplete = jest.fn();
    mockProgress = {
      phase: 'released',
      fileName: 'hero.png',
      bytesTransferred: 1024,
      totalBytes: 1024,
      referenceObjectId: 'ref-1',
      referenceVersionId: null,
      referenceEntryId: null,
      boundEntryId: null,
      failureCode: null,
      problem: null,
    };

    render(
      createElement(ReferenceUploadZone, {
        contextEntryId: 'entry-1',
        onComplete,
      })
    );

    expect(onComplete).toHaveBeenCalledWith('ref-1');
  });
});

// ── ReferenceGateProgress ──────────────────────────────────────────

describe('ReferenceGateProgress', () => {
  it('renders failed state with error', () => {
    render(
      createElement(ReferenceGateProgress, {
        progress: {
          phase: 'failed',
          fileName: 'bad.png',
          bytesTransferred: 0,
          totalBytes: 100,
          referenceObjectId: null,
          referenceVersionId: null,
          referenceEntryId: null,
          boundEntryId: null,
          failureCode: null,
          problem: {
            code: 'reference_mime_not_allowed',
            detail: 'Transfer failed',
          },
        },
      })
    );

    expect(
      screen.getByText('library:references.upload-failed')
    ).toBeInTheDocument();
    // The reason reads by its code (#253); the server's detail never shows.
    expect(
      screen.getByText('library:error-reference_mime_not_allowed')
    ).toBeInTheDocument();
    expect(screen.queryByText('Transfer failed')).toBeNull();
  });

  it('renders rejected state with error', () => {
    render(
      createElement(ReferenceGateProgress, {
        progress: {
          phase: 'rejected',
          fileName: 'suspect.png',
          bytesTransferred: 1024,
          totalBytes: 1024,
          referenceObjectId: null,
          referenceVersionId: null,
          referenceEntryId: null,
          boundEntryId: null,
          failureCode: null,
          problem: {
            code: 'reference_previously_rejected',
            detail: 'Content rejected',
          },
        },
      })
    );

    expect(
      screen.getByText('library:references.upload-rejected')
    ).toBeInTheDocument();
    expect(
      screen.getByText('library:error-reference_previously_rejected')
    ).toBeInTheDocument();
    expect(screen.queryByText('Content rejected')).toBeNull();
  });

  it('renders phase steps for active upload', () => {
    render(
      createElement(ReferenceGateProgress, {
        progress: {
          phase: 'processing',
          fileName: 'hero.png',
          bytesTransferred: 1024,
          totalBytes: 1024,
          referenceObjectId: null,
          referenceVersionId: null,
          referenceEntryId: null,
          boundEntryId: null,
          failureCode: null,
          problem: null,
        },
      })
    );

    expect(screen.getByText('hero.png')).toBeInTheDocument();
    expect(
      screen.getByText('library:references.phase.hashing')
    ).toBeInTheDocument();
    expect(
      screen.getByText('library:references.phase.processing')
    ).toBeInTheDocument();
  });

  it('shows percentage during transfer phase', () => {
    render(
      createElement(ReferenceGateProgress, {
        progress: {
          phase: 'transferring',
          fileName: 'hero.png',
          bytesTransferred: 512,
          totalBytes: 1024,
          referenceObjectId: null,
          referenceVersionId: null,
          referenceEntryId: null,
          boundEntryId: null,
          failureCode: null,
          problem: null,
        },
      })
    );

    expect(screen.getByText('50%')).toBeInTheDocument();
  });
});

// ── ReferenceBindingList ───────────────────────────────────────────

describe('ReferenceBindingList', () => {
  it('shows no-version message when versionId is null', () => {
    render(
      createElement(ReferenceBindingList, {
        entryId: 'entry-1',
        versionId: null,
      })
    );

    expect(
      screen.getByText('library:tabs.bindings.no-version')
    ).toBeInTheDocument();
  });

  it('shows loading state', () => {
    mockBindingsLoading = true;

    render(
      createElement(ReferenceBindingList, {
        entryId: 'entry-1',
        versionId: 'v-1',
      })
    );

    expect(
      screen.getByText('library:references.loading-bindings')
    ).toBeInTheDocument();
  });

  it('shows error state', () => {
    mockBindingsProblem = { detail: 'Server error' };

    render(
      createElement(ReferenceBindingList, {
        entryId: 'entry-1',
        versionId: 'v-1',
      })
    );

    expect(
      screen.getByText('library:references.bindings-error')
    ).toBeInTheDocument();
  });

  it('shows empty state when no bindings', () => {
    mockBindings = [];

    render(
      createElement(ReferenceBindingList, {
        entryId: 'entry-1',
        versionId: 'v-1',
      })
    );

    expect(screen.getByText('library:tabs.bindings.empty')).toBeInTheDocument();
  });

  it('renders bindings with download button for released references', () => {
    mockBindings = [
      {
        id: 'b1',
        reference_version_id: 'ref-v1',
        purpose: 'hero_image',
        reference_display_name: 'Hero Shot',
        gate_status: 'released',
      },
      {
        id: 'b2',
        reference_version_id: 'ref-v2',
        purpose: 'thumbnail',
        reference_display_name: 'Thumb',
        gate_status: 'processing',
      },
    ];

    render(
      createElement(ReferenceBindingList, {
        entryId: 'entry-1',
        versionId: 'v-1',
      })
    );

    expect(screen.getByText('Hero Shot')).toBeInTheDocument();
    expect(screen.getByText('hero_image')).toBeInTheDocument();
    expect(screen.getByText('Thumb')).toBeInTheDocument();

    const downloadButtons = screen.getAllByText('library:references.download');
    expect(downloadButtons.length).toBe(1);
  });

  it('calls download when download button clicked', () => {
    mockBindings = [
      {
        id: 'b1',
        reference_version_id: 'ref-v1',
        purpose: 'hero_image',
        reference_display_name: 'Hero Shot',
        gate_status: 'released',
      },
    ];

    render(
      createElement(ReferenceBindingList, {
        entryId: 'entry-1',
        versionId: 'v-1',
      })
    );

    fireEvent.click(screen.getByText('library:references.download'));
    expect(mockDownload).toHaveBeenCalledWith('ref-v1');
  });

  it('shows download error and dismiss', () => {
    mockBindings = [
      {
        id: 'b1',
        reference_version_id: 'ref-v1',
        purpose: 'hero_image',
        reference_display_name: 'Hero',
        gate_status: 'released',
      },
    ];
    mockDownloadError = 'Signed URL expired';

    render(
      createElement(ReferenceBindingList, {
        entryId: 'entry-1',
        versionId: 'v-1',
      })
    );

    expect(screen.getByText('Signed URL expired')).toBeInTheDocument();
    fireEvent.click(screen.getByText('library:references.dismiss-error'));
    expect(mockResetDownload).toHaveBeenCalled();
  });

  it('does not render download button for unreleased bindings', () => {
    mockBindings = [
      {
        id: 'b1',
        reference_version_id: 'ref-v1',
        purpose: 'hero_image',
        reference_display_name: 'Hero',
        gate_status: 'quarantine',
      },
    ];

    render(
      createElement(ReferenceBindingList, {
        entryId: 'entry-1',
        versionId: 'v-1',
      })
    );

    expect(screen.getByText('Hero')).toBeInTheDocument();
    expect(screen.queryByText('library:references.download')).toBeNull();
  });
});

// ── Gate failure codes and draft bindings (issue #158) ─────────────

describe('ReferenceGateProgress failure codes', () => {
  it('names the unconfigured content-safety service instead of a generic failure', () => {
    render(
      createElement(ReferenceGateProgress, {
        progress: {
          phase: 'failed',
          fileName: 'charger.png',
          bytesTransferred: 1024,
          totalBytes: 1024,
          referenceObjectId: 'ref-1',
          referenceVersionId: null,
          referenceEntryId: null,
          boundEntryId: null,
          failureCode: 'content_safety_unavailable',
          problem: { code: 'content_safety_unavailable' },
        },
      })
    );

    expect(
      screen.getByText('library:references.content-safety-unavailable')
    ).toBeInTheDocument();
    expect(
      screen.getByText('library:references.content-safety-unavailable-hint')
    ).toBeInTheDocument();
    expect(screen.queryByText('library:references.upload-failed')).toBeNull();
  });

  it('lists the promoting phase between the gate and release', () => {
    render(
      createElement(ReferenceGateProgress, {
        progress: {
          phase: 'promoting',
          fileName: 'charger.png',
          bytesTransferred: 1024,
          totalBytes: 1024,
          referenceObjectId: 'ref-1',
          referenceVersionId: null,
          referenceEntryId: null,
          boundEntryId: null,
          failureCode: null,
          problem: null,
        },
      })
    );

    expect(
      screen.getByText('library:references.phase.promoting')
    ).toBeInTheDocument();
  });
});

describe('DraftReferenceBindings', () => {
  it('renders the draft bindings the automatic bind produced', () => {
    render(
      createElement(DraftReferenceBindings, {
        bindings: [
          {
            reference_version_id: 'libver-1',
            purpose: 'product_reference',
            viewpoint: 'unspecified',
          },
        ],
      })
    );

    expect(screen.getByTestId('draft-reference-bindings')).toBeInTheDocument();
    expect(screen.getByText('product_reference')).toBeInTheDocument();
    expect(screen.getByText('unspecified')).toBeInTheDocument();
    expect(
      screen.getByTestId('reference-thumbnail-placeholder')
    ).toBeInTheDocument();
  });

  it('renders the empty hint without bindings', () => {
    render(createElement(DraftReferenceBindings, { bindings: [] }));
    expect(
      screen.getByText('library:references.draft-bindings-empty')
    ).toBeInTheDocument();
  });
});
