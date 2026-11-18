/**
 * The ten picture mechanisms a way of telling can use (design §5.7, "讲法类型"),
 * in the order the design lists them. Mirrors `app/marketing/expression_catalog.py`
 * (and `VISUAL_MECHANISMS` of the plan review, checked by a test); anything
 * else is the user's own description.
 */
export const TELLING_TYPES = [
  'scenario_story',
  'feature_closeup',
  'qa_objection',
  'side_by_side_comparison',
  'owner_testimonial',
  'certification_walkthrough',
  'live_connection_demo',
  'test_process',
  'route_map',
  'fit_guide',
] as const;
