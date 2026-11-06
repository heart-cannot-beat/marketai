/**
 * @jest-environment jsdom
 */
import { renderHook, waitFor } from '@testing-library/react';
import {
  dimensionsFromEntry,
  useTemplateDimensions,
} from '@/hooks/generation/useTemplateDimensions';

const ENTRY = {
  id: 'lib_t',
  display_name: 'Grid',
  current_version_id: 'libver_2',
  versions: [
    {
      id: 'libver_1',
      content: { dimensions: [{ id: 'old', name: 'Old', values: [] }] },
    },
    {
      id: 'libver_2',
      content: {
        dimensions: [
          {
            id: 'angle',
            name: 'Angle',
            values: [
              { id: 'lifestyle', name: 'Lifestyle' },
              { id: 'studio' },
              { name: 'no id, skipped' },
            ],
          },
          { id: 'tone', values: [{ id: 'warm', name: 'Warm' }] },
        ],
      },
    },
  ],
};

const mockGetLibraryEntry = jest.fn();

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'ws-a' } }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => ({
    getLibraryEntry: (...args: unknown[]) => mockGetLibraryEntry(...args),
  }),
}));

describe('dimensionsFromEntry', () => {
  test('projects the current effective version into server-shaped keys', () => {
    expect(dimensionsFromEntry(ENTRY)).toEqual([
      {
        key: 'lib_t:angle',
        templateEntryId: 'lib_t',
        templateName: 'Grid',
        dimensionId: 'angle',
        name: 'Angle',
        values: [
          { id: 'lifestyle', name: 'Lifestyle' },
          { id: 'studio', name: 'studio' },
        ],
      },
      {
        key: 'lib_t:tone',
        templateEntryId: 'lib_t',
        templateName: 'Grid',
        dimensionId: 'tone',
        name: 'tone',
        values: [{ id: 'warm', name: 'Warm' }],
      },
    ]);
  });

  test('an entry without an effective version yields null', () => {
    expect(
      dimensionsFromEntry({ ...ENTRY, current_version_id: null })
    ).toBeNull();
    expect(
      dimensionsFromEntry({ ...ENTRY, current_version_id: 'libver_9' })
    ).toBeNull();
  });
});

describe('useTemplateDimensions', () => {
  beforeEach(() => mockGetLibraryEntry.mockReset());

  test('reads every selected template once and flags the ones that fail', async () => {
    mockGetLibraryEntry.mockImplementation(async ({ entryId }) =>
      entryId === 'lib_t'
        ? { kind: 'ok', status: 200, data: ENTRY, meta: {} }
        : { kind: 'problem', status: 404, problem: {}, meta: {} }
    );

    const { result } = renderHook(() =>
      useTemplateDimensions(['lib_t', 'lib_missing'])
    );
    expect(result.current.isLoading).toBe(true);

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(mockGetLibraryEntry).toHaveBeenCalledTimes(2);
    expect(mockGetLibraryEntry).toHaveBeenCalledWith({
      workspaceSlug: 'ws-a',
      entryId: 'lib_t',
    });
    expect(result.current.dimensions.map((d) => d.key)).toEqual([
      'lib_t:angle',
      'lib_t:tone',
    ]);
    expect(result.current.failedTemplateIds).toEqual(['lib_missing']);
  });

  test('no selection means no request', async () => {
    const { result } = renderHook(() => useTemplateDimensions([]));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(mockGetLibraryEntry).not.toHaveBeenCalled();
    expect(result.current.dimensions).toEqual([]);
  });
});
