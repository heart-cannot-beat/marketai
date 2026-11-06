/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { TagReviewCard } from '@/components/external-creatives/TagReviewCard';
import {
  buildConfirmBody,
  initialChoices,
  screenFiles,
} from '@/lib/external-creatives/model';
import { creative } from './fixtures';

jest.mock('@/hooks/assets/useAssetImageUrl', () => ({
  useAssetImageUrl: (id: string | undefined) => ({
    blobUrl: id ? `blob:${id}` : null,
    isLoading: false,
    problem: undefined,
    source: 'thumbnail',
  }),
}));

function group(name: string) {
  return screen.getByRole('radiogroup', { name });
}

describe('TagReviewCard', () => {
  it('pre-selects confident suggestions only and shows the AI confidence', () => {
    render(
      <TagReviewCard
        creative={creative()}
        confirming={false}
        onConfirm={jest.fn()}
      />
    );
    const people = group('人物');
    expect(within(people).getByRole('radio', { name: '有人' })).toHaveAttribute(
      'aria-checked',
      'true'
    );
    expect(within(people).getByRole('radio', { name: '有人' })).toHaveAttribute(
      'title',
      'AI pick, 82% confident'
    );
    expect(screen.getByTestId('confidence-people')).toHaveStyle({
      width: '82%',
    });
    // 31% is shown as the AI's guess but not chosen for the user.
    const scene = group('场景');
    expect(within(scene).getByRole('radio', { name: '工业' })).toHaveAttribute(
      'aria-checked',
      'false'
    );
    expect(
      within(scene).getByRole('radio', { name: 'None of these' })
    ).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText('画面里能看到车主插枪')).toBeInTheDocument();
    expect(screen.getByText('Asset code MK-7F3K2Q')).toBeInTheDocument();
    expect(screen.getByRole('img')).toHaveAttribute('src', 'blob:av_ext_1');
  });

  it('confirms the edited tags and the chosen direction', () => {
    const onConfirm = jest.fn();
    render(
      <TagReviewCard
        creative={creative()}
        confirming={false}
        onConfirm={onConfirm}
      />
    );
    fireEvent.click(within(group('场景')).getByRole('radio', { name: '生活' }));
    fireEvent.click(
      within(group('人物')).getByRole('radio', { name: 'None of these' })
    );
    fireEvent.change(screen.getByLabelText('Direction (optional)'), {
      target: { value: 'atp_1::dir-1' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm tags' }));
    expect(onConfirm).toHaveBeenCalledWith('exc_1', {
      tags: [
        { factor_id: 'people', value_id: null },
        { factor_id: 'scene', value_id: 'v1' },
      ],
      direction: { plan_id: 'atp_1', direction_key: 'dir-1' },
      approve: true,
    });
  });

  it('starts from the confirmed tags and explains what they unlock', () => {
    render(
      <TagReviewCard
        creative={creative({
          status: 'confirmed',
          confirmation: {
            revision: 1,
            tags: [
              {
                factor_id: 'people',
                factor_label: '人物',
                value_id: 'v2',
                value_label: '无人',
                source: 'user',
              },
              {
                factor_id: 'scene',
                factor_label: '场景',
                value_id: null,
                source: 'untagged',
              },
            ],
            attributable: true,
            experiment_ready: true,
            round: 2,
            concept_count: 1,
          },
        })}
        confirming={false}
        onConfirm={jest.fn()}
      />
    );
    expect(
      within(group('人物')).getByRole('radio', { name: '无人' })
    ).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText('Confirmed')).toBeInTheDocument();
    expect(
      screen.getByText('Can enter experiments alongside generated images')
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'Delivery results will be attributed to round 2 by these values'
      )
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Update tags' })).toBeEnabled();
  });

  it('lets a failed tagging be tagged by hand and labels Mock suggestions', () => {
    const { rerender } = render(
      <TagReviewCard
        creative={creative({
          status: 'tagging_failed',
          suggestion: null,
          failure: {
            code: 'invalid_json',
            message: 'AI 返回的结果格式不正确，可以手动选择标签。',
            suggested_action: 'tag_manually',
          },
        })}
        confirming={false}
        onConfirm={jest.fn()}
      />
    );
    expect(
      screen.getByText("AI couldn't tag it; choose by hand")
    ).toBeInTheDocument();
    expect(
      within(group('人物')).getByRole('radio', { name: 'None of these' })
    ).toHaveAttribute('aria-checked', 'true');
    rerender(
      <TagReviewCard
        creative={creative({
          suggestion: { ...creative().suggestion!, execution_mode: 'mock' },
        })}
        confirming={false}
        onConfirm={jest.fn()}
      />
    );
    expect(
      screen.getByText('Demo tags (controlled Mock, no model called)')
    ).toBeInTheDocument();
    // A Mock reason is a fixed server sentence, not a reading of the image.
    expect(screen.queryByText('画面里能看到车主插枪')).not.toBeInTheDocument();
  });

  it('hides the tag form while the image is processing or was rejected', () => {
    const { rerender } = render(
      <TagReviewCard
        creative={creative({
          status: 'tagging',
          suggestion: null,
          asset_version_id: null,
        })}
        confirming={false}
        onConfirm={jest.fn()}
      />
    );
    expect(screen.getByText('AI is tagging')).toBeInTheDocument();
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument();
    rerender(
      <TagReviewCard
        creative={creative({
          status: 'rejected',
          asset_version_id: null,
          failure: {
            code: 'reference_gate_rejected',
            message: '这张图片没有通过上传检查（格式、尺寸或内容安全）。',
            suggested_action: 'upload_another',
          },
        })}
        confirming={false}
        onConfirm={jest.fn()}
      />
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      'The image did not pass the upload checks.'
    );
    expect(
      screen.queryByRole('button', { name: 'Confirm tags' })
    ).not.toBeInTheDocument();
  });

  it('asks only for a confirmation when the product has no test factors yet', () => {
    const onConfirm = jest.fn();
    render(
      <TagReviewCard
        creative={creative({
          vocabulary: { factors: [], directions: [] },
          suggestion: null,
        })}
        confirming={false}
        onConfirm={onConfirm}
      />
    );
    expect(
      screen.getByText(/This product has no test plan yet/)
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm tags' }));
    expect(onConfirm).toHaveBeenCalledWith('exc_1', {
      tags: [],
      direction: null,
      approve: true,
    });
  });
});

describe('external creative model', () => {
  it('builds the confirm body from every vocabulary factor', () => {
    const item = creative();
    expect(buildConfirmBody(item, initialChoices(item), null).tags).toEqual([
      { factor_id: 'people', value_id: 'v1' },
      { factor_id: 'scene', value_id: null },
    ]);
  });

  it('screens files by type and size before uploading', () => {
    const png = new File(['x'], 'a.png', { type: 'image/png' });
    const gif = new File(['x'], 'b.gif', { type: 'image/gif' });
    const big = new File(['x'], 'c.jpg', { type: 'image/jpeg' });
    Object.defineProperty(big, 'size', { value: 21 * 1024 * 1024 });
    const { accepted, refused } = screenFiles([png, gif, big]);
    expect(accepted).toEqual([png]);
    expect(refused).toEqual([
      { name: 'b.gif', reason: 'type' },
      { name: 'c.jpg', reason: 'size' },
    ]);
  });
});
