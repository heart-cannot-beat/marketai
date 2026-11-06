import type { ExternalCreative } from '@/lib/external-creatives/model';

export function creative(
  overrides: Partial<ExternalCreative> = {}
): ExternalCreative {
  return {
    external_creative_id: 'exc_1',
    product_id: 'prod_1',
    status: 'ready',
    current_step: null,
    execution_mode: 'byok',
    filename: 'lovart-01.png',
    mime_type: 'image/png',
    byte_size: 1024,
    width: 1080,
    height: 1350,
    aspect_ratio: '4:5',
    rights: { made_by: 'designer', permission_confirmed: true },
    asset_id: 'asset_1',
    asset_version_id: 'av_ext_1',
    asset_code: 'MK-7F3K2Q',
    thumbnail: null,
    failure: null,
    vocabulary: {
      factors: [
        {
          factor_id: 'people',
          label: '人物',
          source: 'test_plan',
          values: [
            { value_id: 'v1', label: '有人' },
            { value_id: 'v2', label: '无人' },
          ],
        },
        {
          factor_id: 'scene',
          label: '场景',
          source: 'test_plan',
          values: [
            { value_id: 'v1', label: '生活' },
            { value_id: 'v2', label: '工业' },
          ],
        },
      ],
      directions: [
        {
          plan_id: 'atp_1',
          round: 1,
          direction_key: 'dir-1',
          title: '回家插上，一晚充满',
        },
      ],
    },
    suggestion: {
      factors: [
        {
          factor_id: 'people',
          value_id: 'v1',
          confidence: 0.82,
          reason: '画面里能看到车主插枪',
        },
        {
          factor_id: 'scene',
          value_id: 'v2',
          confidence: 0.31,
          reason: '背景不太清楚',
        },
      ],
      summary: '车库里的充电场景',
      provider: 'openai',
      model: 'gpt-5-mini',
      execution_mode: 'byok',
      preselect_confidence: 0.5,
    },
    confirmation: null,
    operation_id: 'op_1',
    replayed: false,
    ...overrides,
  } as ExternalCreative;
}
