/**
 * @jest-environment jsdom
 */
import { renderHook } from '@testing-library/react';
import { useRouter } from 'next/router';
import { useGenerationNav } from '@/components/generation/shared/useGenerationNav';

jest.mock('next/router', () => ({
  useRouter: jest.fn(),
}));

const mockRouter = {
  query: { slug: 'test-ws' },
  push: jest.fn(),
};

beforeEach(() => {
  (useRouter as jest.Mock).mockReturnValue(mockRouter);
  mockRouter.push.mockClear();
});

describe('useGenerationNav', () => {
  it('builds draft URL with slug and draftId', () => {
    const { result } = renderHook(() => useGenerationNav());
    expect(result.current.buildDraftUrl('draft-123')).toBe(
      '/w/test-ws/generate/drafts/draft-123'
    );
  });

  it('builds run URL with slug and runId', () => {
    const { result } = renderHook(() => useGenerationNav());
    expect(result.current.buildRunUrl('run-456')).toBe(
      '/w/test-ws/generate/runs/run-456'
    );
  });

  it('builds list URL', () => {
    const { result } = renderHook(() => useGenerationNav());
    expect(result.current.buildListUrl()).toBe('/w/test-ws/generate');
  });

  it('navigateToDraft calls router.push', () => {
    const { result } = renderHook(() => useGenerationNav());
    result.current.navigateToDraft('draft-123');
    expect(mockRouter.push).toHaveBeenCalledWith(
      '/w/test-ws/generate/drafts/draft-123'
    );
  });

  it('navigateToRun calls router.push', () => {
    const { result } = renderHook(() => useGenerationNav());
    result.current.navigateToRun('run-456');
    expect(mockRouter.push).toHaveBeenCalledWith(
      '/w/test-ws/generate/runs/run-456'
    );
  });
});
