/**
 * GENERATED FILE — do not edit by hand.
 * Generator: scripts/generate_response_validators.ts
 * Sources: contracts/openapi/marketai-domain-v1.json, contracts/openapi/marketai-domain-v1-policy.json
 * Regenerate with scripts/generate_all.sh — CI fails the build on drift.
 */
import Ajv2020 from 'ajv/dist/2020';
import addFormats from 'ajv-formats';
import type { ValidateFunction } from 'ajv/dist/core';
import type { DomainOperationMap } from './operation-map';

const ajv = new Ajv2020({ strict: true, allErrors: true });
// ajv-formats resolves its own nested `ajv` copy for its `Ajv` parameter
// type (a duplicate-dependency artifact of the npm tree, not a version
// mismatch our own pin controls — see package.json's "ajv": "8.17.1"). Both
// copies satisfy ajv-formats' own `^8.0.0` dependency range and are
// structurally identical at runtime; the cast only works around TypeScript
// treating the two `Ajv` classes as nominally distinct.
addFormats(ajv as unknown as Parameters<typeof addFormats>[0]);

// Registered by pointer, not compiled: makes every component schema an
// addressable `$ref` target for the validators below, whether or not it
// was itself chosen as a top-level validatorId.
const COMPONENT_SCHEMAS: Record<string, object> = {
  AIQCErrorDetail: {
    properties: {
      reason: { type: 'string' },
      field: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      problem: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      excerpt: { type: 'string' },
      sha256: { type: 'string' },
      length: { type: 'integer' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['reason', 'excerpt', 'sha256', 'length'],
    description:
      "Why a returned output was refused (#295): an `error-<reason>` code, the field path, the validator's problem and a\nbounded excerpt with the hash and length of the whole output. Never the request, its headers or a credential.",
  },
  AIQCQuote: {
    properties: {
      available: { type: 'boolean' },
      unavailable_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      provider: { type: 'string' },
      model: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      quantity: { type: 'integer' },
      maximum_cost: { type: 'string' },
      currency: { type: 'string', const: 'USD', default: 'USD' },
      quote_hash: { type: 'string' },
      asset_version_ids: { items: { type: 'string' }, type: 'array' },
      kind: {
        anyOf: [
          { type: 'string', enum: ['single', 'paired'] },
          { type: 'null' },
        ],
      },
      waiting: {
        anyOf: [
          {
            items: { $ref: '#/components/schemas/PairedQCWaiting' },
            type: 'array',
          },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'available',
      'unavailable_reason',
      'provider',
      'model',
      'execution_mode',
      'quantity',
      'maximum_cost',
      'quote_hash',
      'asset_version_ids',
    ],
  },
  ActionEligibility: {
    properties: {
      action: { type: 'string' },
      allowed: { type: 'boolean' },
      reason_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      requirements: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
  },
  ActionEligibilityModel: {
    properties: {
      action: { type: 'string' },
      allowed: { type: 'boolean' },
      reason_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      requirements: { items: { type: 'string' }, type: 'array' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['action', 'allowed'],
  },
  ActivationPreviewCreateRequest: {
    properties: { expected_revision: { type: 'integer' } },
    additionalProperties: false,
    type: 'object',
    required: ['expected_revision'],
  },
  ActivationPreviewResponse: {
    properties: {
      id: { type: 'string' },
      enrollment_id: { type: 'string' },
      meta_mode: { type: 'string' },
      scenario_scope_key: { type: 'string' },
      transition_kind: { type: 'string' },
      delivery_evidence_snapshot_id: { type: 'string' },
      lineage_guard_id: { type: 'string' },
      candidate_fingerprint: { type: 'string' },
      guard_revision: { type: 'integer' },
      lineage_hash: { type: 'string' },
      enrollment_revision: { type: 'integer' },
      expires_at: { type: 'string', format: 'date-time' },
      consumed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      created_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'enrollment_id',
      'meta_mode',
      'scenario_scope_key',
      'transition_kind',
      'delivery_evidence_snapshot_id',
      'lineage_guard_id',
      'candidate_fingerprint',
      'guard_revision',
      'lineage_hash',
      'enrollment_revision',
      'expires_at',
      'consumed_at',
      'created_at',
    ],
  },
  AddExperimentEntryRequest: {
    properties: {
      asset_version_id: { type: 'string', minLength: 1 },
      marketing_vars: { additionalProperties: true, type: 'object' },
      expected_revision: { type: 'integer', minimum: 1 },
    },
    type: 'object',
    required: ['asset_version_id', 'expected_revision'],
  },
  AddProductReference: {
    properties: {
      reference_object_id: {
        anyOf: [
          { type: 'string', maxLength: 64, minLength: 1 },
          { type: 'null' },
        ],
      },
      reference_version_id: {
        anyOf: [
          { type: 'string', maxLength: 64, minLength: 1 },
          { type: 'null' },
        ],
      },
      model_variant: { type: 'string', maxLength: 100, default: '' },
      role: {
        type: 'string',
        enum: ['primary', 'detail', 'logo', 'certification_mark'],
        default: 'detail',
      },
      identity_status: {
        type: 'string',
        enum: ['verified_physical', 'unverified'],
        default: 'unverified',
      },
      note: { type: 'string', maxLength: 1200, default: '' },
    },
    additionalProperties: false,
    type: 'object',
  },
  AdoptIsolatedOutputResult: {
    properties: {
      item_id: { type: 'string' },
      attempt_id: { type: 'string' },
      replayed: { type: 'boolean' },
      asset_version_id: { type: 'string', default: '' },
      publication_id: { type: 'string', default: '' },
      binding_id: { type: 'string', default: '' },
    },
    type: 'object',
    required: ['item_id', 'attempt_id', 'replayed'],
  },
  AdviceDispositionRequest: {
    properties: {
      kind: { type: 'string', minLength: 1 },
      note: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      occurrence_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['kind'],
    description:
      "``kind`` is validated by the domain layer\n(``app.scale.advice.create_disposition`` rejects everything but\n``acknowledged``/``dismissed`` with ``unsupported_disposition_kind``,\nmapped to 422 by ``app.scale.v1_router``'s problem handler) rather than\nre-validated here, so that single rejection reason stays authoritative.",
  },
  AdviceDispositionResponse: {
    properties: {
      id: { type: 'string' },
      thread_id: { type: 'string' },
      occurrence_id: { type: 'string' },
      kind: { type: 'string' },
      note: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      actor_id: { type: 'string' },
      created_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'thread_id',
      'occurrence_id',
      'kind',
      'note',
      'actor_id',
      'created_at',
    ],
  },
  AdviceSnoozeRequest: {
    properties: {
      until: { type: 'string', format: 'date-time' },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['until'],
  },
  AdviceSnoozeResponse: {
    properties: {
      id: { type: 'string' },
      thread_id: { type: 'string' },
      actor_id: { type: 'string' },
      started_at: { type: 'string', format: 'date-time' },
      snooze_until: { type: 'string', format: 'date-time' },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'thread_id',
      'actor_id',
      'started_at',
      'snooze_until',
      'reason',
    ],
  },
  AdviceThreadListResponse: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/AdviceThreadResponse' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['items'],
  },
  AdviceThreadResponse: {
    properties: {
      id: { type: 'string' },
      episode_id: { type: 'string' },
      enrollment_id: { type: 'string' },
      advice_type: { type: 'string' },
      reason_signature: { type: 'string' },
      severity: { type: 'string' },
      lifecycle: { type: 'string' },
      predecessor_thread_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      first_occurrence_id: { type: 'string' },
      latest_occurrence_id: { type: 'string' },
      disposition_kind: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      disposition_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      snooze_until: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      opened_at: { type: 'string', format: 'date-time' },
      resolved_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      lifecycle_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      revision: { type: 'integer' },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibilityModel' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'episode_id',
      'enrollment_id',
      'advice_type',
      'reason_signature',
      'severity',
      'lifecycle',
      'predecessor_thread_id',
      'first_occurrence_id',
      'latest_occurrence_id',
      'disposition_kind',
      'disposition_at',
      'snooze_until',
      'opened_at',
      'resolved_at',
      'lifecycle_reason',
      'revision',
      'allowed_actions',
    ],
  },
  Angle: {
    properties: {
      id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      proposition: { type: 'string', maxLength: 2000, minLength: 1 },
      rationale: { type: 'string', maxLength: 2000, minLength: 1 },
      fact_ids: {
        items: {
          type: 'string',
          maxLength: 64,
          minLength: 1,
          pattern: '^[a-zA-Z0-9_-]+$',
        },
        type: 'array',
        maxItems: 30,
      },
      inference_ids: {
        items: {
          type: 'string',
          maxLength: 64,
          minLength: 1,
          pattern: '^[a-zA-Z0-9_-]+$',
        },
        type: 'array',
        maxItems: 30,
      },
      expressions: {
        items: { $ref: '#/components/schemas/Expression' },
        type: 'array',
        maxItems: 8,
        minItems: 1,
      },
      competitor_overlap: {
        items: {
          type: 'string',
          maxLength: 64,
          minLength: 1,
          pattern: '^[a-zA-Z0-9_-]+$',
        },
        type: 'array',
        maxItems: 20,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'proposition', 'rationale', 'expressions'],
  },
  ApplyTemplateInput: {
    properties: {
      product_entry_id: { type: 'string' },
      display_name: { type: 'string', maxLength: 300, minLength: 1 },
    },
    type: 'object',
    required: ['product_entry_id', 'display_name'],
  },
  ApproveCopyInput: {
    properties: { copy_version_id: { type: 'string' } },
    type: 'object',
    required: ['copy_version_id'],
  },
  ApproveCopyResult: {
    properties: {
      batch_id: { type: 'string' },
      copy_version_id: { type: 'string' },
      approval_id: { type: 'string' },
    },
    type: 'object',
    required: ['batch_id', 'copy_version_id', 'approval_id'],
  },
  ArchiveInput: {
    properties: {
      impact_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      impact_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
  },
  AssetGalleryFacets: {
    properties: {
      providers: {
        items: { $ref: '#/components/schemas/GalleryFacetValue' },
        type: 'array',
      },
      origins: {
        items: { $ref: '#/components/schemas/GalleryFacetValue' },
        type: 'array',
      },
      modes: {
        items: { $ref: '#/components/schemas/GalleryFacetValue' },
        type: 'array',
      },
      reviews: {
        items: { $ref: '#/components/schemas/GalleryFacetValue' },
        type: 'array',
      },
      aspects: {
        items: { $ref: '#/components/schemas/GalleryFacetValue' },
        type: 'array',
      },
      qc_states: {
        items: { $ref: '#/components/schemas/GalleryFacetValue' },
        type: 'array',
      },
      tags: {
        items: { $ref: '#/components/schemas/GalleryFacetValue' },
        type: 'array',
      },
    },
    type: 'object',
    description:
      'Server-computed facets over the full matching Workspace dataset, not\njust the currently loaded page (spec §0.2). ``reviews`` (Task 5) is\ncomputed from the supersession-aware effective-review projection, with\na "no effective review" bucket surfaced as the value ``"unreviewed"``.\n``qc_states`` and ``tags`` stay empty until Task 6 lands their backing\ncolumns.',
  },
  AssetGalleryPage: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/AssetVersionSummary' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      facets: { $ref: '#/components/schemas/AssetGalleryFacets' },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['facets'],
    description:
      '``GET /generation/assets`` (Issue #49 Task 3, spec §0.2). Replaces the\ngeneric ``Page[Any]`` envelope with a typed page carrying server-computed\nfacets and collection-level ``allowed_actions`` (export eligibility for\nthe current scope).',
  },
  AssetQCAttemptSummary: {
    properties: {
      id: { type: 'string' },
      state: { type: 'string' },
      attempt_sequence: { type: 'integer' },
      feedback_round: { type: 'integer' },
      execution_mode: { type: 'string' },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      summary: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reasons: { items: { type: 'string' }, type: 'array' },
      consecutive_failures: { type: 'integer', default: 0 },
      projected_review: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      model: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      controlled_mock: { type: 'boolean', default: false },
      checks: {
        items: { $ref: '#/components/schemas/AssetQCCheckSummary' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'state',
      'attempt_sequence',
      'feedback_round',
      'execution_mode',
    ],
    description:
      'Row of ``AssetVersionDetail.qc_attempts`` -- QC attempt history\n(spec §0.2\'s "QC attempt history + latest_qc_attempt_id"). Since Issue\n#169 it carries the VLM check\'s summary, reasons and per-check verdicts,\nhow many consecutive failures this combination has had, and which human\nquality the failure projected (``needs_changes`` / ``rejected``).',
  },
  AssetQCCheckSummary: {
    properties: {
      rule_id: { type: 'string' },
      check_type: { type: 'string' },
      severity: { type: 'string' },
      outcome: { type: 'string' },
      verdict: { type: 'string' },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      fix_hint: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      must_match_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      description: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['rule_id', 'check_type', 'severity', 'outcome', 'verdict'],
    description:
      'One check of a QC attempt (Issue #169): a must-match item, product\npresence, text artifacts or reference readability, with its verdict\n(``pass``/``fail``/``uncertain``/``skip``) and the Chinese reason.',
  },
  AssetReview: {
    properties: {
      id: { type: 'string' },
      asset_version_id: { type: 'string' },
      review_value: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reason: { type: 'string', default: '' },
      supersedes_review_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      actor_id: { type: 'string' },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source: { type: 'string', default: 'human' },
    },
    type: 'object',
    required: ['id', 'asset_version_id', 'actor_id'],
    description:
      'Row of ``AssetVersionDetail.reviews`` and the create-review result.',
  },
  AssetThumbnailLink: {
    properties: {
      url: { type: 'string' },
      expires_at: { type: 'string' },
      mime_type: { type: 'string' },
      width: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      height: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['url', 'expires_at', 'mime_type'],
    description:
      "A short-lived signed GET for an AssetVersion's gallery thumbnail\n(Issue #159, ``app.generation.thumbnails``).\n\nEmbedded per item by the two list projections that render tiles\n(``AssetVersionSummary.thumbnail``, ``GenerationRunItemSummary.thumbnail``)\nso a page of tiles costs one list request plus one small GET per tile.\nIt is the 512px-long-edge derivative, never the original -- Asset\nDetail, the Lightbox and exports keep the download-intent path for\noriginals. ``null`` whenever the version has no derivative (legacy rows\nawaiting backfill, a derivative that could not be produced, a RunItem\nthat has produced nothing yet); the browser then falls back to the\noriginal through the download-intent route.",
  },
  AssetVersionDetail: {
    properties: {
      id: { type: 'string' },
      asset_id: { type: 'string' },
      product_id: { type: 'string' },
      run_id: { type: 'string' },
      provider: { type: 'string' },
      model: { type: 'string' },
      status: { type: 'string' },
      kind: { type: 'string' },
      aspect_ratio: { type: 'string' },
      stable_dimension_signature: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      tags: { additionalProperties: true, type: 'object' },
      mime_type: { type: 'string' },
      byte_size: { type: 'integer' },
      content_sha256: { type: 'string' },
      origin_kind: { type: 'string' },
      execution_mode: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      mock_evidence: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      seed: { $ref: '#/components/schemas/AssetVersionSeed' },
      prompt: { type: 'string' },
      reference_sources: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      producing_run_item_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      provider_attempt_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reviews: {
        items: { $ref: '#/components/schemas/AssetReview' },
        type: 'array',
      },
      review_head_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      effective_review_value: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      qc_attempts: {
        items: { $ref: '#/components/schemas/AssetQCAttemptSummary' },
        type: 'array',
      },
      latest_qc_attempt_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      latest_qc_state: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      contextual_conclusions: { items: {}, type: 'array' },
      parent: {
        anyOf: [
          { $ref: '#/components/schemas/AssetVersionLineageEntry' },
          { type: 'null' },
        ],
      },
      children: {
        items: { $ref: '#/components/schemas/AssetVersionLineageEntry' },
        type: 'array',
      },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'id',
      'asset_id',
      'product_id',
      'run_id',
      'provider',
      'model',
      'status',
      'kind',
      'aspect_ratio',
      'mime_type',
      'byte_size',
      'content_sha256',
      'origin_kind',
      'seed',
      'prompt',
    ],
    description:
      "``GET /generation/asset-versions/{id}``\n(``asset_queries.get_asset_version_detail``, Issue #49 Task 4, spec\n§0.2). Extends ``AssetVersionSummary``'s fields with full provenance:\nseed, prompt, append-only ``reviews[]``, QC attempt history, and\nparent/children lineage. Browser Task 8's ``LightboxModal`` consumes\nthis.\n\nRaw object keys and committed storage-version identifiers are\ndeliberately never exposed to the browser -- downloads go through a\nsigned DownloadIntent, not a raw blob reference.\n``stable_dimension_signature`` and ``mock_evidence`` stay ``None`` --\nno backing column exists yet. ``contextual_conclusions`` (Task 6, spec\n§0.7) is every non-revoked, fully-anchored\n``ContextualExperimentConclusion`` for this exact AssetVersion, each\ncarrying its own full context. ``review_head_id``/\n``effective_review_value`` apply Task 5's supersession-aware head-review\nrule (spec §0.3) -- a ``cleared`` head still sets ``review_head_id`` but\nprojects ``effective_review_value`` to ``None``.",
  },
  AssetVersionLineageEntry: {
    properties: {
      version_id: { type: 'string' },
      intent: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['version_id'],
    description:
      "One ``parent``/``children`` lineage entry (spec §0.2/§0.6): the exact\nAssetVersion id plus the regeneration intent that produced this edge --\nthe *child* version's producing ``RunItemRecord.intent_kind``, not a\nproperty of the parent. ``intent`` is ``None`` when the producing\nRunItem cannot be resolved (e.g. legacy data).",
  },
  AssetVersionSeed: {
    properties: {
      requested: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      effective: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      unavailable_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    description:
      "Requested vs. effective seed for one AssetVersion's generation call,\nplus the reason the effective seed is unavailable/differs (spec §0.2).",
  },
  AssetVersionSummary: {
    properties: {
      id: { type: 'string' },
      asset_id: { type: 'string' },
      product_id: { type: 'string' },
      run_id: { type: 'string' },
      provider: { type: 'string' },
      model: { type: 'string' },
      status: { type: 'string' },
      aspect_ratio: { type: 'string' },
      stable_dimension_signature: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      tags: { additionalProperties: true, type: 'object' },
      effective_review_value: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      latest_qc_state: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      contextual_conclusion: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      origin_kind: { type: 'string' },
      execution_mode: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      mock_evidence: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      byte_size: { type: 'integer' },
      thumbnail: {
        anyOf: [
          { $ref: '#/components/schemas/AssetThumbnailLink' },
          { type: 'null' },
        ],
      },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'id',
      'asset_id',
      'product_id',
      'run_id',
      'provider',
      'model',
      'status',
      'aspect_ratio',
      'origin_kind',
      'byte_size',
    ],
    description:
      "Row of ``GET /generation/assets`` (``asset_queries.list_asset_versions``,\nIssue #49 Task 3, spec §0.2).\n\n``id`` is the AssetVersion id -- the exact-version identity the browser\nreviews/regenerates/downloads against, matching\n``app.generation.review``'s exact-version anchor.\n\n``stable_dimension_signature`` and ``mock_evidence`` are always\n``None`` for now -- no column backs either of them yet.\n``contextual_conclusion`` (Task 6, spec §0.7) is populated only when\nthe request supplied ``experiment_context``; otherwise it stays\n``None`` -- it is never a global property of the AssetVersion.\n``effective_review_value`` applies Task 5's supersession-aware\nhead-review rule (``app.generation.review.project_effective_review``,\nspec §0.3); ``latest_qc_state`` is the latest\n``AssetQCAttemptRecord.state``. ``thumbnail`` (Issue #159) is the\nversion's signed thumbnail link, or ``None`` when it has no derivative.",
  },
  AttemptCost: {
    properties: { amount: { type: 'string' }, currency: { type: 'string' } },
    type: 'object',
    required: ['amount', 'currency'],
    description:
      'Cost sub-object inside AttemptSummary: Decimal string + currency.',
  },
  AttemptSummary: {
    properties: {
      dispatch_certainty: { type: 'string' },
      outcome: { type: 'string' },
      provider: { type: 'string' },
      model: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      version: { type: 'string' },
      scenario: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      cost: {
        anyOf: [{ $ref: '#/components/schemas/AttemptCost' }, { type: 'null' }],
      },
    },
    type: 'object',
    required: ['dispatch_certainty', 'outcome', 'provider', 'version'],
    description: 'Latest provider attempt state for a RunItem.',
  },
  AudienceCard: {
    properties: {
      who: { type: 'string', maxLength: 500, minLength: 1 },
      why: { type: 'string', maxLength: 500, default: '' },
      pain: { type: 'string', maxLength: 500, default: '' },
    },
    type: 'object',
    required: ['who'],
  },
  AudienceCardEdit: {
    properties: {
      who: { type: 'string', maxLength: 500, minLength: 1 },
      why: { type: 'string', maxLength: 500, default: '' },
      pain: { type: 'string', maxLength: 500, default: '' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['who'],
  },
  AudienceView: {
    properties: {
      text: { type: 'string' },
      card_index: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      card: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['text'],
  },
  AuthorizeIsolatedQCResult: {
    properties: {
      item_id: { type: 'string' },
      attempt_id: { type: 'string' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean' },
      authorized_steps: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
    required: ['item_id', 'attempt_id', 'replayed'],
  },
  BalanceCheck: {
    properties: {
      max_tvd: { type: 'string' },
      factor_key: { type: 'string' },
      factor_label: { type: 'string' },
    },
    type: 'object',
    required: ['max_tvd', 'factor_key', 'factor_label'],
    description:
      'Worst total-variation distance of another factor between the two\ncompared groups (>= 0.25 ``imbalanced``, >= 0.5 ``confounded``).',
  },
  BatchCommand: {
    properties: { batch_id: { type: 'string', maxLength: 64, minLength: 1 } },
    additionalProperties: false,
    type: 'object',
    required: ['batch_id'],
  },
  BatchDraftResult: {
    properties: {
      batch_id: { type: 'string' },
      phase: { type: 'string' },
      reused: { type: 'boolean' },
    },
    type: 'object',
    required: ['batch_id', 'phase', 'reused'],
  },
  BatchMarketTargetInputModel: {
    properties: {
      market_policy_version_id: { type: 'string' },
      market: { type: 'string' },
    },
    type: 'object',
    required: ['market_policy_version_id', 'market'],
  },
  BatchOutcome: {
    properties: {
      batch_id: { type: 'string' },
      phase: { type: 'string' },
      outcome: { type: 'string' },
      revision: { type: 'integer' },
      item_count: { type: 'integer' },
      item_state_counts: {
        additionalProperties: { type: 'integer' },
        type: 'object',
      },
    },
    type: 'object',
    required: ['batch_id', 'phase', 'outcome', 'revision', 'item_count'],
  },
  BatchSourceInputModel: {
    properties: {
      asset_version_id: { type: 'string' },
      decision_id: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['asset_version_id', 'decision_id'],
  },
  BetweenGroups: {
    properties: {
      kind: { type: 'string', const: 'between_groups' },
      groups: {
        items: { $ref: '#/components/schemas/BoardGroup' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['kind', 'groups'],
  },
  BindCredentialInput: {
    properties: { credential_version_id: { type: 'string' } },
    type: 'object',
    required: ['credential_version_id'],
  },
  BindCredentialRequest: {
    properties: { provider: { type: 'string' } },
    additionalProperties: false,
    type: 'object',
    required: ['provider'],
  },
  BindDelivery: {
    properties: {
      entry_id: { type: 'string', maxLength: 64, minLength: 1 },
      ad_account_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^(act_)?[0-9]+$',
      },
      ad_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[0-9]+$',
      },
      conditions_match: { type: 'boolean' },
      deviations: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['entry_id', 'ad_account_id', 'ad_id', 'conditions_match'],
  },
  BindExperimentCandidateRequest: {
    properties: {
      ad_account_id: { type: 'string', minLength: 1 },
      ad_id: { type: 'string', minLength: 1 },
      source: { type: 'string', default: 'manual_read_only' },
      expected_revision: { type: 'integer', minimum: 1 },
    },
    type: 'object',
    required: ['ad_account_id', 'ad_id', 'expected_revision'],
  },
  BindInput: {
    properties: {
      reference_version_id: { type: 'string' },
      purpose: { type: 'string', maxLength: 100, minLength: 1 },
      viewpoint: { type: 'string', maxLength: 100, minLength: 1 },
      priority: { type: 'integer', minimum: 0, default: 0 },
      note: { type: 'string', default: '' },
      conditions: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['reference_version_id', 'purpose', 'viewpoint'],
  },
  BoardAIQC: {
    properties: {
      id: { type: 'string' },
      state: { type: 'string' },
      current: { type: 'boolean' },
      quality_suggestion: {
        anyOf: [
          {
            type: 'string',
            enum: ['needs_changes', 'needs_confirmation', 'ready_to_confirm'],
          },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'state', 'current', 'quality_suggestion'],
  },
  BoardAngle: {
    properties: {
      angle_id: { type: 'string' },
      proposition: { type: 'string' },
      group_ids: { items: { type: 'string' }, type: 'array' },
      expressions: {
        items: { $ref: '#/components/schemas/BoardExpression' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['angle_id', 'proposition', 'group_ids', 'expressions'],
  },
  BoardAppearanceSummary: {
    properties: {
      status: {
        type: 'string',
        enum: ['reference_backed', 'partial', 'pending_verification'],
      },
      reference_version_ids: { items: { type: 'string' }, type: 'array' },
      pending_combination_ids: { items: { type: 'string' }, type: 'array' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['status', 'reference_version_ids', 'pending_combination_ids'],
    description:
      'Round-level product appearance; `pending_combination_ids` lists combinations with a pending execution.',
  },
  BoardCombination: {
    properties: {
      combination_id: { type: 'string' },
      group_id: { type: 'string' },
      group_name: { type: 'string' },
      headline: { type: 'string' },
      planned_executions: {
        anyOf: [{ type: 'integer', minimum: 1 }, { type: 'null' }],
      },
      executions: {
        items: { $ref: '#/components/schemas/BoardExecution' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'combination_id',
      'group_id',
      'group_name',
      'headline',
      'executions',
    ],
  },
  BoardDelivery: {
    properties: {
      entry_id: { type: 'string' },
      ad_ids: { items: { type: 'string' }, type: 'array' },
      has_data: { type: 'boolean' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['entry_id', 'ad_ids', 'has_data'],
  },
  BoardExecution: {
    properties: {
      asset_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      execution_index: { type: 'integer', minimum: 1 },
      execution_version: {
        anyOf: [{ type: 'integer', minimum: 1 }, { type: 'null' }],
      },
      parent_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      duplicate_of: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      angle_id: { type: 'string' },
      expression_id: { type: 'string' },
      combination_id: { type: 'string' },
      group_id: { type: 'string' },
      stage: {
        type: 'string',
        enum: [
          'pending_generation',
          'generating',
          'single_qc_pending',
          'single_qc_running',
          'single_qc_done',
          'paired_qc_pending',
          'paired_qc_running',
          'paired_qc_done',
          'ready',
          'exported',
          'bound',
          'has_data',
          'blocked',
        ],
      },
      blocked_reason: {
        anyOf: [
          {
            type: 'string',
            enum: [
              'quality_changes_requested',
              'file_qc_failed',
              'reference_not_current',
              'review_withdrawn',
            ],
          },
          { type: 'null' },
        ],
      },
      quality: {
        anyOf: [
          { type: 'string', enum: ['approved', 'needs_changes', 'rejected'] },
          { type: 'null' },
        ],
      },
      ai_qc: {
        anyOf: [{ $ref: '#/components/schemas/BoardAIQC' }, { type: 'null' }],
      },
      paired_ai_qc: {
        anyOf: [{ $ref: '#/components/schemas/BoardAIQC' }, { type: 'null' }],
      },
      delivery: {
        anyOf: [
          { $ref: '#/components/schemas/BoardDelivery' },
          { type: 'null' },
        ],
      },
      product_appearance: {
        $ref: '#/components/schemas/BoardProductAppearance',
      },
      compare_with: { items: { type: 'string' }, type: 'array' },
      awaiting_combination_ids: { items: { type: 'string' }, type: 'array' },
      gap: {
        type: 'string',
        enum: [
          'pending_generation',
          'single_qc',
          'paired_qc',
          'review',
          'export',
          'bind',
          'data',
          'none',
        ],
      },
      next_action: {
        type: 'string',
        enum: [
          'generate',
          'single_qc',
          'paired_qc',
          'ai_qc',
          'review',
          'revise',
          'export',
          'bind',
          'import',
          'evaluate',
          'none',
        ],
      },
      thumbnail: {
        anyOf: [
          { $ref: '#/components/schemas/AssetThumbnailLink' },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'asset_version_id',
      'execution_index',
      'parent_version_id',
      'duplicate_of',
      'angle_id',
      'expression_id',
      'combination_id',
      'group_id',
      'stage',
      'blocked_reason',
      'quality',
      'ai_qc',
      'delivery',
      'product_appearance',
      'compare_with',
      'awaiting_combination_ids',
      'gap',
      'next_action',
    ],
    description:
      "One image of a combination, or an ungenerated placeholder without an AssetVersion ID.\n\n`thumbnail` (include_board_thumbnails only) is the version's signed 512px derivative for the tile; null for\nplaceholders and for versions without one, where the tile falls back to the original through the download intent.",
  },
  BoardExpression: {
    properties: {
      expression_id: { type: 'string' },
      name: { type: 'string' },
      visual_mechanism: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      details_missing: { type: 'boolean' },
      shared: { type: 'boolean' },
      combinations: {
        items: { $ref: '#/components/schemas/BoardCombination' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'expression_id',
      'name',
      'visual_mechanism',
      'details_missing',
      'shared',
      'combinations',
    ],
  },
  BoardGroup: {
    properties: {
      id: { type: 'string' },
      name: { type: 'string' },
      angle_ids: { items: { type: 'string' }, type: 'array' },
      combination_ids: { items: { type: 'string' }, type: 'array' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'name', 'angle_ids', 'combination_ids'],
  },
  BoardMatrix: {
    properties: {
      rows: {
        items: { $ref: '#/components/schemas/MatrixRow' },
        type: 'array',
      },
      cols: {
        items: { $ref: '#/components/schemas/MatrixCol' },
        type: 'array',
      },
      cells: {
        items: {
          items: {
            anyOf: [
              { items: { type: 'string' }, type: 'array' },
              { type: 'null' },
            ],
          },
          type: 'array',
        },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['rows', 'cols', 'cells'],
  },
  BoardProductAppearance: {
    properties: {
      status: {
        type: 'string',
        enum: ['reference_backed', 'pending_verification'],
      },
      reason: {
        anyOf: [
          { type: 'string', enum: ['no_reference', 'reference_not_current'] },
          { type: 'null' },
        ],
      },
      reference_version_ids: { items: { type: 'string' }, type: 'array' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['status', 'reason', 'reference_version_ids'],
    description:
      "Whether this execution's visible product appearance can be checked against frozen, current verified photos.\n\n`pending_verification` (外观待核实) keeps product_fidelity unknown. A photo or its verified status is never\nevidence for certification, hidden performance or ad effect.",
  },
  BoardTree: {
    properties: {
      angles: {
        items: { $ref: '#/components/schemas/BoardAngle' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['angles'],
  },
  BoundImage: {
    properties: {
      image_id: { type: 'string' },
      reference_object_id: { type: 'string' },
      reference_entry_id: { type: 'string' },
      reference_version_id: { type: 'string' },
      viewpoint: { type: 'string' },
    },
    type: 'object',
    required: [
      'image_id',
      'reference_object_id',
      'reference_entry_id',
      'reference_version_id',
      'viewpoint',
    ],
  },
  BrandFonts: {
    properties: {
      primary: { type: 'string', maxLength: 60, minLength: 1 },
      secondary: { type: 'string', maxLength: 60, default: '' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['primary'],
  },
  BrandKitContent: {
    properties: {
      logo: {
        anyOf: [{ $ref: '#/components/schemas/BrandLogo' }, { type: 'null' }],
      },
      fonts: { $ref: '#/components/schemas/BrandFonts' },
      palette: {
        items: { type: 'string', pattern: '^#[0-9A-Fa-f]{6}$' },
        type: 'array',
        maxItems: 5,
        minItems: 1,
      },
      readability_rules: { $ref: '#/components/schemas/ReadabilityRules' },
      forbidden: {
        items: { type: 'string', maxLength: 120, minLength: 1 },
        type: 'array',
        maxItems: 8,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['fonts', 'palette'],
    description:
      'The immutable stored version: the logo is resolved to its exact reference identity.',
  },
  BrandKitInput: {
    properties: {
      logo: {
        anyOf: [
          { $ref: '#/components/schemas/BrandLogoInput' },
          { type: 'null' },
        ],
      },
      fonts: { $ref: '#/components/schemas/BrandFonts' },
      palette: {
        items: { type: 'string', pattern: '^#[0-9A-Fa-f]{6}$' },
        type: 'array',
        maxItems: 5,
        minItems: 1,
      },
      readability_rules: { $ref: '#/components/schemas/ReadabilityRules' },
      forbidden: {
        items: { type: 'string', maxLength: 120, minLength: 1 },
        type: 'array',
        maxItems: 8,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['fonts', 'palette'],
  },
  BrandKitList: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/BrandKitSummary' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['items'],
  },
  BrandKitRef: {
    properties: {
      id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      product_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      version: { type: 'integer', minimum: 1 },
      content_hash: { type: 'string', pattern: '^[0-9a-f]{64}$' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'product_id', 'version', 'content_hash'],
    description:
      'An exact, immutable brand baseline version that a plan and its recipes freeze.',
  },
  BrandKitSummary: {
    properties: {
      product_id: { type: 'string' },
      product_name: { type: 'string' },
      current: { $ref: '#/components/schemas/BrandKitVersion' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id', 'product_name', 'current'],
  },
  BrandKitVersion: {
    properties: {
      id: { type: 'string' },
      product_id: { type: 'string' },
      version: { type: 'integer' },
      content_hash: { type: 'string' },
      content: { $ref: '#/components/schemas/BrandKitContent' },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { type: 'string' },
      logo_available: { type: 'boolean' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'product_id',
      'version',
      'content_hash',
      'content',
      'created_at',
      'logo_available',
    ],
  },
  BrandLogo: {
    properties: {
      item_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      placement: {
        type: 'string',
        enum: [
          'top_left',
          'top_right',
          'top_center',
          'bottom_left',
          'bottom_right',
          'bottom_center',
        ],
        default: 'top_left',
      },
      reference_version_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      reference_object_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      content_sha256: { type: 'string', pattern: '^[0-9a-f]{64}$' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'item_id',
      'reference_version_id',
      'reference_object_id',
      'content_sha256',
    ],
  },
  BrandLogoInput: {
    properties: {
      item_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      placement: {
        type: 'string',
        enum: [
          'top_left',
          'top_right',
          'top_center',
          'bottom_left',
          'bottom_right',
          'bottom_center',
        ],
        default: 'top_left',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['item_id'],
    description:
      'A product reference item the product settings marked role=logo (#226).',
  },
  BreakdownResponse: {
    properties: {
      snapshot_id: { type: 'string' },
      episode_id: { type: 'string' },
      breakdown_type: { type: 'string' },
      window_start: { type: 'string', format: 'date' },
      window_end: { type: 'string', format: 'date' },
      safe_watermark: { type: 'string', format: 'date' },
      meta_mode: { type: 'string' },
      policy_version_id: { type: 'string' },
      target_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      segments: {
        items: { $ref: '#/components/schemas/BreakdownSegmentResponse' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'snapshot_id',
      'episode_id',
      'breakdown_type',
      'window_start',
      'window_end',
      'safe_watermark',
      'meta_mode',
      'policy_version_id',
      'target_version_id',
      'segments',
    ],
  },
  BreakdownSegmentResponse: {
    properties: {
      breakdown_type: { type: 'string' },
      segment_key: { type: 'string' },
      state: { type: 'string' },
      suppression_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      spend_share: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      impressions: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      link_clicks: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      purchases: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      purchase_value: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      cpa: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      roas: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      insufficient_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['breakdown_type', 'segment_key', 'state'],
  },
  BrushPoint: {
    properties: {
      x: { type: 'number', maximum: 1, minimum: 0 },
      y: { type: 'number', maximum: 1, minimum: 0 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['x', 'y'],
  },
  CancelResult: {
    properties: {
      batch_id: { type: 'string' },
      cancelled_item_ids: { items: { type: 'string' }, type: 'array' },
      already_terminal: { type: 'boolean' },
    },
    type: 'object',
    required: ['batch_id', 'already_terminal'],
  },
  CandidateCreateRequest: {
    properties: {
      winner_decision_id: { type: 'string', minLength: 1 },
      delivery_target_id: { type: 'string', minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['winner_decision_id', 'delivery_target_id'],
    description:
      'Wire shape for creating one short-lived Enrollment Candidate (spec\n§4.4). ``winner_decision_id``/``delivery_target_id`` are still raw\nidentifiers here -- that is expected: this is the one endpoint whose\nentire job is proving a caller-named pair is currently a valid lineage,\nbefore handing back the opaque, narrow-scoped candidate id every other\nScale write actually consumes.',
  },
  CandidateInput: {
    properties: {
      experiment_version_candidate_id: { type: 'string' },
      asset_version_id: { type: 'string' },
      creative_config: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['experiment_version_candidate_id', 'asset_version_id'],
  },
  CandidateResponse: {
    properties: {
      id: { type: 'string' },
      meta_mode: { type: 'string' },
      scenario_scope_key: { type: 'string' },
      winner_decision_id: { type: 'string' },
      delivery_target_id: { type: 'string' },
      candidate_fingerprint: { type: 'string' },
      expires_at: { type: 'string', format: 'date-time' },
      consumed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      created_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'meta_mode',
      'scenario_scope_key',
      'winner_decision_id',
      'delivery_target_id',
      'candidate_fingerprint',
      'expires_at',
      'consumed_at',
      'created_at',
    ],
  },
  CapabilityDecision: {
    properties: {
      capability: { type: 'string' },
      supported: { type: 'boolean' },
      detail: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['capability', 'supported'],
    description: 'Provider capability resolution result.',
  },
  CheckOverride: {
    properties: {
      ai: { type: 'string', enum: ['pass', 'fail', 'unknown'] },
      human: { type: 'string', enum: ['pass', 'fail', 'unknown'] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['ai', 'human'],
  },
  ClaimDecisionInputModel: {
    properties: {
      claim_index: { type: 'integer' },
      action: { type: 'string', enum: ['replace', 'remove', 'preserve'] },
      replacement_fact_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      removal_strategy: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      rationale: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['claim_index', 'action'],
  },
  CloneProductInput: {
    properties: {
      display_name: { type: 'string', maxLength: 300, minLength: 1 },
      include_optional: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
    required: ['display_name'],
  },
  CloseRound: {
    properties: {
      reason: {
        type: 'string',
        enum: [
          'completed',
          'asset_only',
          'no_qualified',
          'insufficient_evidence',
          'user_stopped',
        ],
      },
      note: { type: 'string', maxLength: 2000, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['reason', 'note'],
  },
  CommandImpactProjection: {
    properties: {
      impact_id: { type: 'string' },
      impact_hash: { type: 'string' },
      command_kind: { type: 'string' },
      target_entry_id: { type: 'string' },
      target_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      target_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      content_hash: { type: 'string', default: '' },
      expires_at: { type: 'string' },
      projection: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: [
      'impact_id',
      'impact_hash',
      'command_kind',
      'target_entry_id',
      'expires_at',
    ],
    description:
      'Response from ``POST .../publish-impact``, ``archive-impact``, ``withdraw-impact``.\n\nThe browser presents this to the user, then echoes ``impact_id`` +\n``impact_hash`` back to the matching command for atomic verification.',
  },
  CommandOutcomeResponse: {
    properties: {
      identity_hash: { type: 'string' },
      command_type: { type: 'string' },
      dedup_key: { type: 'string' },
      result_resource_id: { type: 'string' },
      result_resource_type: { type: 'string' },
      request_fingerprint: { type: 'string' },
      initiator_id: { type: 'string' },
      created_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'identity_hash',
      'command_type',
      'dedup_key',
      'result_resource_id',
      'result_resource_type',
      'request_fingerprint',
      'initiator_id',
      'created_at',
    ],
  },
  Competitor: {
    properties: {
      competitor_id: { type: 'string' },
      product_id: { type: 'string' },
      role: { type: 'string', enum: ['competitor', 'self'] },
      status: { type: 'string', enum: ['active', 'archived'] },
      name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      website_domain: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      input_kind: {
        type: 'string',
        enum: ['name', 'facebook_page', 'ad_library'],
      },
      input_page_alias: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confirmation_status: {
        type: 'string',
        enum: ['confirmed', 'unconfirmed'],
      },
      confirmation: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorConfirmation' },
          { type: 'null' },
        ],
      },
      observed_page_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      last_scanned_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      latest_resolution: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorResolutionSummary' },
          { type: 'null' },
        ],
      },
      can_resolve: { type: 'boolean', default: false },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      archived_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      archived_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'competitor_id',
      'product_id',
      'role',
      'status',
      'input_kind',
      'confirmation_status',
    ],
    description:
      "A competitor of a product (or ``role=self``, the product's own page).\n``input_kind`` is how it was added; ``can_resolve`` says whether a\nname search is possible (a name, or the alias of a pasted page link).\n\nTwo read-only projections from the product's scans, neither a call nor\na change to the confirmation: ``observed_page_name`` is the advertiser\nname of the confirmed page's newest observation (a link confirms a page\nwithout its name; null until a scan sees the page), and\n``last_scanned_at`` is when the newest finished scan (``completed``,\n``partial``, ``failed`` or ``uncertain``) that included this competitor\nfinished (null when none has).",
  },
  CompetitorAd: {
    properties: {
      ad_id: { type: 'string' },
      scan_id: { type: 'string' },
      provider: { type: 'string' },
      ad_archive_id: { type: 'string' },
      collation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      collation_count: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      advertiser_page_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      advertiser_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      page_profile_uri: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      page_categories: { items: { type: 'string' }, type: 'array' },
      is_active: { anyOf: [{ type: 'boolean' }, { type: 'null' }] },
      start_date: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      end_date: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      days_running: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      display_format: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      publisher_platforms: { items: { type: 'string' }, type: 'array' },
      cards: {
        items: { $ref: '#/components/schemas/CompetitorAdCard' },
        type: 'array',
      },
      landing_domain: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      library_url: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      observed_at: { type: 'string' },
      origins: {
        items: { $ref: '#/components/schemas/CompetitorAdOrigin' },
        type: 'array',
      },
      competitor_ids: { items: { type: 'string' }, type: 'array' },
      role: {
        anyOf: [
          { type: 'string', enum: ['competitor', 'self'] },
          { type: 'null' },
        ],
      },
      media_status: {
        type: 'string',
        enum: ['none', 'pending', 'available', 'unavailable'],
      },
      thumbnail: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdThumbnail' },
          { type: 'null' },
        ],
      },
      field_sources: {
        additionalProperties: { type: 'string' },
        type: 'object',
      },
      relevance: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdRelevance' },
          { type: 'null' },
        ],
      },
      tags: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdTags' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'ad_id',
      'scan_id',
      'provider',
      'ad_archive_id',
      'observed_at',
      'media_status',
    ],
    description:
      "One observation (spec §5.3): what was seen in the Meta Ad Library,\nnever what works (ADR 0014). ``competitor_ids`` are the competitors whose\npage it was fetched from (projected from its ``page:`` origins);\n``role`` is ``self`` when the ad is the product's own page's: fetched\nfrom that page, or its advertiser page is the page a ``role=self``\ncompetitor of this scan confirmed.\n``field_sources`` names the provider field each value came from.\n``relevance`` is null until the filter step (or a member) judged it;\n``tags`` until the tag step (or a member) tagged it.",
  },
  CompetitorAdAssessment: {
    properties: {
      assessment_id: { type: 'string' },
      ad_id: { type: 'string' },
      scan_id: { type: 'string' },
      kind: { type: 'string', const: 'relevance' },
      revision: { type: 'integer' },
      source: { type: 'string', enum: ['ai', 'rule', 'user'] },
      relevance: {
        type: 'string',
        enum: ['relevant', 'adjacent', 'unrelated'],
      },
      confidence: { anyOf: [{ type: 'number' }, { type: 'null' }] },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      language: {
        anyOf: [{ type: 'string', enum: ['en', 'zh-CN'] }, { type: 'null' }],
      },
      rule: {
        anyOf: [
          {
            type: 'string',
            enum: ['personal_account', 'wholesale_site', 'dealer'],
          },
          { type: 'null' },
        ],
      },
      previous: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdAssessmentPrevious' },
          { type: 'null' },
        ],
      },
      evidence: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdAssessmentEvidence' },
          { type: 'null' },
        ],
      },
      attempt_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'assessment_id',
      'ad_id',
      'scan_id',
      'kind',
      'revision',
      'source',
      'relevance',
    ],
    description:
      "One append-only revision of an observation's relevance. ``created_by``\nis the member who flipped it (null for the scan's own judgments);\n``previous`` is what a flip replaced.",
  },
  CompetitorAdAssessmentEvidence: {
    properties: {
      job_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      call_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      execution_mode: {
        anyOf: [{ type: 'string', enum: ['byok', 'mock'] }, { type: 'null' }],
      },
      provider: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      model: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      model_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      prompt_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    description:
      'Which ``llm_chat`` call answered an ``ai`` revision (its batch is ``call_name``).',
  },
  CompetitorAdAssessmentList: {
    properties: {
      scan_id: { type: 'string' },
      ad_id: { type: 'string' },
      kind: { type: 'string', const: 'relevance' },
      items: {
        items: { $ref: '#/components/schemas/CompetitorAdAssessment' },
        type: 'array',
      },
      current: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdAssessment' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['scan_id', 'ad_id', 'kind'],
    description:
      "An observation's relevance revisions, oldest first; ``current`` is the one in effect.",
  },
  CompetitorAdAssessmentPrevious: {
    properties: {
      revision: { type: 'integer' },
      relevance: {
        anyOf: [
          { type: 'string', enum: ['relevant', 'adjacent', 'unrelated'] },
          { type: 'null' },
        ],
      },
      source: { type: 'string', enum: ['ai', 'rule', 'user'] },
    },
    type: 'object',
    required: ['revision', 'source'],
    description: 'The revision a flip replaced.',
  },
  CompetitorAdCard: {
    properties: {
      index: { type: 'integer' },
      title: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      body: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      cta_text: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      cta_type: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      link_url: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      link_description: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      image: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdMedia' },
          { type: 'null' },
        ],
      },
      video_poster: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdMedia' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['index'],
    description:
      "Card 0 is the snapshot's own creative; ``snapshot.cards[i]`` is card ``i + 1``.",
  },
  CompetitorAdMedia: {
    properties: {
      kind: { type: 'string', enum: ['image', 'video_poster'] },
      card_index: { type: 'integer' },
      source_field: { type: 'string' },
      source_url: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      expires_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: { type: 'string', enum: ['pending', 'available', 'unavailable'] },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      sha256: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      byte_size: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      mime_type: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      width: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      height: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      fetched_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      refreshed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['kind', 'card_index', 'source_field', 'status'],
    description:
      'One archived image or video poster (video files are never archived).\n``source_url`` is the provider link without its signature; ``expires_at``\nis when the signed link stopped working (``oe=``).',
  },
  CompetitorAdOrigin: {
    properties: {
      origin: { type: 'string' },
      kind: { type: 'string', enum: ['page', 'keyword'] },
      competitor_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      keyword: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      attempt_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      observed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['origin', 'kind'],
    description:
      "A path the observation was found by: a competitor's page or a keyword.",
  },
  CompetitorAdPage: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/CompetitorAd' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      total: { type: 'integer', default: 0 },
    },
    type: 'object',
    description:
      'One page of observations, longest-running first; ``total`` counts every match.',
  },
  CompetitorAdRelevance: {
    properties: {
      relevance: {
        type: 'string',
        enum: ['relevant', 'adjacent', 'unrelated'],
      },
      source: { type: 'string', enum: ['ai', 'rule', 'user'] },
      revision: { type: 'integer' },
      confidence: { anyOf: [{ type: 'number' }, { type: 'null' }] },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      rule: {
        anyOf: [
          {
            type: 'string',
            enum: ['personal_account', 'wholesale_site', 'dealer'],
          },
          { type: 'null' },
        ],
      },
      assessed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['relevance', 'source', 'revision'],
    description:
      "The relevance revision in effect (spec §7.1), AI inference until a\nmember flips it (ADR 0014). ``source``: ``ai`` (the scan's filter step;\n``confidence`` and a one-sentence ``reason`` in the scan's language),\n``rule`` (a keyword-only observation the §7.1 rules judged ``unrelated``\nwithout the LLM; ``rule`` names it) or ``user`` (a flip).",
  },
  CompetitorAdRelevanceFlip: {
    properties: {
      relevance: {
        type: 'string',
        enum: ['relevant', 'adjacent', 'unrelated'],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['relevance'],
    description:
      "A member's verdict on one observation; flipping back is one more revision.",
  },
  CompetitorAdTagAssessment: {
    properties: {
      assessment_id: { type: 'string' },
      ad_id: { type: 'string' },
      scan_id: { type: 'string' },
      kind: { type: 'string', const: 'tags' },
      revision: { type: 'integer' },
      source: { type: 'string', enum: ['ai', 'rule', 'user'] },
      ai_suggestion: { type: 'boolean' },
      factors: {
        items: { $ref: '#/components/schemas/CompetitorAdTagFactor' },
        type: 'array',
      },
      summary: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      language: {
        anyOf: [{ type: 'string', enum: ['en', 'zh-CN'] }, { type: 'null' }],
      },
      vocabulary_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      images: {
        anyOf: [
          {
            items: { $ref: '#/components/schemas/CompetitorAdTagImage' },
            type: 'array',
          },
          { type: 'null' },
        ],
      },
      previous: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdTagPrevious' },
          { type: 'null' },
        ],
      },
      evidence: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdAssessmentEvidence' },
          { type: 'null' },
        ],
      },
      attempt_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'assessment_id',
      'ad_id',
      'scan_id',
      'kind',
      'revision',
      'source',
      'ai_suggestion',
    ],
    description:
      "One append-only tags revision: the AI's suggestion (``ai_suggestion``;\nper factor a value, confidence and reason in the scan's language, a\none-sentence ``summary`` and the card images it saw) or a member's\nconfirmation (``created_by``, ``previous``).",
  },
  CompetitorAdTagFactor: {
    properties: {
      factor_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      factor_label: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      value_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      value_label: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confidence: { anyOf: [{ type: 'number' }, { type: 'null' }] },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
  },
  CompetitorAdTagImage: {
    properties: {
      card_index: { type: 'integer' },
      derivative: { type: 'string', enum: ['thumbnail', 'original'] },
    },
    type: 'object',
    required: ['card_index', 'derivative'],
  },
  CompetitorAdTagList: {
    properties: {
      scan_id: { type: 'string' },
      ad_id: { type: 'string' },
      kind: { type: 'string', const: 'tags' },
      vocabulary: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorTagVocabulary' },
          { type: 'null' },
        ],
      },
      items: {
        items: { $ref: '#/components/schemas/CompetitorAdTagAssessment' },
        type: 'array',
      },
      current: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdTagAssessment' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['scan_id', 'ad_id', 'kind'],
    description:
      "An observation's tags revisions, oldest first; ``current`` is the one\nin effect. ``vocabulary`` is null until the tag step froze one.",
  },
  CompetitorAdTagPrevious: {
    properties: {
      revision: { type: 'integer' },
      source: { type: 'string', enum: ['ai', 'rule', 'user'] },
    },
    type: 'object',
    required: ['revision', 'source'],
  },
  CompetitorAdTagValue: {
    properties: {
      factor_id: { type: 'string' },
      value_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confidence: { anyOf: [{ type: 'number' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['factor_id'],
  },
  CompetitorAdTags: {
    properties: {
      source: { type: 'string', enum: ['ai', 'rule', 'user'] },
      revision: { type: 'integer' },
      ai_suggestion: { type: 'boolean' },
      factors: {
        items: { $ref: '#/components/schemas/CompetitorAdTagValue' },
        type: 'array',
      },
      summary: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      assessed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['source', 'revision', 'ai_suggestion'],
    description:
      'The tags revision in effect: the scan\'s AI suggestion\n(``ai_suggestion``, ``source=ai``) until a member confirms or changes it\n(``source=user``). ``value_id`` null is "none of these".',
  },
  CompetitorAdTagsConfirm: {
    properties: {
      tags: {
        items: { $ref: '#/components/schemas/CompetitorTagInput' },
        type: 'array',
        maxItems: 24,
      },
    },
    additionalProperties: false,
    type: 'object',
    description:
      "A member's tags for one observation, against the scan's frozen\nvocabulary: one value per factor (``value_id`` null or ``none`` = none of\nthese); factors left out are \"none\". Confirming the AI's values as they\nare is a revision too.",
  },
  CompetitorAdThumbnail: {
    properties: {
      url: { type: 'string' },
      expires_at: { type: 'string' },
      mime_type: { type: 'string' },
      width: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      height: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['url', 'expires_at', 'mime_type'],
    description:
      'A short-lived signed GET of the 512px derivative (not downloadable as a reference).',
  },
  CompetitorCandidate: {
    properties: {
      page_id: { type: 'string' },
      page_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      page_alias: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      category: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      likes: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      ig_username: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ig_followers: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      verification: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      page_is_deleted: { anyOf: [{ type: 'boolean' }, { type: 'null' }] },
      avatar: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorCandidateAvatar' },
          { type: 'null' },
        ],
      },
      provider_rank: { type: 'integer' },
      rank: { type: 'integer' },
      name_match: { type: 'string', enum: ['exact', 'partial', 'none'] },
      alias_match: { type: 'boolean', default: false },
      category_class: {
        type: 'string',
        enum: ['commercial', 'other', 'excluded'],
      },
      excluded_reason: {
        anyOf: [
          { type: 'string', enum: ['deleted', 'category'] },
          { type: 'null' },
        ],
      },
      probe: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorPageProbe' },
          { type: 'null' },
        ],
      },
      domain_hit: { anyOf: [{ type: 'boolean' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'page_id',
      'provider_rank',
      'rank',
      'name_match',
      'category_class',
    ],
    description:
      "A page a name search returned, scored by spec §4.2.\n\n``rank`` is the order after scoring and the domain probe (0 first);\n``provider_rank`` is the provider's order. ``name_match`` compares the\nnormalized names (case, spaces and punctuation ignored); ``alias_match``\ncompares with the alias of a pasted ``facebook.com/<alias>`` link.\n``domain_hit`` is null when no official site was given or the page was\nnot probed. ``avatar`` is the copy the resolve downloaded (free, from\nMeta's CDN only), never the provider's signed link; it is served only\nwhile the resolve is running or reusable and never on a confirmation.\nIt is null when the page has no avatar or the download failed: the\ncard shows a placeholder.",
  },
  CompetitorCandidateAvatar: {
    properties: {
      url: { type: 'string' },
      expires_at: { type: 'string' },
      mime_type: { type: 'string' },
      width: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      height: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['url', 'expires_at', 'mime_type'],
    description:
      "A short-lived signed GET of the resolve's archived copy of a page's\navatar (its 512px derivative), read like a board thumbnail.",
  },
  CompetitorClaimCluster: {
    properties: {
      cluster_id: { type: 'string' },
      claim: { type: 'string' },
      ad_ids: { items: { type: 'string' }, type: 'array' },
      observation_count: { type: 'integer', default: 0 },
      advertisers: {
        items: { $ref: '#/components/schemas/CompetitorClusterAdvertiser' },
        type: 'array',
      },
      advertiser_count: { type: 'integer', default: 0 },
      self_included: { type: 'boolean', default: false },
      max_days_running: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      library_urls: { items: { type: 'string' }, type: 'array' },
      ai_inference: { type: 'boolean', default: true },
      ads: {
        items: { $ref: '#/components/schemas/CompetitorSnapshotAd' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['cluster_id', 'claim'],
    description:
      'One buying claim (AI inference, ``ai_inference``): its members as of\nthe snapshot (observations no longer relevant left it), the advertisers\n(``role=self`` marked; ``advertiser_count`` leaves them out) and the\nlongest days running.',
  },
  CompetitorClusterAdvertiser: {
    properties: {
      advertiser_page_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      advertiser_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      role: { type: 'string', enum: ['competitor', 'self'] },
    },
    type: 'object',
    required: ['role'],
  },
  CompetitorConfirmRequest: {
    properties: {
      resolve_job_id: {
        anyOf: [
          { type: 'string', maxLength: 64, minLength: 1 },
          { type: 'null' },
        ],
      },
      page_id: {
        anyOf: [
          { type: 'string', maxLength: 32, minLength: 1 },
          { type: 'null' },
        ],
      },
      page_url: {
        anyOf: [
          { type: 'string', maxLength: 2048, minLength: 1 },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    description:
      'Either a candidate of a reusable resolve (``resolve_job_id`` +\n``page_id``) or a page link carrying a page id (``page_url``: a\n``profile.php?id=`` link or an Ad Library ``view_all_page_id`` link).\nConfirming the page that is already confirmed from the same source\nreturns that revision instead of appending another.',
  },
  CompetitorConfirmation: {
    properties: {
      revision: { type: 'integer' },
      platform: { type: 'string', const: 'meta', default: 'meta' },
      page_id: { type: 'string' },
      page_alias: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      page_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source: { type: 'string', enum: ['link', 'auto', 'user'] },
      link_kind: {
        anyOf: [
          { type: 'string', enum: ['facebook_page', 'ad_library'] },
          { type: 'null' },
        ],
      },
      resolve_job_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      domain_hit: { anyOf: [{ type: 'boolean' }, { type: 'null' }] },
      candidate: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorCandidate' },
          { type: 'null' },
        ],
      },
      confirmed_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confirmed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['revision', 'page_id', 'source'],
    description:
      "The latest confirmed page: an append-only revision. ``source`` is\n``link`` (a pasted link with a page id), ``auto`` (the resolve matched\nby exact name and domain, or by the pasted alias) or ``user``.\n``candidate`` is the resolve's card for that page as it was confirmed\n(without the avatar).",
  },
  CompetitorCreateRequest: {
    properties: {
      product_id: { type: 'string', maxLength: 64, minLength: 1 },
      input: { type: 'string', maxLength: 2048, minLength: 1 },
      website: {
        anyOf: [{ type: 'string', maxLength: 2048 }, { type: 'null' }],
      },
      role: {
        type: 'string',
        enum: ['competitor', 'self'],
        default: 'competitor',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id', 'input'],
    description:
      "One input of spec §4.1 in ``input``: a Facebook page link\n(``facebook.com/<alias>``, ``/profile.php?id=<id>``), an Ad Library link\nwith ``view_all_page_id``, or a name. ``website`` (a URL or a bare\ndomain) is the official site a resolve compares landing pages with.\n``role=self`` adds the product's own page as a baseline.",
  },
  CompetitorDimensionCount: {
    properties: {
      factor_id: { type: 'string' },
      label: { type: 'string' },
      values: {
        items: { $ref: '#/components/schemas/CompetitorDimensionValueCount' },
        type: 'array',
      },
      none: { $ref: '#/components/schemas/CompetitorDimensionNone' },
    },
    type: 'object',
    required: ['factor_id', 'label'],
    description:
      'Per value, relevant observations a member confirmed it on\n(``confirmed``) and ones that only carry it as an AI suggestion.',
  },
  CompetitorDimensionNone: {
    properties: {
      confirmed: { type: 'integer', default: 0 },
      suggested: { type: 'integer', default: 0 },
      missing: { type: 'integer', default: 0 },
    },
    type: 'object',
    description:
      'Relevant observations with no value of this factor: ``confirmed``\nnone, ``suggested`` none, or no tags at all (``missing``).',
  },
  CompetitorDimensionValueCount: {
    properties: {
      value_id: { type: 'string' },
      label: { type: 'string' },
      confirmed: { type: 'integer', default: 0 },
      suggested: { type: 'integer', default: 0 },
    },
    type: 'object',
    required: ['value_id', 'label'],
  },
  CompetitorDiscovery: {
    properties: {
      advertiser_page_id: { type: 'string' },
      advertiser_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      categories: { items: { type: 'string' }, type: 'array' },
      observation_count: { type: 'integer' },
      landing_domains: { items: { type: 'string' }, type: 'array' },
      keywords: { items: { type: 'string' }, type: 'array' },
      relevance: { type: 'string', enum: ['relevant', 'adjacent'] },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      language: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      library_url: { type: 'string' },
      call_name: { type: 'string' },
      state: {
        type: 'string',
        enum: ['open', 'accepted', 'rejected', 'competitor'],
      },
      decision: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorDiscoveryDecision' },
          { type: 'null' },
        ],
      },
      competitor_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'advertiser_page_id',
      'observation_count',
      'relevance',
      'library_url',
      'call_name',
      'state',
    ],
    description:
      "One recommended advertiser: found only by the scan's keywords, past\nthe §7.1 rules (never a personal account; no avatar is ever shown) and\njudged ``relevant`` or ``adjacent`` by its ``discover:i`` call -- AI\ninference with a one-sentence ``reason`` in the scan's ``language``.\n``observation_count`` counts its keyword-only observations in this scan.\n``state``: ``open`` (never decided, or the latest decision is an\n``accept`` whose competitor no longer has the page on the product's\nlist -- archived or re-confirmed elsewhere -- so it is decided again);\n``accepted`` (the latest decision, its competitor still on the list);\n``rejected``; ``competitor`` (already on the list, added another way).\n``decision`` is the product's latest decision, in effect or not;\n``competitor_id`` is the listed competitor that has the page, if any.\n``library_url`` opens its page in the Meta Ad Library.",
  },
  CompetitorDiscoveryDecision: {
    properties: {
      decision_id: { type: 'string' },
      scan_id: { type: 'string' },
      advertiser_page_id: { type: 'string' },
      decision: { type: 'string', enum: ['accept', 'reject'] },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      competitor_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      decided_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      decided_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['decision_id', 'scan_id', 'advertiser_page_id', 'decision'],
    description:
      "A member's decision on one recommended advertiser: append-only, per\nproduct. ``competitor_id`` is the competitor an ``accept`` created. The\nnewest decision counts: a ``reject`` always, an ``accept`` only while\nthat competitor still has the page on the product's list.",
  },
  CompetitorDiscoveryDecisionRequest: {
    properties: {
      decision: { type: 'string', enum: ['accept', 'reject'] },
      reason: { anyOf: [{ type: 'string', maxLength: 500 }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['decision'],
    description:
      "``accept`` adds the advertiser as a competitor confirmed by the page\nID from its ads (no resolve, no charge); ``reject`` keeps it out of the\nproduct's later scans. ``reason`` is optional.",
  },
  CompetitorDiscoveryDecisionResult: {
    properties: {
      discovery: { $ref: '#/components/schemas/CompetitorDiscovery' },
      decision: { $ref: '#/components/schemas/CompetitorDiscoveryDecision' },
      competitor: {
        anyOf: [{ $ref: '#/components/schemas/Competitor' }, { type: 'null' }],
      },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['discovery', 'decision'],
    description:
      'The recommendation after the decision, the decision in effect, and the\ncompetitor an accept created (null for a reject).',
  },
  CompetitorDiscoveryList: {
    properties: {
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      language: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: {
        type: 'string',
        enum: [
          'pending',
          'running',
          'completed',
          'partial',
          'failed',
          'skipped',
        ],
      },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      advertisers: { type: 'integer', default: 0 },
      judged: { type: 'integer', default: 0 },
      recommended: { type: 'integer', default: 0 },
      unrelated: { type: 'integer', default: 0 },
      over_cap: { type: 'integer', default: 0 },
      items: {
        items: { $ref: '#/components/schemas/CompetitorDiscovery' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['scan_id', 'product_id', 'execution_mode', 'status'],
    description:
      "A scan's recommendations, most observations first. ``status`` and\n``reason`` are the discover step's, which runs last on a scan with\nkeywords (``skipped`` + ``no_keywords`` without any, ``no_candidates``\nwhen they found no advertiser outside the list; ``pending`` until it\nruns; ``partial`` or ``failed`` with ``calls_failed``,\n``ordinals_exhausted`` or the ``llm_chat`` code that stopped it, which a\nresume finishes). ``advertisers`` is\nthe frozen table (at most ``max_discover_advertisers``; ``over_cap``\nmore were left out), ``judged`` how many have an answer, ``unrelated``\nhow many were judged unrelated and are not listed.",
  },
  CompetitorList: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/Competitor' },
        type: 'array',
      },
    },
    type: 'object',
  },
  CompetitorPageProbe: {
    properties: {
      status: { type: 'string', enum: ['succeeded', 'failed', 'uncertain'] },
      country: { type: 'string' },
      total_active_ads: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      sampled_ads: { type: 'integer', default: 0 },
      landing_domains: { items: { type: 'string' }, type: 'array' },
      error_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['status', 'country'],
    description:
      "One ``probe`` call: the first page of a candidate's active ads.\n``total_active_ads`` is the provider's own count; ``landing_domains``\nare the registrable domains the sampled ads link to.",
  },
  CompetitorQuoteLine: {
    properties: {
      operation: { type: 'string', enum: ['page_search', 'probe'] },
      calls: { type: 'integer' },
      credits: { type: 'integer' },
      usd: { type: 'string' },
    },
    type: 'object',
    required: ['operation', 'calls', 'credits', 'usd'],
  },
  CompetitorRelevanceSources: {
    properties: {
      ai: { type: 'integer', default: 0 },
      rule: { type: 'integer', default: 0 },
      user: { type: 'integer', default: 0 },
      ai_inferred: { type: 'integer', default: 0 },
      non_ai: { type: 'integer', default: 0 },
    },
    type: 'object',
    description:
      'Who judged the relevant observations; ``non_ai`` = ``rule`` + ``user``.',
  },
  CompetitorResolution: {
    properties: {
      competitor_id: { type: 'string' },
      resolve_job_id: { type: 'string' },
      operation_id: { type: 'string' },
      status: {
        type: 'string',
        enum: ['queued', 'running', 'completed', 'failed', 'uncertain'],
      },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      provider: { type: 'string' },
      query: { type: 'string' },
      website_domain: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      country: { type: 'string' },
      quote: { $ref: '#/components/schemas/CompetitorResolveQuote' },
      cost: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorResolveCost' },
          { type: 'null' },
        ],
      },
      candidates: {
        items: { $ref: '#/components/schemas/CompetitorCandidate' },
        type: 'array',
      },
      auto_confirmed: { type: 'boolean', default: false },
      confirmation_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      waits: { type: 'integer', default: 0 },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorResolveFailure' },
          { type: 'null' },
        ],
      },
      reusable: { type: 'boolean', default: false },
      expires_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      started_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'competitor_id',
      'resolve_job_id',
      'operation_id',
      'status',
      'execution_mode',
      'provider',
      'query',
      'country',
      'quote',
    ],
    description:
      "A resolve: ``queued`` -> ``running`` -> ``completed``; ``failed``\n(the search failed, or a 402/401/403 stopped the probes -- candidates\nalready found stay listed) or ``uncertain`` (a call's outcome is\nunknown; nothing is resent). ``waits`` counts how often it queued\nbehind another market-data job on the same key (spec §5.1). A finished\nresolve is ``reusable`` for confirmation until ``expires_at`` (72 hours).\n``auto_confirmed`` is true when it confirmed the page itself (exact name\nand domain hit, or the alias of a pasted page link).",
  },
  CompetitorResolutionSummary: {
    properties: {
      resolve_job_id: { type: 'string' },
      operation_id: { type: 'string' },
      status: {
        type: 'string',
        enum: ['queued', 'running', 'completed', 'failed', 'uncertain'],
      },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      auto_confirmed: { type: 'boolean', default: false },
      reusable: { type: 'boolean', default: false },
      expires_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['resolve_job_id', 'operation_id', 'status', 'execution_mode'],
  },
  CompetitorResolveCost: {
    properties: {
      calls: { type: 'integer' },
      credits_charged: { type: 'integer' },
      usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['calls', 'credits_charged', 'usd'],
    description:
      'What the resolve\'s calls cost: settled from each call\'s\n``credits_charged``. ``usd`` is real money (``"0"`` under Mock).',
  },
  CompetitorResolveFailure: {
    properties: {
      code: { type: 'string' },
      retryable: { type: 'boolean', default: false },
      params: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['code'],
  },
  CompetitorResolveQuote: {
    properties: {
      competitor_id: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      available_modes: {
        items: { type: 'string', enum: ['byok', 'mock'] },
        type: 'array',
      },
      byok_unavailable_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      byok_unavailable_params: { additionalProperties: true, type: 'object' },
      provider: { type: 'string' },
      query: { type: 'string' },
      website_domain: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      country: { type: 'string' },
      max_credits: { type: 'integer' },
      max_usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      currency: { type: 'string', const: 'USD', default: 'USD' },
      pricing_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      lines: {
        items: { $ref: '#/components/schemas/CompetitorQuoteLine' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'competitor_id',
      'execution_mode',
      'available_modes',
      'provider',
      'query',
      'country',
      'max_credits',
      'max_usd',
    ],
    description:
      'The maximum a resolve can cost: ``page_search`` x 1 + ``probe`` x 3\n(fewer probes are made when fewer candidates qualify; the rest is\nreleased). ``max_usd`` is what the Workspace pays: ``"0"`` under\nControlled Mock, whose ``simulated_usd`` is an estimate only (null when\nthe deployment has no price table). ``available_modes`` lists ``byok``\nonly when a ``market_data`` key is bound and prices are configured;\notherwise ``byok_unavailable_code`` says why, with the values its copy\ninterpolates in ``byok_unavailable_params`` (the missing ``operations``\nof ``market_data_price_operation_missing``).',
  },
  CompetitorResolveRequest: {
    properties: {
      execution_mode: {
        anyOf: [{ type: 'string', enum: ['byok', 'mock'] }, { type: 'null' }],
      },
      country: { type: 'string', maxLength: 2, minLength: 2, default: 'US' },
    },
    additionalProperties: false,
    type: 'object',
    description:
      "Start a resolve. ``execution_mode`` defaults to the quote's default\n(``byok`` when available, else ``mock``); ``country`` (ISO 3166-1\nalpha-2) is the market the probes read active ads in.",
  },
  CompetitorScan: {
    properties: {
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      operation_id: { type: 'string' },
      job_id: { type: 'string' },
      status: {
        type: 'string',
        enum: [
          'queued',
          'running',
          'completed',
          'partial',
          'failed',
          'uncertain',
        ],
      },
      stage: {
        anyOf: [
          {
            type: 'string',
            enum: ['fetch', 'media', 'filter', 'tag', 'cluster', 'freeze'],
          },
          { type: 'null' },
        ],
      },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      provider: { type: 'string' },
      platform: { type: 'string', const: 'meta', default: 'meta' },
      country: { type: 'string' },
      params: { $ref: '#/components/schemas/CompetitorScanParams' },
      targets: {
        items: { $ref: '#/components/schemas/CompetitorScanTarget' },
        type: 'array',
      },
      steps: {
        items: { $ref: '#/components/schemas/CompetitorScanStepState' },
        type: 'array',
      },
      quote: { $ref: '#/components/schemas/CompetitorScanQuote' },
      cost: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorScanCost' },
          { type: 'null' },
        ],
      },
      initial_credential_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      resumes: {
        items: { $ref: '#/components/schemas/CompetitorScanResume' },
        type: 'array',
      },
      reconciliation: {
        items: { $ref: '#/components/schemas/CompetitorScanReconciliation' },
        type: 'array',
      },
      media: { $ref: '#/components/schemas/CompetitorScanMedia' },
      observation_count: { type: 'integer', default: 0 },
      waits: { type: 'integer', default: 0 },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorScanFailure' },
          { type: 'null' },
        ],
      },
      can_resume: { type: 'boolean', default: false },
      reclusters: {
        items: { $ref: '#/components/schemas/CompetitorScanRecluster' },
        type: 'array',
      },
      snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      started_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'scan_id',
      'product_id',
      'operation_id',
      'job_id',
      'status',
      'execution_mode',
      'provider',
      'country',
      'params',
      'quote',
    ],
    description:
      "A scan: ``queued`` -> ``running`` -> ``completed`` | ``partial`` (some\npages succeeded, some failed or ran out of credits) | ``failed`` (no page\nsucceeded) | ``uncertain`` (a page's outcome is unknown and not yet\nreconciled). ``stage`` is the last completed step. ``can_resume`` when\npages are left, a page awaits reconciliation, or a judgment step did not\nfinish (a ``completed`` scan whose AI key was refused mid-way, say).\n``waits`` counts how often it queued behind another market-data job on\nthe same key (spec §5.1). ``snapshot_id`` is the snapshot of the\nrevisions in effect as of the last freeze.",
  },
  CompetitorScanBalanceCheck: {
    properties: {
      outcome: {
        type: 'string',
        enum: ['not_charged', 'charged', 'manual_required'],
      },
      reason: {
        anyOf: [
          {
            type: 'string',
            enum: [
              'difference',
              'cached_baseline',
              'no_baseline',
              'credential_changed',
              'unknown_charge_in_window',
              'balance_unavailable',
            ],
          },
          { type: 'null' },
        ],
      },
      difference: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      baseline_credits_remaining: {
        anyOf: [{ type: 'integer' }, { type: 'null' }],
      },
      known_charges: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      balance: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      checked_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['outcome'],
    description:
      'The automatic reconciliation (spec §5.1): the free credit-balance\ncompared with the last successful call before the page, less the charges\nknown since. ``difference`` 0 settles "not charged", exactly 1 "charged";\nanything else (``reason``) is left to the Owner.',
  },
  CompetitorScanCost: {
    properties: {
      market_data: {
        $ref: '#/components/schemas/CompetitorScanMarketDataCost',
      },
      llm: { $ref: '#/components/schemas/CompetitorScanLlmCost' },
      usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['market_data', 'llm', 'usd'],
    description:
      'What the scan cost so far: both lines and their sum (real money).',
  },
  CompetitorScanCredential: {
    properties: {
      binding_scope: { type: 'string', enum: ['market_data', 'generation'] },
      provider: { type: 'string' },
      ready: { type: 'boolean' },
      code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      params: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['binding_scope', 'provider', 'ready'],
    description:
      'A key a real scan needs: ``market_data`` (ScrapeCreators) or\n``generation`` (the AI key ``llm_chat`` uses). ``code`` says why it is\nnot ready.',
  },
  CompetitorScanFailure: {
    properties: {
      code: { type: 'string' },
      retryable: { type: 'boolean', default: false },
      params: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['code'],
  },
  CompetitorScanList: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/CompetitorScanSummary' },
        type: 'array',
      },
    },
    type: 'object',
  },
  CompetitorScanLlmCost: {
    properties: {
      calls: { type: 'integer' },
      usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['calls', 'usd'],
  },
  CompetitorScanLlmQuote: {
    properties: {
      provider: { type: 'string' },
      model: { type: 'string' },
      calls: { type: 'integer' },
      max_usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      price_rule_version: { type: 'string' },
      lines: {
        items: { $ref: '#/components/schemas/CompetitorScanLlmQuoteLine' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['provider', 'model', 'calls', 'max_usd', 'price_rule_version'],
    description:
      'Line (b): the LLM maximum, reserved per call name and released for\nevery call name the scan does not use.',
  },
  CompetitorScanLlmQuoteLine: {
    properties: {
      step: { type: 'string', enum: ['filter', 'tag', 'cluster', 'discover'] },
      calls: { type: 'integer' },
      max_input_tokens: { type: 'integer' },
      max_output_tokens: { type: 'integer' },
      usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['step', 'calls', 'max_input_tokens', 'max_output_tokens', 'usd'],
    description:
      "One LLM step: ``calls`` numbered call names, each reserved at\n``quote_llm_chat``'s maximum for its token ceilings.",
  },
  CompetitorScanMarketDataCost: {
    properties: {
      calls: { type: 'integer' },
      credits_charged: { type: 'integer' },
      usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['calls', 'credits_charged', 'usd'],
    description:
      "Settled from each call's ``credits_charged`` (and reconciled pages).",
  },
  CompetitorScanMarketDataQuote: {
    properties: {
      provider: { type: 'string' },
      max_credits: { type: 'integer' },
      max_usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      pricing_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      lines: {
        items: { $ref: '#/components/schemas/CompetitorScanQuoteLine' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['provider', 'max_credits', 'max_usd'],
    description:
      'Line (a): the provider\'s maximum. ``max_usd`` is real money (``"0"``\nunder Mock, whose ``simulated_usd`` is an estimate, null without a price\ntable).',
  },
  CompetitorScanMedia: {
    properties: {
      planned: { type: 'integer', default: 0 },
      available: { type: 'integer', default: 0 },
      unavailable: { type: 'integer', default: 0 },
      pending: { type: 'integer', default: 0 },
      refreshes_used: { type: 'integer', default: 0 },
      refresh_quota: { type: 'integer', default: 0 },
    },
    type: 'object',
    description: 'Media archiving: one entry per card image or video poster.',
  },
  CompetitorScanParams: {
    properties: {
      competitor_ids: { items: { type: 'string' }, type: 'array' },
      keywords: { items: { type: 'string' }, type: 'array' },
      country: { type: 'string' },
      platform: { type: 'string', const: 'meta', default: 'meta' },
      ad_status: { type: 'string', const: 'active', default: 'active' },
      max_pages_per_competitor: { type: 'integer' },
      keyword_pages: { type: 'integer' },
      max_media_refreshes: { type: 'integer' },
      max_tagged_ads: { type: 'integer' },
      max_discover_advertisers: { type: 'integer' },
      language: { type: 'string', enum: ['en', 'zh-CN'] },
    },
    type: 'object',
    required: [
      'competitor_ids',
      'country',
      'max_pages_per_competitor',
      'keyword_pages',
      'max_media_refreshes',
      'max_tagged_ads',
      'max_discover_advertisers',
      'language',
    ],
    description:
      "The scan's frozen parameters; first release: Meta, active ads only.",
  },
  CompetitorScanQuote: {
    properties: {
      kind: { type: 'string', enum: ['scan', 'resume'] },
      scan_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      product_id: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      available_modes: {
        items: { type: 'string', enum: ['byok', 'mock'] },
        type: 'array',
      },
      byok_unavailable_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      byok_unavailable_params: { additionalProperties: true, type: 'object' },
      credentials: {
        items: { $ref: '#/components/schemas/CompetitorScanCredential' },
        type: 'array',
      },
      country: { type: 'string' },
      language: { type: 'string', enum: ['en', 'zh-CN'] },
      competitor_count: { type: 'integer' },
      keyword_count: { type: 'integer' },
      market_data: {
        $ref: '#/components/schemas/CompetitorScanMarketDataQuote',
      },
      llm: { $ref: '#/components/schemas/CompetitorScanLlmQuote' },
      max_usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      currency: { type: 'string', const: 'USD', default: 'USD' },
    },
    type: 'object',
    required: [
      'kind',
      'product_id',
      'execution_mode',
      'available_modes',
      'country',
      'language',
      'competitor_count',
      'keyword_count',
      'market_data',
      'llm',
      'max_usd',
    ],
    description:
      'The two-line maximum of a scan (``kind=scan``) or of what is left of\none (``kind=resume``: remaining pages, the unused refresh quota and the\nLLM steps without an answer). ``available_modes`` lists ``byok`` only\nwhen both keys are ready and prices are configured; otherwise\n``byok_unavailable_code`` (and its ``byok_unavailable_params``) says\nwhy. ``max_usd`` is the sum of both lines.',
  },
  CompetitorScanQuoteLine: {
    properties: {
      operation: {
        type: 'string',
        enum: ['page', 'keyword_page', 'ad_refresh'],
      },
      calls: { type: 'integer' },
      credits: { type: 'integer' },
      usd: { type: 'string' },
    },
    type: 'object',
    required: ['operation', 'calls', 'credits', 'usd'],
    description:
      'One provider operation: ``calls`` x its credits x the credit price.',
  },
  CompetitorScanRecluster: {
    properties: {
      operation_id: { type: 'string' },
      job_id: { type: 'string' },
      status: {
        type: 'string',
        enum: ['queued', 'running', 'completed', 'failed'],
      },
      max_usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      cluster_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      failure: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      requested_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      requested_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['operation_id', 'job_id', 'status', 'max_usd'],
    description:
      "A member's paid recluster: its own Operation and Job, ``cluster:1``\nreserved at ``max_usd``; when it completes, the new clusters revision\nand the snapshot it froze.",
  },
  CompetitorScanReclusterQuote: {
    properties: {
      kind: { type: 'string', const: 'recluster' },
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      relevant_count: { type: 'integer' },
      llm: { $ref: '#/components/schemas/CompetitorScanLlmQuote' },
      max_usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      currency: { type: 'string', const: 'USD', default: 'USD' },
    },
    type: 'object',
    required: [
      'kind',
      'scan_id',
      'product_id',
      'execution_mode',
      'relevant_count',
      'llm',
      'max_usd',
    ],
    description:
      "What a recluster can cost at most: ``cluster:1`` at\n``quote_llm_chat``'s maximum, in the scan's own execution mode.",
  },
  CompetitorScanReconcileRequest: {
    properties: {
      attempt_id: { type: 'string', maxLength: 64, minLength: 1 },
      outcome: { type: 'string', enum: ['charged', 'not_charged'] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['attempt_id', 'outcome'],
    description:
      "The Owner's verdict on one pending page: did the provider charge it?",
  },
  CompetitorScanReconciliation: {
    properties: {
      attempt_id: { type: 'string' },
      call_name: { type: 'string' },
      origin: { type: 'string' },
      page: { type: 'integer' },
      credential_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: { type: 'string', enum: ['pending', 'charged', 'not_charged'] },
      source: {
        anyOf: [{ type: 'string', enum: ['auto', 'owner'] }, { type: 'null' }],
      },
      balance_check: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorScanBalanceCheck' },
          { type: 'null' },
        ],
      },
      decided_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      decided_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['attempt_id', 'call_name', 'origin', 'page', 'status'],
    description:
      'A page whose outcome was lost after the dispatch fence. Its cost is\nsettled by the automatic check or by the Owner; the page itself is\nfetched again as a new attempt.',
  },
  CompetitorScanRequest: {
    properties: {
      product_id: { type: 'string', maxLength: 64, minLength: 1 },
      competitor_ids: {
        items: { type: 'string', maxLength: 64, minLength: 1 },
        type: 'array',
        maxItems: 20,
        minItems: 1,
      },
      keywords: {
        items: { type: 'string', maxLength: 100, minLength: 1 },
        type: 'array',
        maxItems: 5,
      },
      country: { type: 'string', maxLength: 2, minLength: 2, default: 'US' },
      max_pages_per_competitor: {
        type: 'integer',
        maximum: 5,
        minimum: 1,
        default: 3,
      },
      keyword_pages: { type: 'integer', maximum: 5, minimum: 1, default: 2 },
      max_media_refreshes: {
        type: 'integer',
        maximum: 50,
        minimum: 0,
        default: 10,
      },
      max_tagged_ads: {
        type: 'integer',
        maximum: 200,
        minimum: 0,
        default: 60,
      },
      max_discover_advertisers: {
        type: 'integer',
        maximum: 100,
        minimum: 0,
        default: 30,
      },
      execution_mode: {
        anyOf: [{ type: 'string', enum: ['byok', 'mock'] }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id', 'competitor_ids'],
    description:
      "What to scan (spec §5.1). Every competitor must belong to the product,\nbe active and have a confirmed page; ``role=self`` competitors are\nscanned as the baseline. ``keywords`` feed competitor discovery (spec\n§8), ``keyword_pages`` pages each. ``max_media_refreshes`` is the quota\nof paid ``ad_refresh`` calls for media links that expired.\n``max_tagged_ads`` and ``max_discover_advertisers`` cap the LLM steps.\n``execution_mode`` defaults to the quote's default (``byok`` when both\nkeys are ready, else ``mock``). The AI output language is the\ninitiator's interface language, frozen into ``params.language``.",
  },
  CompetitorScanResume: {
    properties: {
      operation_id: { type: 'string' },
      job_id: { type: 'string' },
      credential_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      max_credits: { type: 'integer' },
      max_usd: { type: 'string' },
      requested_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      requested_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['operation_id', 'job_id', 'max_credits', 'max_usd'],
    description:
      'A resume: same scan, a new Job and Operation, possibly a newer key.',
  },
  CompetitorScanSnapshot: {
    properties: {
      snapshot_id: { type: 'string' },
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      country: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      hash: { type: 'string' },
      cluster_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      current: { type: 'boolean', default: false },
      counts: { $ref: '#/components/schemas/CompetitorSnapshotCounts' },
      job_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      content: { additionalProperties: true, type: 'object' },
      created: { type: 'boolean', default: false },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'snapshot_id',
      'scan_id',
      'product_id',
      'country',
      'execution_mode',
      'hash',
    ],
    description:
      'A snapshot with its frozen ``content`` (``version``\n``competitor-snapshot/v1``: ``relevant_ad_ids``, ``judgments``,\n``cluster_revision``, ``claim_clusters``, ``unclustered_ad_ids``,\n``dimension_counts``, ``long_running_ad_ids``, ``adjacent_ad_ids``,\n``self_ad_ids``, ``ads``, ``params``). A refreeze answers ``created``\nfalse when the hash was already frozen.',
  },
  CompetitorScanSnapshotList: {
    properties: {
      scan_id: { type: 'string' },
      current_snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      items: {
        items: { $ref: '#/components/schemas/CompetitorScanSnapshotMeta' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['scan_id'],
    description: "A scan's snapshots, newest first.",
  },
  CompetitorScanSnapshotMeta: {
    properties: {
      snapshot_id: { type: 'string' },
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      country: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      hash: { type: 'string' },
      cluster_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      current: { type: 'boolean', default: false },
      counts: { $ref: '#/components/schemas/CompetitorSnapshotCounts' },
      job_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'snapshot_id',
      'scan_id',
      'product_id',
      'country',
      'execution_mode',
      'hash',
    ],
    description:
      "A snapshot: the scan's relevant observations and the revisions in\neffect, frozen with a hash over the whole content (spec §7.1).\n``current`` is the scan's ``snapshot_id``.",
  },
  CompetitorScanSnapshotSummary: {
    properties: {
      snapshot_id: { type: 'string' },
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      country: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      hash: { type: 'string' },
      cluster_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      current: { type: 'boolean', default: false },
      counts: { $ref: '#/components/schemas/CompetitorSnapshotCounts' },
      job_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      mock: { type: 'boolean', default: false },
      language: {
        anyOf: [{ type: 'string', enum: ['en', 'zh-CN'] }, { type: 'null' }],
      },
      pending_changes: { type: 'boolean', default: false },
      can_recluster: { type: 'boolean', default: false },
      relevance_sources: {
        $ref: '#/components/schemas/CompetitorRelevanceSources',
      },
      clusters: {
        items: { $ref: '#/components/schemas/CompetitorClaimCluster' },
        type: 'array',
      },
      unclustered: {
        items: { $ref: '#/components/schemas/CompetitorSnapshotAd' },
        type: 'array',
      },
      adjacent: {
        items: { $ref: '#/components/schemas/CompetitorSnapshotAd' },
        type: 'array',
      },
      long_running: {
        items: { $ref: '#/components/schemas/CompetitorSnapshotAd' },
        type: 'array',
      },
      self_observations: {
        items: { $ref: '#/components/schemas/CompetitorSnapshotAd' },
        type: 'array',
      },
      long_running_days: { type: 'integer', default: 60 },
      dimension_counts: {
        items: { $ref: '#/components/schemas/CompetitorDimensionCount' },
        type: 'array',
      },
      conclusion: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorSnapshotConclusion' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'snapshot_id',
      'scan_id',
      'product_id',
      'country',
      'execution_mode',
      'hash',
    ],
    description:
      "The summary of one snapshot (spec §9): claim clusters, the relevant\nobservations no cluster holds (``unclustered``: flipped to relevant\nsince the last clustering), adjacent, long-running (``long_running_days``\nor more) and own-page observations, dimension counts and the AI's\nconclusion. ``pending_changes`` says a refreeze would make a new\nsnapshot; ``can_recluster`` that a recluster can be quoted now.",
  },
  CompetitorScanStepState: {
    properties: {
      step: {
        type: 'string',
        enum: ['fetch', 'media', 'filter', 'tag', 'cluster', 'freeze'],
      },
      status: {
        type: 'string',
        enum: [
          'pending',
          'running',
          'completed',
          'partial',
          'failed',
          'skipped',
        ],
      },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      started_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      attempts: { type: 'integer', default: 0 },
    },
    type: 'object',
    required: ['step', 'status'],
    description:
      "One Worker step (spec §5.2). A ``partial`` or ``failed`` filter or tag\nstep says why in ``reason``: ``calls_failed``, ``ordinals_exhausted``\n(more work than reserved call names) or the ``llm_chat`` code that\nstopped the run (e.g. ``credential_rejected``; tag and cluster then\nrecord the same code without calling). ``skipped`` reasons:\n``no_vocabulary`` (tag: the product has no dimension vocabulary),\n``vocabulary_not_frozen`` (tag: a scan admitted before Issue #272),\n``no_relevant_observations`` (tag, cluster), ``no_observations`` and\n``no_judgments`` (freeze); ``not_implemented`` on scans of releases\nbefore Issue #272. A ``completed`` cluster step with ``reason=answered``\nread an earlier run's answer. Discovery (Issue #276) reports its step\non the scan's discoveries, not here.",
  },
  CompetitorScanSummary: {
    properties: {
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      operation_id: { type: 'string' },
      job_id: { type: 'string' },
      status: {
        type: 'string',
        enum: [
          'queued',
          'running',
          'completed',
          'partial',
          'failed',
          'uncertain',
        ],
      },
      stage: {
        anyOf: [
          {
            type: 'string',
            enum: ['fetch', 'media', 'filter', 'tag', 'cluster', 'freeze'],
          },
          { type: 'null' },
        ],
      },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      country: { type: 'string' },
      competitor_ids: { items: { type: 'string' }, type: 'array' },
      keywords: { items: { type: 'string' }, type: 'array' },
      observation_count: { type: 'integer', default: 0 },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'scan_id',
      'product_id',
      'operation_id',
      'job_id',
      'status',
      'execution_mode',
      'country',
    ],
  },
  CompetitorScanTarget: {
    properties: {
      origin: { type: 'string' },
      kind: { type: 'string', enum: ['page', 'keyword'] },
      competitor_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      role: {
        anyOf: [
          { type: 'string', enum: ['competitor', 'self'] },
          { type: 'null' },
        ],
      },
      page_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      page_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      observed_page_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confirmation_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      keyword: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: {
        type: 'string',
        enum: ['pending', 'fetching', 'completed', 'failed', 'uncertain'],
      },
      pages_planned: { type: 'integer' },
      pages_fetched: { type: 'integer' },
      ads_seen: { type: 'integer' },
      exhausted: { type: 'boolean', default: false },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorScanFailure' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'origin',
      'kind',
      'status',
      'pages_planned',
      'pages_fetched',
      'ads_seen',
    ],
    description:
      "One fetch path: a competitor's confirmed page (``page:<competitor_id>``)\nor a keyword (``keyword:<keyword>``). ``pages_fetched`` counts pages\nthat succeeded; ``exhausted`` means the provider had no further page.\nA failed target is fetched again by ``resume`` from its stored cursor.\n``page_name`` is the confirmation's, frozen at admission (none for a\nlink); ``observed_page_name`` is read-only, the advertiser name of the\npage's newest observation in the product's scans (null until one).",
  },
  CompetitorSnapshotAd: {
    properties: {
      ad_id: { type: 'string' },
      ad_archive_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      advertiser_page_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      advertiser_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      role: {
        anyOf: [
          { type: 'string', enum: ['competitor', 'self'] },
          { type: 'null' },
        ],
      },
      days_running: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      display_format: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      title: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      library_url: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      relevance: {
        anyOf: [
          { type: 'string', enum: ['relevant', 'adjacent', 'unrelated'] },
          { type: 'null' },
        ],
      },
      relevance_source: {
        anyOf: [
          { type: 'string', enum: ['ai', 'rule', 'user'] },
          { type: 'null' },
        ],
      },
      ai_inferred: { type: 'boolean', default: false },
      tags: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorSnapshotAdTags' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['ad_id'],
    description:
      "An observation as the summary lists it. ``ai_inferred`` is false for\na member's or a rule's relevance (``source`` ``user`` / ``rule``).",
  },
  CompetitorSnapshotAdTags: {
    properties: {
      source: { type: 'string', enum: ['ai', 'rule', 'user'] },
      ai_suggestion: { type: 'boolean' },
      values: {
        additionalProperties: { anyOf: [{ type: 'string' }, { type: 'null' }] },
        type: 'object',
      },
    },
    type: 'object',
    required: ['source', 'ai_suggestion'],
  },
  CompetitorSnapshotConclusion: {
    properties: {
      text: { type: 'string' },
      ai_inference: { type: 'boolean', default: true },
      cluster_revision: { type: 'integer' },
      language: {
        anyOf: [{ type: 'string', enum: ['en', 'zh-CN'] }, { type: 'null' }],
      },
    },
    type: 'object',
    required: ['text', 'cluster_revision'],
    description:
      "The AI's one-sentence conclusion of the cited clusters revision (AI inference).",
  },
  CompetitorSnapshotCounts: {
    properties: {
      observations: { type: 'integer', default: 0 },
      relevant: { type: 'integer', default: 0 },
      adjacent: { type: 'integer', default: 0 },
      unrelated: { type: 'integer', default: 0 },
      unassessed: { type: 'integer', default: 0 },
      self: { type: 'integer', default: 0 },
    },
    type: 'object',
  },
  CompetitorTagInput: {
    properties: {
      factor_id: { type: 'string', maxLength: 200, minLength: 1 },
      value_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['factor_id'],
  },
  CompetitorTagVocabulary: {
    properties: {
      factors: {
        items: { $ref: '#/components/schemas/CompetitorTagVocabularyFactor' },
        type: 'array',
      },
    },
    type: 'object',
    description: "The product's dimension vocabulary as the scan froze it.",
  },
  CompetitorTagVocabularyFactor: {
    properties: {
      factor_id: { type: 'string' },
      label: { type: 'string' },
      source: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      values: {
        items: { $ref: '#/components/schemas/CompetitorTagVocabularyValue' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['factor_id', 'label'],
    description:
      '``source`` is ``test_plan``, ``dimension_template`` or\n``expression_catalog`` (the expression mechanism catalog, one factor\nevery product has; Issue #329).',
  },
  CompetitorTagVocabularyValue: {
    properties: { value_id: { type: 'string' }, label: { type: 'string' } },
    type: 'object',
    required: ['value_id', 'label'],
  },
  ConceptResolutionActionRequest: {
    properties: {
      duplicate_group_hash: { type: 'string', maxLength: 64, minLength: 64 },
      action: {
        type: 'string',
        enum: [
          'allow_controlled_duplicate',
          'exclude_source_candidate',
          'remap_before_freeze',
        ],
      },
      reason: { type: 'string', maxLength: 2000, minLength: 1 },
      source_candidate_fingerprints: {
        anyOf: [{ items: { type: 'string' }, type: 'array' }, { type: 'null' }],
      },
      source_entry_ids: {
        anyOf: [{ items: { type: 'string' }, type: 'array' }, { type: 'null' }],
      },
      source_candidate_fingerprint: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      remap_concept_key_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['duplicate_group_hash', 'action', 'reason'],
  },
  ConceptResolutionEvidence: {
    properties: {
      concept_resolutions_hash: { type: 'string' },
      had_duplicates: { type: 'boolean' },
      resolutions: {
        items: { $ref: '#/components/schemas/ResolutionEvidenceRow' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['concept_resolutions_hash', 'had_duplicates'],
  },
  ConfirmAIQC: {
    properties: {
      ai_qc_ids: {
        items: { type: 'string' },
        type: 'array',
        maxItems: 60,
        minItems: 1,
      },
      resolved_checks: {
        additionalProperties: { type: 'string', const: 'pass' },
        type: 'object',
        maxProperties: 6,
      },
      note: { type: 'string', maxLength: 2000, default: '' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['ai_qc_ids'],
  },
  ConfirmExperimentProposalRequest: {
    properties: { expected_proposal_sequence: { type: 'integer', minimum: 1 } },
    type: 'object',
    required: ['expected_proposal_sequence'],
  },
  ConfirmResult: {
    properties: {
      batch_id: { type: 'string' },
      item_ids: { items: { type: 'string' }, type: 'array' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean' },
    },
    type: 'object',
    required: ['batch_id', 'replayed'],
  },
  ConfirmedCombination: {
    properties: {
      combination_id: { type: 'string' },
      combination_key: { type: 'string' },
      current_asset_version_id: { type: 'string' },
      supporting_evaluations: { items: { type: 'string' }, type: 'array' },
      scope: { type: 'string', const: 'repeatable_under_recorded_conditions' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'combination_id',
      'combination_key',
      'current_asset_version_id',
      'supporting_evaluations',
      'scope',
    ],
  },
  ConfirmedDirection: {
    properties: {
      plan_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      direction_key: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      title: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
  },
  ConfirmedProduct: {
    properties: {
      product_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      entry_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      version_no: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      confirmed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      card: { additionalProperties: true, type: 'object' },
      bound_images: {
        items: { $ref: '#/components/schemas/BoundImage' },
        type: 'array',
      },
    },
    type: 'object',
  },
  ConfirmedTag: {
    properties: {
      factor_id: { type: 'string' },
      factor_label: { type: 'string' },
      value_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      value_label: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      suggested_value_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      suggested_confidence: { anyOf: [{ type: 'number' }, { type: 'null' }] },
      source: { type: 'string' },
    },
    type: 'object',
    required: ['factor_id', 'factor_label', 'source'],
  },
  ConnectionCheckInput: {
    properties: {
      execution_mode: {
        anyOf: [{ type: 'string', maxLength: 40 }, { type: 'null' }],
        description:
          "Ignored. A data source is checked in its published version's execution mode.",
        deprecated: true,
      },
    },
    type: 'object',
  },
  ConversionRequest: {
    properties: {
      provider: { type: 'string', default: 'scale_advice_default' },
      model: { type: 'string', default: 'scale_advice_default' },
      refresh_axis: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
  },
  ConversionResponse: {
    properties: {
      id: { type: 'string' },
      thread_id: { type: 'string' },
      occurrence_id: { type: 'string' },
      generation_draft_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      scale_test_plan_draft_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      input_lineage_hash: { type: 'string' },
      initiator_id: { type: 'string' },
      created_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'thread_id',
      'occurrence_id',
      'generation_draft_id',
      'scale_test_plan_draft_id',
      'input_lineage_hash',
      'initiator_id',
      'created_at',
    ],
  },
  CopyChange: {
    properties: { expected: { type: 'string' }, rendered: { type: 'string' } },
    additionalProperties: false,
    type: 'object',
    required: ['expected', 'rendered'],
    description:
      'Words of the confirmed copy and what the image rendered in their place; either side may be empty.',
  },
  CopyEdit: {
    properties: {
      headline: {
        anyOf: [{ type: 'string', maxLength: 200 }, { type: 'null' }],
      },
      primary_text: {
        anyOf: [{ type: 'string', maxLength: 1000 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
  },
  CostEvidence: {
    properties: {
      state: { type: 'string' },
      amount: { type: 'string' },
      currency: { type: 'string' },
    },
    type: 'object',
    required: ['state', 'amount', 'currency'],
    description: "Spend reservation state for a RunItem's provider call.",
  },
  CreateBindingRequest: {
    properties: {
      ad_account_id: { type: 'string' },
      meta_mode: { type: 'string' },
      access_token_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      app_secret_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['ad_account_id', 'meta_mode'],
  },
  CreateConnectionRequest: {
    properties: {
      meta_mode: { type: 'string' },
      ad_account_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['meta_mode'],
  },
  CreateContractRequest: {
    properties: {
      connection_id: { type: 'string' },
      meta_mode: { type: 'string' },
      contract_type: { type: 'string' },
      ad_account_id: { type: 'string' },
      fields: { items: {}, type: 'array' },
      attribution_window: { additionalProperties: true, type: 'object' },
      breakdowns: { items: {}, type: 'array' },
      time_window: { additionalProperties: true, type: 'object' },
      local_timezone: { type: 'string' },
      delivery_config: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'connection_id',
      'meta_mode',
      'contract_type',
      'ad_account_id',
      'fields',
      'attribution_window',
      'breakdowns',
      'time_window',
      'local_timezone',
    ],
  },
  CreateCredentialRequest: {
    properties: {
      credential_key: { type: 'string' },
      provider: { type: 'string' },
      plaintext_b64: { type: 'string' },
      display_label: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['credential_key', 'provider', 'plaintext_b64'],
  },
  CreateDownloadIntentRequest: {
    properties: {
      owner_kind: { type: 'string' },
      owner_id: { type: 'string' },
      committed_blob_ref_key: { type: 'string' },
      committed_blob_ref_version: { type: 'string' },
      filename: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'owner_kind',
      'owner_id',
      'committed_blob_ref_key',
      'committed_blob_ref_version',
      'filename',
    ],
  },
  CreateEntryInput: {
    properties: {
      entry_type: { type: 'string' },
      display_name: { type: 'string', maxLength: 300, minLength: 1 },
      content: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['entry_type', 'display_name'],
  },
  CreateEvidenceHandoff: {
    properties: {
      evaluation_id: { type: 'string', maxLength: 64, minLength: 1 },
      asset_version_id: { type: 'string', maxLength: 64, minLength: 1 },
      offer_version_id: { type: 'string', maxLength: 64, minLength: 1 },
      confirms_tested_context: { type: 'boolean' },
      note: { type: 'string', maxLength: 2000, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'evaluation_id',
      'asset_version_id',
      'offer_version_id',
      'confirms_tested_context',
      'note',
    ],
  },
  CreateExperimentRequest: {
    properties: {
      product_id: { type: 'string', minLength: 1 },
      name: { type: 'string', maxLength: 300, minLength: 1 },
      draft_context: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['product_id', 'name', 'draft_context'],
  },
  CreateExportInput: {
    properties: {
      selector: { $ref: '#/components/schemas/ExportSelectorInput' },
    },
    type: 'object',
    required: ['selector'],
  },
  CreateHandoff: {
    properties: {
      asset_version_ids: {
        items: { type: 'string' },
        type: 'array',
        maxItems: 60,
        minItems: 1,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['asset_version_ids'],
  },
  CreateImageEdit: {
    properties: {
      asset_version_id: { type: 'string', maxLength: 64, minLength: 1 },
      crop: {
        anyOf: [{ $ref: '#/components/schemas/ImageCrop' }, { type: 'null' }],
      },
      rotation: { type: 'integer', enum: [0, 90, 180, 270], default: 0 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['asset_version_id'],
  },
  CreateInternalEntryInput: {
    properties: {
      entry_type: { type: 'string' },
      display_name: { type: 'string', maxLength: 300, minLength: 1 },
      content: { additionalProperties: true, type: 'object' },
      parent_type: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      parent_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['entry_type', 'display_name'],
  },
  CreateProject: {
    properties: {
      source_text: { type: 'string', maxLength: 16000, default: '' },
      product_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
  },
  CreateReferenceObjectInput: {
    properties: {
      upload_intent_id: { type: 'string' },
      filename: { type: 'string', default: '' },
      declared_mime: { type: 'string', default: '' },
      execution_mode: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
        description:
          'Ignored. The content-safety execution mode is decided by the server.',
        deprecated: true,
      },
      safety_scenario: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
        description:
          'Ignored. The controlled-Mock scenario is decided by the server.',
        deprecated: true,
      },
      rights: {
        anyOf: [
          { $ref: '#/components/schemas/ReferenceRightsDeclarationInput' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['upload_intent_id'],
  },
  CreateRegenerationDraftInput: {
    properties: {
      intent: { type: 'string', minLength: 1 },
      prompt_overrides: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      qc_attempt_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['intent'],
  },
  CreateReviewInput: {
    properties: {
      review_value: { type: 'string', minLength: 1 },
      reason: { type: 'string', default: '' },
      supersedes_review_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['review_value'],
  },
  CreateUploadIntentRequest: {
    properties: {
      purpose: { type: 'string' },
      owner_kind: { type: 'string' },
      owner_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      filename: { type: 'string' },
      content_type: { type: 'string' },
      content_length_min: { type: 'integer' },
      content_length_max: { type: 'integer' },
      checksum_sha256_b64: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'purpose',
      'owner_kind',
      'filename',
      'content_type',
      'content_length_min',
      'content_length_max',
      'checksum_sha256_b64',
    ],
  },
  CreatedEvidenceHandoff: {
    properties: {
      decision_id: { type: 'string' },
      evidence_origin: { type: 'string', const: 'marketing_business_target' },
      no_meta_writes: { type: 'boolean', const: true },
    },
    additionalProperties: false,
    type: 'object',
    required: ['decision_id', 'evidence_origin', 'no_meta_writes'],
  },
  CreatedEvidenceHandoffEnvelope: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/CreatedEvidenceHandoff' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  CreativeCombination: {
    properties: {
      id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      angle_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      expression_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      elements: {
        additionalProperties: { type: 'string', maxLength: 1000 },
        type: 'object',
        maxProperties: 20,
      },
      headline: { type: 'string', maxLength: 2000, minLength: 1 },
      body_copy: { type: 'string', maxLength: 2000, minLength: 1 },
      visual_brief: { type: 'string', maxLength: 2000, minLength: 1 },
      image_brief: {
        anyOf: [{ $ref: '#/components/schemas/ImageBrief' }, { type: 'null' }],
      },
      checks: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 12,
        minItems: 1,
      },
      executions: {
        anyOf: [{ type: 'integer', maximum: 4, minimum: 1 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'angle_id',
      'expression_id',
      'headline',
      'body_copy',
      'visual_brief',
      'checks',
    ],
  },
  CreativeFailure: {
    properties: {
      code: { type: 'string' },
      message: { type: 'string' },
      detail: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      suggested_action: { type: 'string' },
    },
    type: 'object',
    required: ['code', 'message', 'suggested_action'],
  },
  CreativeProvenance: {
    properties: {
      image_tool: {
        anyOf: [
          { $ref: '#/components/schemas/ImageToolSpec' },
          { type: 'null' },
        ],
      },
      edit_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      crop: {
        anyOf: [
          { additionalProperties: { type: 'number' }, type: 'object' },
          { type: 'null' },
        ],
      },
      rotation: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      algorithm: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      kind: {
        type: 'string',
        enum: [
          'generated_for_plan',
          'existing_creative',
          'qc_feedback_revision',
          'image_edit',
        ],
      },
      asset_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      asset_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      product_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      origin_kind: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      execution_mode: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      content_sha256: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      original_prompt: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      original_provider: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      original_model: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      plan_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      combination: {
        anyOf: [
          { $ref: '#/components/schemas/CreativeCombination' },
          { type: 'null' },
        ],
      },
      source_asset_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      source_sha256: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      qc_review_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ai_qc_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      feedback_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      feedback_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['kind'],
  },
  CreativeQuality: {
    properties: {
      ai_qc_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
      paired_ai_qc_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
      override_note: { type: 'string', maxLength: 2000, default: '' },
      asset_version_id: { type: 'string', maxLength: 64, minLength: 1 },
      quality: {
        type: 'string',
        enum: ['approved', 'needs_changes', 'rejected'],
      },
      checks: {
        additionalProperties: {
          type: 'string',
          enum: ['pass', 'fail', 'unknown'],
        },
        type: 'object',
        maxProperties: 30,
      },
      note: { type: 'string', maxLength: 2000, minLength: 1 },
      compared_asset_version_ids: {
        items: { type: 'string' },
        type: 'array',
        maxItems: 20,
      },
      source: {
        type: 'string',
        enum: ['human_semantic_review', 'ai_assisted_human_review'],
      },
      required_checks: {
        additionalProperties: { type: 'string' },
        type: 'object',
      },
      recipe_hash: { type: 'string' },
      plan_hash: { type: 'string' },
      ai_input_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ai_policy_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      normalization_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      effective_findings: {
        items: { $ref: '#/components/schemas/EffectiveFinding' },
        type: 'array',
      },
      overrides: {
        additionalProperties: { $ref: '#/components/schemas/CheckOverride' },
        type: 'object',
      },
      paired_ai_input_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      paired_ai_policy_version: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      qc_kinds: {
        anyOf: [
          {
            additionalProperties: { type: 'string', enum: ['ai', 'human'] },
            propertyNames: { enum: ['single', 'paired'] },
            type: 'object',
          },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'asset_version_id',
      'quality',
      'checks',
      'note',
      'source',
      'required_checks',
      'recipe_hash',
      'plan_hash',
    ],
  },
  CredentialBindingSummary: {
    properties: {
      id: { type: 'string' },
      data_source_entry_id: { type: 'string' },
      credential_version_id: { type: 'string' },
      revision: { type: 'integer' },
      state: { type: 'string' },
    },
    type: 'object',
    required: [
      'id',
      'data_source_entry_id',
      'credential_version_id',
      'revision',
      'state',
    ],
    description:
      'Shape of ``datasources.bind_credential`` / ``get_active_binding``.',
  },
  DataSourceConnectionCheck: {
    properties: {
      check_id: { type: 'string' },
      data_source_entry_id: { type: 'string' },
      health: {
        type: 'string',
        enum: ['unknown', 'healthy', 'degraded', 'failed', 'revoked'],
      },
      reason_code: {
        type: 'string',
        description:
          'One of the codes in app.library.connection_checks.REASON_HEALTH.',
      },
      adapter_kind: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      adapter_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      execution_mode: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      data_source_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      credential_state: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
        description:
          'State of the bound CredentialVersion, or null when none is bound.',
      },
      checked_at: { type: 'string', format: 'date-time' },
    },
    type: 'object',
    required: [
      'check_id',
      'data_source_entry_id',
      'health',
      'reason_code',
      'adapter_kind',
      'adapter_version',
      'execution_mode',
      'data_source_version_id',
      'credential_state',
      'checked_at',
    ],
    description:
      "``GET /entries/{id}/connection-checks/{check_id}``: one check's evidence (#383).\n\nThe conclusion is ``health`` plus ``reason_code``; the Operation that ran\nthe check is ``succeeded`` whatever it found. ``execution_mode`` is the\nchecked version's, never the one a client asked for.",
  },
  'DecisionRules-Input': {
    properties: {
      business_metric: { type: 'string', enum: ['cpa', 'roas', 'ctr'] },
      threshold: {
        anyOf: [
          { type: 'number', exclusiveMinimum: 0 },
          {
            type: 'string',
            pattern:
              '^(?!^[-+.]*$)[+-]?0*(?:\\d{0,8}|(?=[\\d.]{1,15}0*$)\\d{0,8}\\.\\d{0,6}0*$)',
          },
        ],
      },
      business_goal: { type: 'string', maxLength: 2000, minLength: 1 },
      evidence_requirements: { type: 'string', maxLength: 2000, minLength: 1 },
      evaluation_schedule: { type: 'string', maxLength: 2000, minLength: 1 },
      stop_rule: { type: 'string', maxLength: 2000, minLength: 1 },
      max_small_rounds: { type: 'integer', maximum: 20, minimum: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'business_metric',
      'threshold',
      'business_goal',
      'evidence_requirements',
      'evaluation_schedule',
      'stop_rule',
      'max_small_rounds',
    ],
  },
  'DecisionRules-Output': {
    properties: {
      business_metric: { type: 'string', enum: ['cpa', 'roas', 'ctr'] },
      threshold: {
        type: 'string',
        pattern:
          '^(?!^[-+.]*$)[+-]?0*(?:\\d{0,8}|(?=[\\d.]{1,15}0*$)\\d{0,8}\\.\\d{0,6}0*$)',
      },
      business_goal: { type: 'string', maxLength: 2000, minLength: 1 },
      evidence_requirements: { type: 'string', maxLength: 2000, minLength: 1 },
      evaluation_schedule: { type: 'string', maxLength: 2000, minLength: 1 },
      stop_rule: { type: 'string', maxLength: 2000, minLength: 1 },
      max_small_rounds: { type: 'integer', maximum: 20, minimum: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'business_metric',
      'threshold',
      'business_goal',
      'evidence_requirements',
      'evaluation_schedule',
      'stop_rule',
      'max_small_rounds',
    ],
  },
  DeliveryBinding: {
    properties: {
      entry_id: { type: 'string', maxLength: 64, minLength: 1 },
      ad_account_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^(act_)?[0-9]+$',
      },
      ad_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[0-9]+$',
      },
      conditions_match: { type: 'boolean' },
      deviations: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
      },
      id: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['entry_id', 'ad_account_id', 'ad_id', 'conditions_match', 'id'],
  },
  DeliveryConditions: {
    properties: {
      audience: { type: 'string' },
      market: { type: 'string' },
      placement: { type: 'string' },
      offer: { type: 'string' },
      landing_page: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['audience', 'market', 'placement', 'offer', 'landing_page'],
  },
  DirectionAdd: {
    properties: {
      title: { type: 'string', maxLength: 120, minLength: 1 },
      what_it_tests: {
        anyOf: [{ type: 'string', maxLength: 1000 }, { type: 'null' }],
      },
      hypothesis: {
        anyOf: [{ type: 'string', maxLength: 1000 }, { type: 'null' }],
      },
      scene_description: {
        anyOf: [{ type: 'string', maxLength: 1000 }, { type: 'null' }],
      },
      copy: {
        anyOf: [{ $ref: '#/components/schemas/CopyEdit' }, { type: 'null' }],
      },
      factor_values: {
        anyOf: [
          {
            additionalProperties: {
              items: { $ref: '#/components/schemas/FactorValueRef' },
              type: 'array',
            },
            type: 'object',
          },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['title'],
    description:
      "The user's own idea; omitted factors take each factor's first value.",
  },
  DirectionChoice: {
    properties: {
      plan_id: { type: 'string', maxLength: 64, minLength: 1 },
      direction_key: { type: 'string', maxLength: 32, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['plan_id', 'direction_key'],
  },
  DirectionCopy: {
    properties: {
      headline: { type: 'string', default: '' },
      primary_text: { type: 'string', default: '' },
    },
    type: 'object',
  },
  DirectionEdit: {
    properties: {
      direction_id: { type: 'string', maxLength: 32, minLength: 1 },
      title: { anyOf: [{ type: 'string', maxLength: 120 }, { type: 'null' }] },
      what_it_tests: {
        anyOf: [{ type: 'string', maxLength: 1000 }, { type: 'null' }],
      },
      hypothesis: {
        anyOf: [{ type: 'string', maxLength: 1000 }, { type: 'null' }],
      },
      scene_description: {
        anyOf: [{ type: 'string', maxLength: 1000 }, { type: 'null' }],
      },
      copy: {
        anyOf: [{ $ref: '#/components/schemas/CopyEdit' }, { type: 'null' }],
      },
      factor_values: {
        anyOf: [
          {
            additionalProperties: {
              items: { $ref: '#/components/schemas/FactorValueRef' },
              type: 'array',
            },
            type: 'object',
          },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['direction_id'],
    description: "Edits are limited to the card's text and its factor values.",
  },
  DirectionLearning: {
    properties: {
      direction_key: { type: 'string' },
      title: { type: 'string' },
      compared_with: { type: 'string' },
      verdict: {
        type: 'string',
        enum: ['winner', 'loser', 'no_difference', 'insufficient_data'],
      },
      verdict_metric: { type: 'string', enum: ['ctr', 'cpa'] },
      flags: { items: { type: 'string' }, type: 'array' },
      sentence: { type: 'string' },
      n: { $ref: '#/components/schemas/SampleSize' },
      ctr: { $ref: '#/components/schemas/MetricComparison' },
      cpa: { $ref: '#/components/schemas/MetricComparison' },
    },
    type: 'object',
    required: [
      'direction_key',
      'title',
      'compared_with',
      'verdict',
      'verdict_metric',
      'sentence',
      'n',
      'ctr',
      'cpa',
    ],
  },
  DirectionView: {
    properties: {
      direction_id: { type: 'string' },
      ordinal: { type: 'integer' },
      title: { type: 'string' },
      what_it_tests: { type: 'string' },
      hypothesis: { type: 'string' },
      scene_description: { type: 'string' },
      copy: { $ref: '#/components/schemas/DirectionCopy' },
      factors: {
        items: { $ref: '#/components/schemas/FactorView' },
        type: 'array',
      },
      factor_values: {
        additionalProperties: { items: { type: 'string' }, type: 'array' },
        type: 'object',
      },
      combination_count: { type: 'integer' },
      combination_signatures: { items: { type: 'string' }, type: 'array' },
      selected_in: {
        additionalProperties: { type: 'integer' },
        type: 'object',
      },
      removed: { type: 'boolean' },
      user_added: { type: 'boolean' },
    },
    type: 'object',
    required: [
      'direction_id',
      'ordinal',
      'title',
      'what_it_tests',
      'hypothesis',
      'scene_description',
      'copy',
      'combination_count',
      'removed',
      'user_added',
    ],
    description:
      'A direction card. ``direction_id`` is the stable plan-local id used\nby ``revise``; ``factors`` is the folded "factors and values" section;\n``selected_in`` counts this direction\'s combinations per scale.',
  },
  DownloadGrant: {
    properties: {
      url: { type: 'string' },
      kind: { type: 'string', default: 'signed_get' },
      headers: { additionalProperties: { type: 'string' }, type: 'object' },
      content_disposition: { type: 'string' },
    },
    type: 'object',
    required: ['url', 'content_disposition'],
    description:
      'Signed capability returned by a download-intent route (spec §7.4).\n\nThe browser never sees an object key or storage credentials -- only a\nshort-lived signed ``url`` plus enough shape (``kind``, ``headers``,\n``content_disposition``) to perform the fetch and save the result.',
  },
  DownloadGrantResponse: {
    properties: {
      url: { type: 'string' },
      expires_at: { type: 'string', format: 'date-time' },
      content_disposition: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['url', 'expires_at', 'content_disposition'],
  },
  DraftSummary: {
    properties: {
      id: { type: 'string' },
      entry_id: { type: 'string' },
      revision: { type: 'integer' },
      state: { type: 'string' },
      content_schema_version: { type: 'integer', default: 1 },
      base_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      origin_command: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      content: { additionalProperties: true, type: 'object' },
      validation: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['id', 'entry_id', 'revision', 'state'],
    description:
      'Shape of ``service._draft_dict`` -- also the ``bind`` command result.',
  },
  DuplicateAnalysisResponse: {
    properties: {
      experiment_id: { type: 'string' },
      analyzed_revision: { type: 'integer' },
      candidate_set_hash: { type: 'string', maxLength: 64, minLength: 64 },
      has_duplicates: { type: 'boolean' },
      duplicate_groups: {
        items: { $ref: '#/components/schemas/DuplicateGroup' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'experiment_id',
      'analyzed_revision',
      'candidate_set_hash',
      'has_duplicates',
    ],
  },
  DuplicateGroup: {
    properties: {
      duplicate_group_hash: { type: 'string', maxLength: 64, minLength: 64 },
      dimension_key: { type: 'string' },
      concept_key_id: { type: 'string' },
      concept_key_name: { type: 'string' },
      members: {
        items: { $ref: '#/components/schemas/DuplicateGroupMember' },
        type: 'array',
      },
      allowed_actions: { items: { type: 'string' }, type: 'array' },
      remap_targets: {
        items: { $ref: '#/components/schemas/RemapTarget' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'duplicate_group_hash',
      'dimension_key',
      'concept_key_id',
      'concept_key_name',
    ],
  },
  DuplicateGroupMember: {
    properties: {
      source_entry_id: { type: 'string' },
      source_candidate_fingerprint: {
        type: 'string',
        maxLength: 64,
        minLength: 64,
      },
      asset_version_id: { type: 'string' },
      display_label: { type: 'string' },
    },
    type: 'object',
    required: [
      'source_entry_id',
      'source_candidate_fingerprint',
      'asset_version_id',
      'display_label',
    ],
  },
  EditProject: {
    properties: {
      command: {
        type: 'string',
        enum: [
          'save_understanding',
          'confirm_understanding',
          'save_plan',
          'confirm_plan',
          'revise_source',
        ],
      },
      understanding: {
        anyOf: [
          { $ref: '#/components/schemas/Understanding' },
          { type: 'null' },
        ],
      },
      plan: {
        anyOf: [{ $ref: '#/components/schemas/TestPlan' }, { type: 'null' }],
      },
      source_text: {
        anyOf: [{ type: 'string', maxLength: 16000 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['command'],
  },
  EffectiveFinding: {
    properties: {
      check_id: { type: 'string', maxLength: 64, minLength: 1 },
      status: { type: 'string', enum: ['pass', 'fail', 'unknown'] },
      confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
      visual_evidence: { type: 'string', maxLength: 320, minLength: 1 },
      rationale: { type: 'string', maxLength: 320, minLength: 1 },
      suggested_fix: { type: 'string', maxLength: 320 },
      fact_ids: { items: { type: 'string' }, type: 'array', maxItems: 10 },
      model_status: { type: 'string', enum: ['pass', 'fail', 'unknown'] },
      gate_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      unsupported_claims: { items: { type: 'string' }, type: 'array' },
      rendered_text_diff: {
        items: { $ref: '#/components/schemas/RenderedTextDifference' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'check_id',
      'status',
      'confidence',
      'visual_evidence',
      'rationale',
      'suggested_fix',
      'fact_ids',
      'model_status',
    ],
  },
  EffectiveReport: {
    properties: {
      summary: { type: 'string' },
      findings: {
        items: { $ref: '#/components/schemas/EffectiveFinding' },
        type: 'array',
      },
      source: {
        type: 'string',
        enum: ['ai_visual_suggestion', 'controlled_mock'],
      },
      normalization_version: { type: 'string' },
      quality_suggestion: {
        type: 'string',
        enum: ['needs_changes', 'needs_confirmation', 'ready_to_confirm'],
      },
      human_approved: { type: 'boolean', const: false },
      provider_receipt: {
        anyOf: [
          { $ref: '#/components/schemas/ProviderReceipt' },
          { type: 'null' },
        ],
      },
      image_evidence: {
        items: { $ref: '#/components/schemas/ImageEvidence' },
        type: 'array',
      },
      rendered_text: {
        anyOf: [
          { $ref: '#/components/schemas/RenderedText' },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'summary',
      'findings',
      'source',
      'normalization_version',
      'quality_suggestion',
      'human_approved',
    ],
  },
  EligibleLocalizationSource: {
    properties: {
      evidence_origin: { type: 'string', default: 'legacy_experiment' },
      asset_version_id: { type: 'string' },
      decision_id: { type: 'string' },
      experiment_id: { type: 'string' },
      experiment_version_id: { type: 'string' },
      experiment_version_candidate_id: { type: 'string' },
      source_generation_mode: { type: 'string' },
      experiment_evidence_mode: { type: 'string' },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      source_market: { type: 'string' },
      placement: { type: 'string' },
      source_market_snapshot: {
        anyOf: [
          { additionalProperties: { type: 'string' }, type: 'object' },
          { type: 'null' },
        ],
      },
      source_context_problem: {
        anyOf: [
          { $ref: '#/components/schemas/LocalizationSourceContextProblem' },
          { type: 'null' },
        ],
      },
      reusable_inventories: {
        items: { $ref: '#/components/schemas/LocalizationReusableInventory' },
        type: 'array',
      },
      asset: { additionalProperties: true, type: 'object' },
      product: { additionalProperties: true, type: 'object' },
      offer: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: [
      'asset_version_id',
      'decision_id',
      'experiment_id',
      'experiment_version_id',
      'experiment_version_candidate_id',
      'source_generation_mode',
      'experiment_evidence_mode',
      'product_version_id',
      'offer_version_id',
      'source_market',
      'placement',
    ],
  },
  EnrollmentActivateRequest: {
    properties: {
      activation_preview_id: { type: 'string', maxLength: 64, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['activation_preview_id'],
    description:
      'Issue #86 Task 11: the only field a caller may supply is the id of an\nalready-consumed-server-side ``ScaleActivationPreviewRecord`` --\n``delivery_config``/``episode_context`` (the pre-hardening shape a\nbrowser could use to fabricate any lineage/evidence it liked) are\nrejected as unknown fields by ``_Base``\'s ``extra="forbid"``, surfacing\nas a 422.',
  },
  EnrollmentCloseRequest: {
    properties: { reason: { type: 'string', default: 'user_requested' } },
    additionalProperties: false,
    type: 'object',
  },
  EnrollmentCreateRequest: {
    properties: { enrollment_candidate_id: { type: 'string', minLength: 1 } },
    additionalProperties: false,
    type: 'object',
    required: ['enrollment_candidate_id'],
    description:
      'Issue #86 Task 9: the only field a caller may supply is the id of an\nalready-proven ``ScaleEnrollmentCandidateRecord`` -- every raw Winner/\nDeliveryTarget identifier a pre-#86 client sent directly (and\n``creator_id``, which the server now derives exclusively from the\nverified ``TenantContext``) is rejected as an unknown field by\n``_Base``\'s ``extra="forbid"``, surfacing as a 422.',
  },
  EnrollmentEvidenceResponse: {
    properties: {
      enrollment_id: { type: 'string' },
      episode_id: { type: 'string' },
      evaluation_id: { type: 'string' },
      snapshot_id: { type: 'string' },
      policy_version_id: { type: 'string' },
      target_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      algorithm_version: { type: 'string' },
      metric_semantic_contract_set_hash: { type: 'string' },
      maturity: { type: 'string' },
      data_quality_state: { type: 'string' },
      health: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      health_reason: { type: 'string' },
      healthy_evaluations: { type: 'integer' },
      streak_counters: {
        items: { additionalProperties: true, type: 'object' },
        type: 'array',
      },
      safe_watermark: { type: 'string', format: 'date' },
      last_complete_local_day: { type: 'string', format: 'date' },
      last_sync_at: { type: 'string', format: 'date-time' },
      freshness_reference_at: { type: 'string', format: 'date-time' },
      complete_local_days_in_episode: { type: 'integer' },
      active_delivery_days_in_episode: { type: 'integer' },
      current_window: { $ref: '#/components/schemas/EvidenceWindowResponse' },
      baseline_window: { $ref: '#/components/schemas/EvidenceWindowResponse' },
      rolling_window: { $ref: '#/components/schemas/EvidenceWindowResponse' },
      quality_issues: {
        items: { $ref: '#/components/schemas/EvidenceQualityIssueResponse' },
        type: 'array',
      },
      signals: {
        items: { $ref: '#/components/schemas/EvidenceSignalResponse' },
        type: 'array',
      },
      cursor_as_of_local_day: { type: 'string', format: 'date' },
      cursor_source_revision: { type: 'integer' },
      cursor_semantic_event_sequence: { type: 'integer' },
      cursor_lineage_generation: { type: 'integer' },
      revision: { type: 'integer' },
      updated_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'enrollment_id',
      'episode_id',
      'evaluation_id',
      'snapshot_id',
      'policy_version_id',
      'target_version_id',
      'algorithm_version',
      'metric_semantic_contract_set_hash',
      'maturity',
      'data_quality_state',
      'health',
      'health_reason',
      'healthy_evaluations',
      'streak_counters',
      'safe_watermark',
      'last_complete_local_day',
      'last_sync_at',
      'freshness_reference_at',
      'complete_local_days_in_episode',
      'active_delivery_days_in_episode',
      'current_window',
      'baseline_window',
      'rolling_window',
      'quality_issues',
      'signals',
      'cursor_as_of_local_day',
      'cursor_source_revision',
      'cursor_semantic_event_sequence',
      'cursor_lineage_generation',
      'revision',
      'updated_at',
    ],
    description:
      "Current evidence/readiness for an Enrollment's active Episode --\nthe accepted-Evaluation projection (spec §3.4 step 3/4), not a raw\nevidence snapshot. Reflects only a projection-eligible result that has\nalready won the structured cursor CAS.",
  },
  EnrollmentListResponse: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/EnrollmentResponse' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['items'],
  },
  EnrollmentPauseRequest: {
    properties: { reason: { type: 'string', default: 'user_requested' } },
    additionalProperties: false,
    type: 'object',
  },
  EnrollmentResponse: {
    properties: {
      id: { type: 'string' },
      meta_mode: { type: 'string' },
      meta_connection_id: { type: 'string' },
      scenario_scope_key: { type: 'string' },
      ad_account_id: { type: 'string' },
      winner_decision_id: { type: 'string' },
      winner_confirmation_id: { type: 'string' },
      asset_version_id: { type: 'string' },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      market_code: { type: 'string' },
      landing_page_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      market_asset_binding_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      campaign_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ad_set_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ad_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      remote_binding_key: { type: 'string' },
      lifecycle: { type: 'string' },
      transition_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      current_episode_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      creator_id: { type: 'string' },
      original_initiator_id: { type: 'string' },
      revision: { type: 'integer' },
      evaluation_cancellation_generation: { type: 'integer' },
      created_at: { type: 'string', format: 'date-time' },
      updated_at: { type: 'string', format: 'date-time' },
      closed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibilityModel' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'meta_mode',
      'meta_connection_id',
      'scenario_scope_key',
      'ad_account_id',
      'winner_decision_id',
      'winner_confirmation_id',
      'asset_version_id',
      'product_version_id',
      'offer_version_id',
      'market_code',
      'landing_page_version_id',
      'market_asset_binding_id',
      'campaign_remote_id',
      'ad_set_remote_id',
      'ad_remote_id',
      'remote_binding_key',
      'lifecycle',
      'transition_reason',
      'current_episode_id',
      'creator_id',
      'original_initiator_id',
      'revision',
      'evaluation_cancellation_generation',
      'created_at',
      'updated_at',
      'closed_at',
      'allowed_actions',
    ],
  },
  EnrollmentResumeRequest: {
    properties: {
      activation_preview_id: { type: 'string', maxLength: 64, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['activation_preview_id'],
    description:
      'Issue #86 Task 11: same preview-only shape as\n``EnrollmentActivateRequest`` -- resume cannot bypass the preview\nprotocol (spec §5.3), even when it will end up continuing an\nalready-open Episode untouched.',
  },
  EpisodeResponse: {
    properties: {
      id: { type: 'string' },
      enrollment_id: { type: 'string' },
      meta_mode: { type: 'string' },
      phase: { type: 'string' },
      started_at: { type: 'string', format: 'date-time' },
      started_local_day: { type: 'string', format: 'date' },
      ended_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      closure_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      delivery_evidence_hash: { type: 'string' },
      delivery_comparability_hash: { type: 'string' },
      metric_semantic_contract_set_id: { type: 'string' },
      timezone_name: { type: 'string' },
      currency: { type: 'string' },
      attribution_semantics_hash: { type: 'string' },
      revision: { type: 'integer' },
      projection_revision: { type: 'integer' },
      accepted_evaluation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibilityModel' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'enrollment_id',
      'meta_mode',
      'phase',
      'started_at',
      'started_local_day',
      'ended_at',
      'closure_reason',
      'delivery_evidence_hash',
      'delivery_comparability_hash',
      'metric_semantic_contract_set_id',
      'timezone_name',
      'currency',
      'attribution_semantics_hash',
      'revision',
      'projection_revision',
      'accepted_evaluation_id',
      'allowed_actions',
    ],
  },
  EraseRegion: {
    properties: {
      x: { type: 'number', exclusiveMaximum: 1, minimum: 0 },
      y: { type: 'number', exclusiveMaximum: 1, minimum: 0 },
      width: { type: 'number', maximum: 1, exclusiveMinimum: 0 },
      height: { type: 'number', maximum: 1, exclusiveMinimum: 0 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['x', 'y', 'width', 'height'],
  },
  EraseStroke: {
    properties: {
      width: { type: 'number', maximum: 0.5, minimum: 0.002 },
      points: {
        items: { $ref: '#/components/schemas/BrushPoint' },
        type: 'array',
        maxItems: 512,
        minItems: 1,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['width', 'points'],
  },
  'EvaluationProtocol-Input': {
    properties: {
      version: {
        type: 'string',
        const: 'marketing-eval-v1',
        default: 'marketing-eval-v1',
      },
      min_days: { type: 'integer', maximum: 90, minimum: 3, default: 7 },
      min_impressions: {
        type: 'integer',
        maximum: 100000000,
        minimum: 100,
        default: 1000,
      },
      min_clicks: {
        type: 'integer',
        maximum: 10000000,
        minimum: 1,
        default: 20,
      },
      min_purchases: {
        type: 'integer',
        maximum: 1000000,
        minimum: 5,
        default: 20,
      },
      min_spend: {
        anyOf: [
          { type: 'number', maximum: 10000000, minimum: 1 },
          { type: 'string', pattern: '^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$' },
        ],
        default: '20',
      },
      alpha: {
        anyOf: [
          { type: 'number', maximum: 0.1, minimum: 0.001 },
          { type: 'string', pattern: '^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$' },
        ],
        default: '0.05',
      },
      attribution_lag_days: {
        type: 'integer',
        maximum: 30,
        minimum: 2,
        default: 8,
      },
      allocation: {
        type: 'string',
        enum: ['equal_budget', 'observational'],
        default: 'equal_budget',
      },
      max_spend_ratio: {
        anyOf: [
          { type: 'number', maximum: 4, minimum: 1 },
          { type: 'string', pattern: '^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$' },
        ],
        default: '2',
      },
      checkpoint: {
        type: 'string',
        const: 'end_of_window',
        default: 'end_of_window',
      },
    },
    additionalProperties: false,
    type: 'object',
  },
  'EvaluationProtocol-Output': {
    properties: {
      version: {
        type: 'string',
        const: 'marketing-eval-v1',
        default: 'marketing-eval-v1',
      },
      min_days: { type: 'integer', maximum: 90, minimum: 3, default: 7 },
      min_impressions: {
        type: 'integer',
        maximum: 100000000,
        minimum: 100,
        default: 1000,
      },
      min_clicks: {
        type: 'integer',
        maximum: 10000000,
        minimum: 1,
        default: 20,
      },
      min_purchases: {
        type: 'integer',
        maximum: 1000000,
        minimum: 5,
        default: 20,
      },
      min_spend: {
        type: 'string',
        pattern: '^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$',
        default: '20',
      },
      alpha: {
        type: 'string',
        pattern: '^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$',
        default: '0.05',
      },
      attribution_lag_days: {
        type: 'integer',
        maximum: 30,
        minimum: 2,
        default: 8,
      },
      allocation: {
        type: 'string',
        enum: ['equal_budget', 'observational'],
        default: 'equal_budget',
      },
      max_spend_ratio: {
        type: 'string',
        pattern: '^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$',
        default: '2',
      },
      checkpoint: {
        type: 'string',
        const: 'end_of_window',
        default: 'end_of_window',
      },
    },
    additionalProperties: false,
    type: 'object',
  },
  EvaluationResult: {
    properties: {
      purchases_reported: { type: 'boolean', default: false },
      id: { type: 'string' },
      kind: { type: 'string', enum: ['group', 'asset'] },
      label: { type: 'string' },
      entry_ids: { items: { type: 'string' }, type: 'array' },
      entry_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      metric: { type: 'string', enum: ['cpa', 'roas', 'ctr'] },
      estimate: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      interval_low: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      interval_high: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reason_codes: { items: { type: 'string' }, type: 'array' },
      totals: { $ref: '#/components/schemas/MetricTotals' },
      business_status: {
        type: 'string',
        enum: ['passes', 'does_not_pass', 'uncertain', 'insufficient'],
      },
      conclusion_scope: {
        type: 'string',
        enum: ['asset', 'tested_group_of_combinations'],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'kind',
      'label',
      'entry_ids',
      'entry_id',
      'metric',
      'estimate',
      'interval_low',
      'interval_high',
      'reason_codes',
      'totals',
      'business_status',
      'conclusion_scope',
    ],
  },
  EvidenceAssetOption: {
    properties: {
      asset_version_id: { type: 'string' },
      label: { type: 'string' },
      localization: { type: 'boolean' },
      scale: { type: 'boolean' },
      scope: {
        type: 'string',
        enum: ['repeatable_combination', 'exact_asset_only'],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['asset_version_id', 'label', 'localization', 'scale', 'scope'],
  },
  EvidenceBasis: {
    properties: {
      fact_ids: {
        items: {
          type: 'string',
          maxLength: 64,
          minLength: 1,
          pattern: '^[a-zA-Z0-9_-]+$',
        },
        type: 'array',
        maxItems: 30,
      },
      gap: { type: 'string', maxLength: 2000, default: '' },
    },
    additionalProperties: false,
    type: 'object',
    description:
      'Confirmed-understanding facts, or an explicit gap; inferences are not evidence.',
  },
  EvidenceHandoffEnvelope: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/EvidenceHandoffOptions' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  EvidenceHandoffOptions: {
    properties: {
      evaluation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      assets: {
        items: { $ref: '#/components/schemas/EvidenceAssetOption' },
        type: 'array',
      },
      offers: {
        items: { $ref: '#/components/schemas/EvidenceOfferOption' },
        type: 'array',
      },
      existing: {
        items: { $ref: '#/components/schemas/ExistingEvidenceHandoff' },
        type: 'array',
      },
      evidence_source: { type: 'string', const: 'user_supplied_delivery' },
      no_meta_writes: { type: 'boolean', const: true },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'evaluation_id',
      'assets',
      'offers',
      'existing',
      'evidence_source',
      'no_meta_writes',
    ],
  },
  EvidenceOfferOption: {
    properties: {
      id: { type: 'string' },
      label: { type: 'string' },
      product: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      context: { $ref: '#/components/schemas/EvidenceSourceContext' },
      item: {
        anyOf: [
          {
            additionalProperties: { $ref: '#/components/schemas/JsonValue' },
            type: 'object',
          },
          { type: 'null' },
        ],
      },
      landing_page: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'label', 'product', 'context', 'item', 'landing_page'],
  },
  EvidenceQualityIssueResponse: {
    properties: {
      code: { type: 'string' },
      severity: { type: 'string' },
      is_blocking: { type: 'boolean' },
      contract_type: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      window_role: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      local_day: {
        anyOf: [{ type: 'string', format: 'date' }, { type: 'null' }],
      },
      segment_key: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      detail: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      recovery_action: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'code',
      'severity',
      'is_blocking',
      'contract_type',
      'window_role',
      'local_day',
      'segment_key',
      'detail',
      'recovery_action',
    ],
  },
  EvidenceSignalResponse: {
    properties: {
      signal_id: { type: 'string' },
      state: { type: 'string' },
      expected: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      observed: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      interval_lower: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      interval_upper: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      threshold: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      p_value: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      adjusted_q_value: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reason_code: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'signal_id',
      'state',
      'expected',
      'observed',
      'interval_lower',
      'interval_upper',
      'threshold',
      'p_value',
      'adjusted_q_value',
      'reason_code',
    ],
  },
  EvidenceSourceContext: {
    properties: {
      market: { type: 'string' },
      placement: { type: 'string' },
      locale: { type: 'string' },
      country: { type: 'string' },
      currency: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['market', 'placement', 'locale', 'country', 'currency'],
  },
  EvidenceWindowResponse: {
    properties: {
      role: { type: 'string' },
      start_local_day: { type: 'string', format: 'date' },
      end_local_day: { type: 'string', format: 'date' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['role', 'start_local_day', 'end_local_day'],
  },
  ExclusionSummary: {
    properties: {
      excluded_count: { type: 'integer' },
      reasons: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
    required: ['excluded_count'],
    description: 'Summary of excluded combinations in a preview.',
  },
  ExistingEvidenceHandoff: {
    properties: {
      decision_id: { type: 'string' },
      asset_version_id: { type: 'string' },
      evaluation_id: { type: 'string' },
      current: { type: 'boolean' },
      scope: {
        type: 'string',
        enum: ['repeatable_combination', 'exact_asset_only'],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'decision_id',
      'asset_version_id',
      'evaluation_id',
      'current',
      'scope',
    ],
  },
  Expansion: {
    properties: {
      stage: { type: 'string' },
      scale: { type: 'string' },
      combinations: { type: 'integer' },
      reused_combinations: { type: 'integer' },
      variants: { type: 'integer' },
      images: { type: 'integer' },
      max_cost: { $ref: '#/components/schemas/MaxCost' },
      sample_run_in_progress: { type: 'boolean', default: false },
      already_run: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'stage',
      'scale',
      'combinations',
      'reused_combinations',
      'variants',
      'images',
      'max_cost',
    ],
    description:
      'What ``POST /runs {stage: "full"}`` would generate now: the plan\'s\nsmall/full selection minus combinations the sample Run already produced\n(``reused_combinations``, never paid twice).',
  },
  ExperimentBindingMutation: {
    properties: {
      candidate_id: { type: 'string' },
      ad_account_id: { type: 'string' },
      ad_id: { type: 'string' },
    },
    type: 'object',
    required: ['candidate_id', 'ad_account_id', 'ad_id'],
  },
  ExperimentCandidate: {
    properties: {
      id: { type: 'string' },
      position: { type: 'integer' },
      asset_version_id: { type: 'string' },
      candidate_hash: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'sandbox', 'mock'] },
      marketing_vars: { additionalProperties: true, type: 'object' },
      binding: {
        anyOf: [
          { $ref: '#/components/schemas/ExperimentCandidateBinding' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'id',
      'position',
      'asset_version_id',
      'candidate_hash',
      'execution_mode',
    ],
  },
  ExperimentCandidateBinding: {
    properties: {
      ad_account_id: { type: 'string' },
      ad_id: { type: 'string' },
      source: { type: 'string' },
    },
    type: 'object',
    required: ['ad_account_id', 'ad_id', 'source'],
  },
  ExperimentConfirmationResult: {
    properties: {
      confirmation_id: { type: 'string' },
      decisions: {
        items: { $ref: '#/components/schemas/ExperimentDecision' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['confirmation_id'],
  },
  ExperimentContextMutation: {
    properties: {
      revision: { type: 'integer' },
      draft_context: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['revision', 'draft_context'],
  },
  ExperimentDecision: {
    properties: {
      id: { type: 'string' },
      experiment_id: { type: 'string' },
      verdict: { type: 'string' },
      metric: { type: 'string' },
      estimate: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      interval_low: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      interval_high: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      candidate_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      proposal_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confirmation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reason_codes: { items: { type: 'string' }, type: 'array' },
      confirmed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
    },
    type: 'object',
    required: ['id', 'experiment_id', 'verdict', 'metric'],
  },
  ExperimentDetail: {
    properties: {
      id: { type: 'string' },
      product_id: { type: 'string' },
      name: { type: 'string' },
      state: { type: 'string' },
      revision: { type: 'integer' },
      record_schema_version: { type: 'integer' },
      draft_context: { additionalProperties: true, type: 'object' },
      frozen_snapshot_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { type: 'string', format: 'date-time' },
      frozen_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      started_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      closed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      frozen_context: {
        anyOf: [
          { $ref: '#/components/schemas/ExperimentFrozenContext' },
          { type: 'null' },
        ],
      },
      entries: {
        items: { $ref: '#/components/schemas/ExperimentEntry' },
        type: 'array',
      },
      candidates: {
        items: { $ref: '#/components/schemas/ExperimentCandidate' },
        type: 'array',
      },
      judgements: {
        items: { $ref: '#/components/schemas/ExperimentJudgement' },
        type: 'array',
      },
      proposals: {
        items: { $ref: '#/components/schemas/ExperimentProposal' },
        type: 'array',
      },
      decisions: {
        items: { $ref: '#/components/schemas/ExperimentDecision' },
        type: 'array',
      },
      concept_resolution_evidence: {
        anyOf: [
          { $ref: '#/components/schemas/ConceptResolutionEvidence' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'id',
      'product_id',
      'name',
      'state',
      'revision',
      'record_schema_version',
      'created_at',
    ],
  },
  ExperimentEntry: {
    properties: {
      id: { type: 'string' },
      position: { type: 'integer' },
      asset_version_id: { type: 'string' },
      marketing_vars: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['id', 'position', 'asset_version_id'],
  },
  ExperimentEntryMutation: {
    properties: {
      id: { type: 'string' },
      experiment_id: { type: 'string' },
      asset_version_id: { type: 'string' },
      position: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      marketing_vars: { additionalProperties: true, type: 'object' },
      revision: { type: 'integer' },
    },
    type: 'object',
    required: ['id', 'experiment_id', 'asset_version_id', 'revision'],
  },
  ExperimentFrozenContext: {
    properties: {
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      market: { type: 'string' },
      placement: { type: 'string' },
      landing_page_evidence_hash: { type: 'string' },
      audience: { additionalProperties: true, type: 'object' },
      daily_budget_amount: { type: 'string' },
      total_budget_cap: { type: 'string' },
      budget_currency: { type: 'string' },
      schedule_start_at: { type: 'string', format: 'date-time' },
      schedule_end_at: { type: 'string', format: 'date-time' },
      schedule_timezone: { type: 'string' },
      attribution_click_window_days: { type: 'integer' },
      attribution_view_window_days: { type: 'integer' },
      statistical_plan: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: [
      'product_version_id',
      'offer_version_id',
      'market',
      'placement',
      'landing_page_evidence_hash',
      'daily_budget_amount',
      'total_budget_cap',
      'budget_currency',
      'schedule_start_at',
      'schedule_end_at',
      'schedule_timezone',
      'attribution_click_window_days',
      'attribution_view_window_days',
    ],
  },
  ExperimentJudgement: {
    properties: {
      id: { type: 'string' },
      experiment_id: { type: 'string' },
      algorithm_version: { type: 'string' },
      as_of: { type: 'string', format: 'date' },
      snapshot_hash: { type: 'string' },
      result: { additionalProperties: true, type: 'object' },
      created_at: { type: 'string', format: 'date-time' },
    },
    type: 'object',
    required: [
      'id',
      'experiment_id',
      'algorithm_version',
      'as_of',
      'snapshot_hash',
      'created_at',
    ],
  },
  ExperimentJudgementResult: {
    properties: {
      judgement: { $ref: '#/components/schemas/ExperimentJudgement' },
      proposal_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      proposal_state: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['judgement'],
  },
  ExperimentPage: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/ExperimentSummary' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
  },
  ExperimentProposal: {
    properties: {
      id: { type: 'string' },
      judgement_id: { type: 'string' },
      proposal_hash: { type: 'string' },
      state: { type: 'string' },
      sequence: { type: 'integer' },
      proposed_entries: {
        items: { additionalProperties: true, type: 'object' },
        type: 'array',
      },
      evidence: { additionalProperties: true, type: 'object' },
      created_at: { type: 'string', format: 'date-time' },
    },
    type: 'object',
    required: [
      'id',
      'judgement_id',
      'proposal_hash',
      'state',
      'sequence',
      'created_at',
    ],
  },
  ExperimentSummary: {
    properties: {
      id: { type: 'string' },
      name: { type: 'string' },
      state: { type: 'string' },
      revision: { type: 'integer' },
      record_schema_version: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      created_at: { type: 'string', format: 'date-time' },
      frozen_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      closed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
    },
    type: 'object',
    required: ['id', 'name', 'state', 'revision', 'created_at'],
  },
  ExperimentTimeline: {
    properties: {
      head_sequence: { type: 'integer' },
      items: {
        items: { $ref: '#/components/schemas/ExperimentTimelineEvent' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['head_sequence'],
  },
  ExperimentTimelineEvent: {
    properties: {
      id: { type: 'string' },
      timeline_sequence: { type: 'integer' },
      event_type: { type: 'string' },
      source_resource_type: { type: 'string' },
      source_resource_id: { type: 'string' },
      actor_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      summary: { additionalProperties: true, type: 'object' },
      observed_at: { type: 'string', format: 'date-time' },
    },
    type: 'object',
    required: [
      'id',
      'timeline_sequence',
      'event_type',
      'source_resource_type',
      'source_resource_id',
      'observed_at',
    ],
  },
  ExplanationData: {
    properties: {
      provider_receipt: {
        anyOf: [
          { $ref: '#/components/schemas/TextProviderReceipt' },
          { type: 'null' },
        ],
      },
      report: { $ref: '#/components/schemas/WorkflowExplanationReport' },
      paragraphs: {
        items: { $ref: '#/components/schemas/ExplanationParagraph' },
        type: 'array',
      },
      evaluation_id: { type: 'string' },
      input_hash: { type: 'string' },
      result_hash: { type: 'string' },
      question: { type: 'string' },
      conditions: { $ref: '#/components/schemas/DeliveryConditions' },
      limitations: { items: { type: 'string' }, type: 'array' },
      evidence_status: {
        type: 'string',
        enum: ['simulated', 'user_supplied_delivery', 'not_supplied'],
      },
      next_action: { type: 'string' },
      confirmed_combinations: {
        items: { $ref: '#/components/schemas/ConfirmedCombination' },
        type: 'array',
      },
      policy: { type: 'string', const: 'marketing-explanation-v1' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'report',
      'paragraphs',
      'evaluation_id',
      'input_hash',
      'result_hash',
      'question',
      'conditions',
      'limitations',
      'evidence_status',
      'next_action',
      'confirmed_combinations',
      'policy',
    ],
  },
  ExplanationFocus: {
    properties: {
      evidence_id: { type: 'string' },
      interpretation: {
        type: 'string',
        enum: [
          'retain_asset',
          'not_success',
          'investigate',
          'compare_scope',
          'repeat_execution',
          'close',
          'explore_expression',
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['evidence_id', 'interpretation'],
  },
  ExplanationParagraph: {
    properties: {
      evidence_id: { type: 'string' },
      object_id: { type: 'string' },
      observation: { type: 'string' },
      explanation: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['evidence_id', 'object_id', 'observation', 'explanation'],
  },
  ExportCreateRequest: {
    properties: {
      enrollment_id: { type: 'string', minLength: 1 },
      episode_id: { type: 'string', minLength: 1 },
      export_kind: { type: 'string', minLength: 1 },
      snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      evaluation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['enrollment_id', 'episode_id', 'export_kind'],
  },
  ExportDownloadIntentResponse: {
    properties: {
      intent_id: { type: 'string' },
      expires_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['intent_id', 'expires_at'],
  },
  ExportManifest: {
    properties: {
      package_id: { type: 'string' },
      execution_mode: { type: 'string' },
      entries: {
        items: { $ref: '#/components/schemas/ExportManifestEntry' },
        type: 'array',
      },
      total_versions: { type: 'integer' },
    },
    type: 'object',
    required: ['package_id', 'execution_mode', 'total_versions'],
    description:
      '``GET /generation/exports/{id}/manifest`` (export.build_manifest).',
  },
  ExportManifestEntry: {
    properties: {
      asset_version_id: { type: 'string' },
      asset_id: { type: 'string' },
      content_sha256: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      byte_size: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      mime_type: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      origin_kind: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      execution_mode: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      producing_run_item_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      provider_attempt_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      requested_seed: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      effective_seed: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      seed_unavailable_reason: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
    },
    type: 'object',
    required: ['asset_version_id', 'asset_id'],
  },
  ExportPackageDetail: {
    properties: {
      package_id: { type: 'string' },
      operation_id: { type: 'string' },
      execution_mode: { type: 'string' },
      state: { type: 'string' },
      revision: { type: 'integer' },
      byte_size: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      zip_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'package_id',
      'operation_id',
      'execution_mode',
      'state',
      'revision',
    ],
    description:
      "``GET /generation/exports/{id}`` result (export.get_export_package).\n\nRecovery/polling shape (Issue #49 Task 9): the browser's ``useExportFlow``\npolls this alongside the Operation to recover state across a refresh.\n``state`` here is the ExportPackage's own projection\n(``pending``/``building``/``ready``/``failed``/``expired``), unlike the\n202 response's Operation-state ``state``.",
  },
  ExportPackageQueued: {
    properties: {
      package_id: { type: 'string' },
      operation_id: { type: 'string' },
      state: { type: 'string' },
    },
    type: 'object',
    required: ['package_id', 'operation_id', 'state'],
    description:
      '``POST /generation/exports`` 202 result (Issue #49 Task 9).\n\n``state`` is the authorizing Operation\'s state (``"queued"`` for a\nfresh call, or whatever state a replayed Operation has already reached)\n-- not the ExportPackage\'s own ``pending``/``building``/``ready``/\n``failed`` projection, which ``GET /generation/exports/{id}``\n(``ExportPackageDetail``) surfaces instead.',
  },
  ExportResponse: {
    properties: {
      id: { type: 'string' },
      operation_id: { type: 'string' },
      enrollment_id: { type: 'string' },
      episode_id: { type: 'string' },
      export_kind: { type: 'string' },
      requested_by: { type: 'string' },
      requested_at: { type: 'string', format: 'date-time' },
      completed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      result_object_reference: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'operation_id',
      'enrollment_id',
      'episode_id',
      'export_kind',
      'requested_by',
      'requested_at',
      'completed_at',
      'result_object_reference',
    ],
  },
  ExportSelectorInput: {
    properties: {
      kind: { type: 'string', minLength: 1 },
      version_ids: {
        anyOf: [{ items: { type: 'string' }, type: 'array' }, { type: 'null' }],
      },
      scope: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      experiment_context_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['kind'],
    description:
      "A discriminated ``ExportSelector`` (Issue #49 Task 9, spec §0.4).\n\nNot a strict pydantic discriminated union -- ``kind`` picks which of the\nother (all-optional) fields ``app.generation.export._resolve_selector``\nactually reads, and that function is the single place that enforces\neach kind's own preconditions (e.g. ``winner_only`` requiring\n``experiment_context_id``) as a domain ``ExportError`` rather than a\n422 schema-validation error, so the response ``code`` stays a stable\ndomain vocabulary instead of a pydantic error shape.",
  },
  Expression: {
    properties: {
      id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      name: { type: 'string', maxLength: 2000, minLength: 1 },
      description: { type: 'string', maxLength: 2000, minLength: 1 },
      fit_reason: { type: 'string', maxLength: 2000, minLength: 1 },
      required_evidence: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
      },
      how_to_tell: { type: 'string', maxLength: 2000, minLength: 1 },
      visual_mechanism: { type: 'string', maxLength: 120, minLength: 1 },
      evidence_basis: { $ref: '#/components/schemas/EvidenceBasis' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'name',
      'description',
      'fit_reason',
      'how_to_tell',
      'visual_mechanism',
      'evidence_basis',
    ],
  },
  ExternalCreativeConfirmRequest: {
    properties: {
      tags: {
        items: { $ref: '#/components/schemas/TagChoice' },
        type: 'array',
        maxItems: 24,
      },
      direction: {
        anyOf: [
          { $ref: '#/components/schemas/DirectionChoice' },
          { type: 'null' },
        ],
      },
      approve: { type: 'boolean', default: true },
    },
    additionalProperties: false,
    type: 'object',
    description:
      'The reviewed tags (one per factor; ``value_id`` null or omitted factor\n= "none of these"), an optional direction, and whether confirming also\napproves the image for tests (default true).',
  },
  ExternalCreativeCreateRequest: {
    properties: {
      product_id: { type: 'string', maxLength: 64, minLength: 1 },
      upload_intent_ids: {
        items: { type: 'string' },
        type: 'array',
        maxItems: 10,
        minItems: 1,
      },
      rights: { $ref: '#/components/schemas/RightsDeclaration' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id', 'upload_intent_ids', 'rights'],
    description:
      'Finalized uploads of purpose ``external_creative`` (PNG/JPEG/WebP,\n<= 20 MB each, at most 10) for one product.',
  },
  ExternalCreativeDetail: {
    properties: {
      external_creative_id: { type: 'string' },
      product_id: { type: 'string' },
      status: { type: 'string' },
      current_step: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      execution_mode: { type: 'string' },
      filename: { type: 'string', default: '' },
      mime_type: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      byte_size: { type: 'integer', default: 0 },
      width: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      height: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      aspect_ratio: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      rights: { $ref: '#/components/schemas/RightsView' },
      asset_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      asset_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      asset_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      thumbnail: {
        anyOf: [
          { $ref: '#/components/schemas/ThumbnailLink' },
          { type: 'null' },
        ],
      },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/CreativeFailure' },
          { type: 'null' },
        ],
      },
      vocabulary: { $ref: '#/components/schemas/Vocabulary' },
      suggestion: {
        anyOf: [
          { $ref: '#/components/schemas/TagSuggestion' },
          { type: 'null' },
        ],
      },
      confirmation: {
        anyOf: [
          { $ref: '#/components/schemas/TagConfirmation' },
          { type: 'null' },
        ],
      },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'external_creative_id',
      'product_id',
      'status',
      'execution_mode',
      'rights',
      'vocabulary',
    ],
    description:
      '``status``: ``queued`` -> ``gating`` -> ``preparing_asset`` ->\n``tagging`` -> ``ready`` (suggestion available) -> ``confirmed``; or\n``rejected`` (upload gate), ``failed``, ``tagging_failed`` (the asset\nexists; tag it by hand). ``asset_code`` (``MK-XXXXXX``) is what a\nresults sheet matches.',
  },
  ExternalCreativeList: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/ExternalCreativeDetail' },
        type: 'array',
      },
    },
    type: 'object',
  },
  Fact: {
    properties: {
      id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      statement: { type: 'string', maxLength: 2000, minLength: 1 },
      source_quote: { type: 'string', maxLength: 2000, minLength: 1 },
      confirmation: {
        type: 'string',
        enum: ['unconfirmed', 'confirmed'],
        default: 'unconfirmed',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'statement', 'source_quote'],
  },
  FactorComparison: {
    properties: {
      kind: { type: 'string', const: 'factor' },
      changed: { items: { type: 'string' }, type: 'array' },
      pairs: {
        items: { $ref: '#/components/schemas/FactorPair' },
        type: 'array',
      },
      unpaired_combination_ids: { items: { type: 'string' }, type: 'array' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['kind', 'changed', 'pairs', 'unpaired_combination_ids'],
  },
  FactorLearning: {
    properties: {
      key: { type: 'string' },
      label: { type: 'string' },
      values: {
        items: { $ref: '#/components/schemas/ValueLearning' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['key', 'label'],
  },
  FactorPair: {
    properties: {
      combination_ids: { items: { type: 'string' }, type: 'array' },
      group_ids: { items: { type: 'string' }, type: 'array' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['combination_ids', 'group_ids'],
  },
  FactorValueRef: {
    properties: {
      value_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
      label: { anyOf: [{ type: 'string', maxLength: 80 }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    description:
      'An existing value (``value_id``) or a new one worded by the user (``label``).',
  },
  FactorValueView: {
    properties: {
      id: { type: 'string' },
      label: { type: 'string' },
      proposed_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['id', 'label'],
  },
  FactorView: {
    properties: {
      key: { type: 'string' },
      label: { type: 'string' },
      values: {
        items: { $ref: '#/components/schemas/FactorValueView' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['key', 'label'],
  },
  FeedbackDraft: {
    properties: {
      image_tool: {
        anyOf: [
          { $ref: '#/components/schemas/ImageToolSpec' },
          { type: 'null' },
        ],
      },
      id: { type: 'string' },
      draft_id: { type: 'string' },
      source_asset_version_id: { type: 'string' },
      combination_id: { type: 'string' },
      request_note: { type: 'string' },
      provider: { type: 'string' },
      model: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'draft_id',
      'source_asset_version_id',
      'combination_id',
      'request_note',
      'provider',
      'model',
    ],
  },
  FinalizeUploadResponse: {
    properties: {
      operation_id: { type: 'string' },
      state: {
        type: 'string',
        enum: [
          'queued',
          'running',
          'partially_succeeded',
          'succeeded',
          'failed',
          'cancelled',
          'uncertain',
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['operation_id', 'state'],
    description:
      'See the note on `UploadIntentResponse` -- the handler returns a\n`JSONResponse` directly.',
  },
  ForkInput: {
    properties: {
      display_name: { type: 'string', maxLength: 300, minLength: 1 },
    },
    type: 'object',
    required: ['display_name'],
  },
  FreezeExperimentRequest: {
    properties: {
      expected_revision: { type: 'integer', minimum: 1 },
      analyzed_candidate_set_hash: {
        type: 'string',
        maxLength: 64,
        minLength: 64,
      },
      concept_resolution_plan: {
        items: { $ref: '#/components/schemas/ConceptResolutionActionRequest' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['expected_revision', 'analyzed_candidate_set_hash'],
  },
  FreezeRound: {
    properties: {
      start_date: { type: 'string', format: 'date' },
      end_date: { type: 'string', format: 'date' },
      account_timezone: { type: 'string', maxLength: 64, default: 'UTC' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      protocol: { $ref: '#/components/schemas/EvaluationProtocol-Input' },
      attribution_window: {
        type: 'string',
        enum: [
          '1d_click',
          '7d_click',
          '1d_view',
          '1d_click_1d_view',
          '7d_click_1d_view',
        ],
        default: '7d_click_1d_view',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['start_date', 'end_date', 'execution_mode', 'protocol'],
  },
  FrozenAngle: {
    properties: {
      id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      proposition: { type: 'string', maxLength: 2000, minLength: 1 },
      rationale: { type: 'string', maxLength: 2000, minLength: 1 },
      fact_ids: {
        items: {
          type: 'string',
          maxLength: 64,
          minLength: 1,
          pattern: '^[a-zA-Z0-9_-]+$',
        },
        type: 'array',
        maxItems: 30,
      },
      inference_ids: {
        items: {
          type: 'string',
          maxLength: 64,
          minLength: 1,
          pattern: '^[a-zA-Z0-9_-]+$',
        },
        type: 'array',
        maxItems: 30,
      },
      expressions: {
        items: { $ref: '#/components/schemas/FrozenExpression' },
        type: 'array',
        maxItems: 8,
        minItems: 1,
      },
      competitor_overlap: {
        items: {
          type: 'string',
          maxLength: 64,
          minLength: 1,
          pattern: '^[a-zA-Z0-9_-]+$',
        },
        type: 'array',
        maxItems: 20,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'proposition', 'rationale', 'expressions'],
  },
  FrozenExpression: {
    properties: {
      id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      name: { type: 'string', maxLength: 2000, minLength: 1 },
      description: { type: 'string', maxLength: 2000, minLength: 1 },
      fit_reason: { type: 'string', maxLength: 2000, minLength: 1 },
      required_evidence: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
      },
      how_to_tell: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      visual_mechanism: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      evidence_basis: {
        anyOf: [
          { $ref: '#/components/schemas/EvidenceBasis' },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'name', 'description', 'fit_reason'],
    description:
      'Plans drafted before expression distinctness lack the three elements.',
  },
  FrozenTestGroup: {
    properties: {
      id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      name: { type: 'string', maxLength: 2000, minLength: 1 },
      combination_ids: {
        items: {
          type: 'string',
          maxLength: 64,
          minLength: 1,
          pattern: '^[a-zA-Z0-9_-]+$',
        },
        type: 'array',
        maxItems: 30,
        minItems: 1,
      },
      rationale: { type: 'string', maxLength: 2000, minLength: 1 },
      style: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'name', 'combination_ids', 'rationale'],
  },
  FrozenTestPlan: {
    properties: {
      stage: {
        type: 'string',
        enum: [
          'screen_angles',
          'match_expressions',
          'confirm_combination',
          'optimize_elements',
        ],
      },
      question: { type: 'string', maxLength: 2000, minLength: 1 },
      comparison_kind: {
        type: 'string',
        enum: ['whole_combinations', 'specific_factor', 'business_retest'],
      },
      changed: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
        minItems: 1,
      },
      allowed_changes: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
      },
      held_constant: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
        minItems: 1,
      },
      limitations: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
        minItems: 1,
      },
      limited_expression_coverage: { type: 'boolean', default: false },
      conclusion_scope: {
        type: 'string',
        enum: ['asset', 'combination', 'specific_factor'],
      },
      audience: { type: 'string', maxLength: 2000, minLength: 1 },
      market: { type: 'string', maxLength: 2000, minLength: 1 },
      ad_language: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      strategy_language: {
        type: 'string',
        enum: ['zh-CN', 'en-US'],
        default: 'en-US',
      },
      language_confirmation_required: { type: 'boolean', default: false },
      placement: { type: 'string', maxLength: 2000, minLength: 1 },
      offer: { type: 'string', maxLength: 2000, minLength: 1 },
      landing_page: { type: 'string', maxLength: 2000, minLength: 1 },
      baseline: { type: 'string', maxLength: 2000, minLength: 1 },
      currency: {
        type: 'string',
        enum: [
          'AED',
          'AUD',
          'BRL',
          'CAD',
          'CHF',
          'CNY',
          'EUR',
          'GBP',
          'HKD',
          'IDR',
          'INR',
          'JPY',
          'KRW',
          'MXN',
          'MYR',
          'NZD',
          'PHP',
          'PLN',
          'SAR',
          'SEK',
          'SGD',
          'THB',
          'TWD',
          'USD',
          'VND',
          'ZAR',
        ],
        default: 'USD',
      },
      media_budget: {
        type: 'string',
        pattern:
          '^(?!^[-+.]*$)[+-]?0*(?:\\d{0,12}|(?=[\\d.]{1,15}0*$)\\d{0,12}\\.\\d{0,2}0*$)',
      },
      small_round_budget: {
        type: 'string',
        pattern:
          '^(?!^[-+.]*$)[+-]?0*(?:\\d{0,12}|(?=[\\d.]{1,15}0*$)\\d{0,12}\\.\\d{0,2}0*$)',
      },
      duration_days: { type: 'integer', maximum: 90, minimum: 1 },
      allocation: { type: 'string', maxLength: 2000, minLength: 1 },
      rules: { $ref: '#/components/schemas/DecisionRules-Output' },
      executions_per_combination: {
        type: 'integer',
        maximum: 4,
        minimum: 1,
        default: 1,
      },
      angles: {
        items: { $ref: '#/components/schemas/FrozenAngle' },
        type: 'array',
        maxItems: 8,
        minItems: 1,
      },
      combinations: {
        items: { $ref: '#/components/schemas/CreativeCombination' },
        type: 'array',
        maxItems: 30,
        minItems: 1,
      },
      groups: {
        items: { $ref: '#/components/schemas/FrozenTestGroup' },
        type: 'array',
        maxItems: 12,
        minItems: 1,
      },
      plan_review: {
        items: { $ref: '#/components/schemas/ReviewItem' },
        type: 'array',
        maxItems: 20,
      },
      guiding_questions: {
        items: { $ref: '#/components/schemas/GuidingQuestion' },
        type: 'array',
        maxItems: 3,
      },
      style_variation: {
        anyOf: [{ type: 'string', enum: ['held', 'tested'] }, { type: 'null' }],
      },
      style_notes: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      brand_kit: {
        anyOf: [{ $ref: '#/components/schemas/BrandKitRef' }, { type: 'null' }],
      },
      competitor_snapshot_id: {
        anyOf: [
          {
            type: 'string',
            maxLength: 64,
            minLength: 1,
            pattern: '^[a-zA-Z0-9_-]+$',
          },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'stage',
      'question',
      'comparison_kind',
      'changed',
      'held_constant',
      'limitations',
      'conclusion_scope',
      'audience',
      'market',
      'placement',
      'offer',
      'landing_page',
      'baseline',
      'media_budget',
      'small_round_budget',
      'duration_days',
      'allocation',
      'rules',
      'angles',
      'combinations',
      'groups',
    ],
    description:
      'Historical snapshots may predate language, expression and style fields; never backfill evidence.',
  },
  GalleryFacetValue: {
    properties: { value: { type: 'string' }, count: { type: 'integer' } },
    type: 'object',
    required: ['value', 'count'],
  },
  GenerationCapabilities: {
    properties: {
      providers: { items: { type: 'string' }, type: 'array' },
      models: { additionalProperties: true, type: 'object' },
      modes: { items: { type: 'string' }, type: 'array' },
      format_limits: { additionalProperties: true, type: 'object' },
      credential_readiness: { additionalProperties: true, type: 'object' },
      llm: { additionalProperties: true, type: 'object' },
      provider_options: {
        items: { $ref: '#/components/schemas/GenerationProviderOption' },
        type: 'array',
      },
      products: {
        items: { $ref: '#/components/schemas/GenerationProductOption' },
        type: 'array',
      },
    },
    type: 'object',
    description: 'Workspace provider catalog and non-secret readiness.',
  },
  GenerationDefaultsUpdate: {
    properties: {
      provider: { type: 'string', maxLength: 64, minLength: 1 },
      model: { type: 'string', maxLength: 128, minLength: 1 },
      execution_mode: { type: 'string', enum: ['mock', 'byok'] },
      aspect_ratio: { type: 'string', maxLength: 16, minLength: 1 },
      variants: {
        additionalProperties: { type: 'integer' },
        propertyNames: { enum: ['sample', 'small', 'full'] },
        type: 'object',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'provider',
      'model',
      'execution_mode',
      'aspect_ratio',
      'variants',
    ],
  },
  GenerationDefaultsView: {
    properties: {
      provider: { type: 'string' },
      model: { type: 'string' },
      execution_mode: { type: 'string' },
      aspect_ratio: { type: 'string' },
      variants: { additionalProperties: { type: 'integer' }, type: 'object' },
      source: { type: 'string' },
      revision: { type: 'integer' },
      credential_bound: { type: 'boolean' },
      warnings: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
    required: [
      'provider',
      'model',
      'execution_mode',
      'aspect_ratio',
      'variants',
      'source',
      'revision',
      'credential_bound',
    ],
    description:
      'Effective「生成默认值」. ``source`` is ``workspace`` once an Owner\nsaved them (``revision`` >= 1), else ``computed`` (``revision`` 0).\n``warnings``: ``mock_mode`` (show the bind-a-key banner),\n``byok_credential_missing``.',
  },
  GenerationDraftDetail: {
    properties: {
      id: { type: 'string' },
      product_id: { type: 'string' },
      provider: { type: 'string' },
      model: { type: 'string' },
      execution_mode: { type: 'string' },
      state: { type: 'string' },
      revision: { type: 'integer' },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      selectors: { additionalProperties: true, type: 'object' },
      combination_strategy: { additionalProperties: true, type: 'object' },
      format_config: { additionalProperties: true, type: 'object' },
      exclusions: { additionalProperties: true, type: 'object' },
      regeneration_intent: { type: 'string', default: 'new_generation' },
      source_asset_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      source_recipe_identity: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'product_id',
      'provider',
      'model',
      'execution_mode',
      'state',
      'revision',
    ],
    description:
      '``GET /generation/drafts/{id}`` and the create/update/regenerate result.',
  },
  GenerationModelOption: {
    properties: {
      image_tools: {
        items: {
          type: 'string',
          enum: ['upscale', 'remove_background', 'erase'],
        },
        type: 'array',
      },
      id: { type: 'string' },
      image_sizes: { items: { type: 'string' }, type: 'array' },
      maximum_costs: {
        additionalProperties: { type: 'string' },
        type: 'object',
      },
      available: { type: 'boolean' },
      unavailable_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['id', 'image_sizes', 'maximum_costs', 'available'],
  },
  GenerationPreviewDetail: {
    properties: {
      purpose: {
        type: 'string',
        const: 'initial_generation',
        default: 'initial_generation',
      },
      id: { type: 'string' },
      preview_hash: { type: 'string' },
      recipe_input_hash: { type: 'string' },
      execution_mode: { type: 'string' },
      valid_until: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      draft_id: { type: 'string' },
      draft_revision: { type: 'integer' },
      invalidation_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      combination_count: { type: 'integer', default: 0 },
      call_count: { type: 'integer', default: 0 },
      maximum_cost: {
        anyOf: [{ $ref: '#/components/schemas/MaximumCost' }, { type: 'null' }],
      },
      final_prompt: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      items: {
        items: { $ref: '#/components/schemas/PreviewItemSummary' },
        type: 'array',
      },
      capability_decisions: {
        items: { $ref: '#/components/schemas/CapabilityDecision' },
        type: 'array',
      },
      exclusion_summary: {
        anyOf: [
          { $ref: '#/components/schemas/ExclusionSummary' },
          { type: 'null' },
        ],
      },
      skipped_signature_count: { type: 'integer', default: 0 },
      skipped_call_count: { type: 'integer', default: 0 },
      estimated_cost_saving: {
        anyOf: [{ $ref: '#/components/schemas/MaximumCost' }, { type: 'null' }],
      },
      strategy_applied: {
        anyOf: [
          { $ref: '#/components/schemas/StrategyApplied' },
          { type: 'null' },
        ],
      },
      combinations_dropped_by_constraints: { type: 'integer', default: 0 },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
      prompt: { type: 'string', default: '' },
      quantity: { type: 'integer', default: 1 },
      selection_maximum_cost: { type: 'string', default: '0' },
      currency: { type: 'string', default: 'USD' },
      pricing_version: { type: 'string', default: '' },
      provider: { type: 'string', default: '' },
      model: { type: 'string', default: '' },
      image_tool: {
        anyOf: [
          { $ref: '#/components/schemas/ImageToolSpec' },
          { type: 'null' },
        ],
      },
      format_config: { additionalProperties: true, type: 'object' },
      submitted_run_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'id',
      'preview_hash',
      'recipe_input_hash',
      'execution_mode',
      'draft_id',
      'draft_revision',
    ],
    description:
      '``POST /generation/drafts/{id}/previews`` and ``GET /generation/previews/{id}``.',
  },
  GenerationProductOption: {
    properties: {
      id: { type: 'string' },
      name: { type: 'string' },
      version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['id', 'name'],
  },
  GenerationProviderOption: {
    properties: {
      id: { type: 'string' },
      label: { type: 'string' },
      default_model: { type: 'string' },
      execution_mode: { type: 'string' },
      credential_ready: { type: 'boolean' },
      available: { type: 'boolean' },
      unavailable_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      aspect_ratios: { items: { type: 'string' }, type: 'array' },
      models: {
        items: { $ref: '#/components/schemas/GenerationModelOption' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'label',
      'default_model',
      'execution_mode',
      'credential_ready',
      'available',
      'aspect_ratios',
      'models',
    ],
  },
  GenerationRecoveryPreview: {
    properties: {
      purpose: {
        type: 'string',
        const: 'run_recovery',
        default: 'run_recovery',
      },
      id: { type: 'string' },
      preview_hash: { type: 'string' },
      command: { type: 'string' },
      targets: { items: { type: 'string' }, type: 'array' },
      estimated_additional_cost: { $ref: '#/components/schemas/MaximumCost' },
      valid_until: { type: 'string' },
      run_id: { type: 'string', default: '' },
      expected_run_revision: { type: 'integer', default: 0 },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'preview_hash',
      'command',
      'estimated_additional_cost',
      'valid_until',
    ],
    description:
      'POST /generation/runs/{id}/recovery-previews and\nGET /generation/previews/{id} when purpose is run_recovery.',
  },
  GenerationRunCommandSummary: {
    properties: {
      id: { type: 'string' },
      command_kind: { type: 'string' },
      operation_id: { type: 'string' },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['id', 'command_kind', 'operation_id'],
  },
  GenerationRunDetail: {
    properties: {
      id: { type: 'string' },
      product_id: { type: 'string' },
      provider: { type: 'string' },
      model: { type: 'string' },
      status: { type: 'string' },
      total: { type: 'integer' },
      done: { type: 'integer' },
      failed: { type: 'integer' },
      execution_mode: { type: 'string' },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      revision: { type: 'integer' },
      recipe_snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      commands: {
        items: { $ref: '#/components/schemas/GenerationRunCommandSummary' },
        type: 'array',
      },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'product_id',
      'provider',
      'model',
      'status',
      'total',
      'done',
      'failed',
      'execution_mode',
      'revision',
    ],
    description: '``GET /generation/runs/{id}`` (run_queries.get_run_detail).',
  },
  GenerationRunItemDetail: {
    properties: {
      id: { type: 'string' },
      ordinal: { type: 'integer' },
      state: { type: 'string' },
      combination_fingerprint: { type: 'string' },
      variant_index: { type: 'integer' },
      tags: { additionalProperties: true, type: 'object' },
      bound_asset_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      thumbnail: {
        anyOf: [
          { $ref: '#/components/schemas/AssetThumbnailLink' },
          { type: 'null' },
        ],
      },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
      combination_identity: { additionalProperties: true, type: 'object' },
      execution_mode: { type: 'string' },
      error_summary: { type: 'string', default: '' },
      error_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source_asset_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      source_run_item_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      intent_kind: { type: 'string', default: 'new_generation' },
      latest_attempt: {
        anyOf: [
          { $ref: '#/components/schemas/AttemptSummary' },
          { type: 'null' },
        ],
      },
      qc_result: {
        anyOf: [
          { $ref: '#/components/schemas/QcResultSummary' },
          { type: 'null' },
        ],
      },
      cost_evidence: {
        anyOf: [
          { $ref: '#/components/schemas/CostEvidence' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'id',
      'ordinal',
      'state',
      'combination_fingerprint',
      'variant_index',
      'execution_mode',
    ],
    description:
      '``GET /generation/runs/{id}/items/{item_id}`` (run_queries.get_run_item_detail).',
  },
  GuardrailConfirmRequest: {
    properties: {
      preview_token: { type: 'string' },
      expected_revision: { type: 'integer' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['preview_token', 'expected_revision'],
  },
  GuardrailPreviewRequest: {
    properties: {
      per_operation_limit: { type: 'string' },
      rolling_24h_limit: { type: 'string' },
      period_limit: { type: 'string' },
      outstanding_exposure_limit: { type: 'string' },
      expected_revision: { type: 'integer' },
      mfa_evidence_ref: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'per_operation_limit',
      'rolling_24h_limit',
      'period_limit',
      'outstanding_exposure_limit',
      'expected_revision',
    ],
  },
  GuidingQuestion: {
    properties: {
      id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      question: { type: 'string', maxLength: 2000, minLength: 1 },
      options: {
        items: { type: 'string', maxLength: 300, minLength: 1 },
        type: 'array',
        maxItems: 6,
        minItems: 2,
      },
      multiple: { type: 'boolean', default: false },
      why_needed: { type: 'string', maxLength: 2000, minLength: 1 },
      selected: {
        items: { type: 'string', maxLength: 300, minLength: 1 },
        type: 'array',
        maxItems: 6,
      },
      custom_answer: { type: 'string', maxLength: 1000, default: '' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'question', 'options', 'why_needed'],
  },
  HandoffEntry: {
    properties: {
      entry_id: { type: 'string' },
      asset_version_id: { type: 'string' },
      group_id: { type: 'string' },
      group_name: { type: 'string' },
      combination_id: { type: 'string' },
      angle_id: { type: 'string' },
      expression_id: { type: 'string' },
      proposition: { type: 'string' },
      expression_name: { type: 'string' },
      ad_name: { type: 'string' },
      display_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      headline: { type: 'string' },
      body_copy: { type: 'string' },
      content_sha256: { type: 'string' },
      mime_type: { type: 'string' },
      recipe_hash: { type: 'string' },
      product_references: {
        anyOf: [
          { $ref: '#/components/schemas/ProductPhotoLineage' },
          { type: 'null' },
        ],
      },
      provenance: {
        anyOf: [
          { $ref: '#/components/schemas/CreativeProvenance' },
          { type: 'null' },
        ],
      },
      provider: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      model: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'entry_id',
      'asset_version_id',
      'group_id',
      'group_name',
      'combination_id',
      'angle_id',
      'expression_id',
      'proposition',
      'expression_name',
      'ad_name',
      'headline',
      'body_copy',
      'content_sha256',
      'mime_type',
      'recipe_hash',
    ],
  },
  IdentityProbeResponse: {
    properties: {
      workspace_id: { type: 'string' },
      role: { type: 'string', enum: ['owner', 'member'] },
      initiator_id: { type: 'string' },
      authorization_epoch: { type: 'integer' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['workspace_id', 'role', 'initiator_id', 'authorization_epoch'],
    description:
      'The verified TenantContext, echoed back for the tracer round trip.\n\nDeclared only so `generate_openapi.py` can extract a real response\nschema from `response_model` — the handler returns a `JSONResponse`\ndirectly, which FastAPI never re-validates against `response_model`, so\nthis model documents the contract without changing runtime behavior.',
  },
  ImageBrief: {
    properties: {
      proposition: { type: 'string', maxLength: 2000, minLength: 1 },
      expression: { type: 'string', maxLength: 2000, minLength: 1 },
      hold_constant: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
        minItems: 1,
      },
      checks: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 12,
        minItems: 1,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['proposition', 'expression', 'hold_constant', 'checks'],
    description:
      'English instructions for the image model, separate from strategy prose.',
  },
  ImageCrop: {
    properties: {
      x: { type: 'number', exclusiveMaximum: 1, minimum: 0 },
      y: { type: 'number', exclusiveMaximum: 1, minimum: 0 },
      width: { type: 'number', maximum: 1, exclusiveMinimum: 0 },
      height: { type: 'number', maximum: 1, exclusiveMinimum: 0 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['x', 'y', 'width', 'height'],
  },
  ImageEvidence: {
    properties: {
      asset_version_id: { type: 'string' },
      mime: { type: 'string' },
      sha256: { type: 'string' },
      width: { type: 'integer' },
      height: { type: 'integer' },
      policy: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'asset_version_id',
      'mime',
      'sha256',
      'width',
      'height',
      'policy',
    ],
  },
  ImageToolSpec: {
    properties: {
      kind: { type: 'string', enum: ['upscale', 'remove_background', 'erase'] },
      source_width: { type: 'integer', maximum: 8192, minimum: 1 },
      source_height: { type: 'integer', maximum: 8192, minimum: 1 },
      scale: { anyOf: [{ type: 'integer', enum: [2, 4] }, { type: 'null' }] },
      region: {
        anyOf: [{ $ref: '#/components/schemas/EraseRegion' }, { type: 'null' }],
      },
      strokes: {
        anyOf: [
          {
            items: { $ref: '#/components/schemas/EraseStroke' },
            type: 'array',
            maxItems: 128,
            minItems: 1,
          },
          { type: 'null' },
        ],
      },
      algorithm: {
        type: 'string',
        const: 'supplier-image-tools-v1',
        default: 'supplier-image-tools-v1',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['kind', 'source_width', 'source_height'],
  },
  ImpactDependent: {
    properties: {
      version_id: { type: 'string' },
      entry_id: { type: 'string' },
      entry_type: { type: 'string' },
      version_no: { type: 'integer' },
      state: { type: 'string' },
      eligibility: { type: 'string' },
    },
    type: 'object',
    required: [
      'version_id',
      'entry_id',
      'entry_type',
      'version_no',
      'state',
      'eligibility',
    ],
  },
  Inference: {
    properties: {
      id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      statement: { type: 'string', maxLength: 2000, minLength: 1 },
      rationale: { type: 'string', maxLength: 2000, minLength: 1 },
      fact_ids: {
        items: {
          type: 'string',
          maxLength: 64,
          minLength: 1,
          pattern: '^[a-zA-Z0-9_-]+$',
        },
        type: 'array',
        maxItems: 30,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'statement', 'rationale'],
  },
  IntakeFailure: {
    properties: {
      code: { type: 'string' },
      step: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      message: { type: 'string' },
      detail: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      suggested_action: { type: 'string' },
      retryable: { type: 'boolean', default: false },
      blocked_category: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['code', 'message', 'suggested_action'],
    description:
      'Why the intake stopped. ``code`` is stable (``access_denied``,\n``timeout``, ``not_product_page``, ``blocked_address``,\n``extraction_invalid``, ``credential_unavailable`` ...);\n``suggested_action`` tells the UI what to offer\n(``paste_text_or_screenshot``, ``check_link``, ``retry`` ...).',
  },
  IntakeImage: {
    properties: {
      image_id: { type: 'string' },
      rank: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      role: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source: { type: 'string' },
      page_source: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source_url: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      alt: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      mime_type: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      byte_size: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      width: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      height: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      sha256: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      gate_state: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      gate_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reference_object_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      selectable: { type: 'boolean', default: false },
      variant_label: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['image_id', 'source'],
    description:
      "A candidate image. ``gate_state`` is the reference gate's verdict\n(``released`` images are ``selectable`` and can be previewed through the\ndownload-intent route); ``role`` is the model's ranking role.",
  },
  IntakeImageDownloadGrant: {
    properties: {
      intake_id: { type: 'string' },
      image_id: { type: 'string' },
      reference_object_id: { type: 'string' },
      download_url: { type: 'string' },
      expires_at: { type: 'string' },
      mime_type: { type: 'string' },
      byte_size: { type: 'integer' },
      width: { type: 'integer' },
      height: { type: 'integer' },
    },
    type: 'object',
    required: [
      'intake_id',
      'image_id',
      'reference_object_id',
      'download_url',
      'expires_at',
      'mime_type',
      'byte_size',
      'width',
      'height',
    ],
  },
  IntakeInputSummary: {
    properties: {
      url: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      text_chars: { type: 'integer', default: 0 },
      upload_count: { type: 'integer', default: 0 },
    },
    type: 'object',
  },
  InternalEntryContext: {
    properties: {
      id: { type: 'string' },
      entry_type: { type: 'string' },
      display_name: { type: 'string' },
      lifecycle: { type: 'string' },
      current_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      root_entry_id: { type: 'string' },
      root_entry_type: { type: 'string' },
      parent_entry_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      relation_kind: { type: 'string' },
      creation_evidence: { type: 'string', default: '' },
    },
    type: 'object',
    required: [
      'id',
      'entry_type',
      'display_name',
      'lifecycle',
      'root_entry_id',
      'root_entry_type',
      'relation_kind',
    ],
    description:
      '``GET /library/entries/{root}/internal/{type}/{id}`` — verified context.',
  },
  InternalEntryMeta: {
    properties: {
      id: { type: 'string' },
      entry_type: { type: 'string' },
      display_name: { type: 'string' },
      lifecycle: { type: 'string' },
      current_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      host_category: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      context_count: { type: 'integer', default: 0 },
    },
    type: 'object',
    required: ['id', 'entry_type', 'display_name', 'lifecycle'],
    description:
      '``GET /library/internal/{type}/{id}`` — unscoped canonical metadata.',
  },
  JsonValue: {},
  JudgeExperimentRequest: {
    properties: {
      expected_revision: { type: 'integer', minimum: 1 },
      as_of: { anyOf: [{ type: 'string', format: 'date' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['expected_revision'],
  },
  LaunchCandidateResponse: {
    properties: {
      id: { type: 'string' },
      candidate_index: { type: 'integer' },
      experiment_version_candidate_id: { type: 'string' },
      asset_version_id: { type: 'string' },
      creative_config: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: [
      'id',
      'candidate_index',
      'experiment_version_candidate_id',
      'asset_version_id',
    ],
  },
  LaunchParityCheckResponse: {
    properties: {
      passed: { type: 'boolean' },
      all_candidates_present: { type: 'boolean' },
      budget_parity_verified: { type: 'boolean' },
      config_parity_verified: { type: 'boolean' },
      status_parity_verified: { type: 'boolean' },
    },
    type: 'object',
    required: [
      'passed',
      'all_candidates_present',
      'budget_parity_verified',
      'config_parity_verified',
      'status_parity_verified',
    ],
  },
  LaunchProvisioningAccepted: {
    properties: {
      id: { type: 'string' },
      operation_id: { type: 'string' },
      operation_state: { type: 'string' },
      provisioning_state: { type: 'string' },
    },
    type: 'object',
    required: ['id', 'operation_id', 'operation_state', 'provisioning_state'],
  },
  LaunchProvisioningResponse: {
    properties: {
      id: { type: 'string' },
      operation_id: { type: 'string' },
      operation_state: { type: 'string' },
      provisioning_state: { type: 'string' },
      activation_state: { type: 'string' },
      remote_campaign_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      fencing_token: { type: 'integer' },
      steps: {
        items: { $ref: '#/components/schemas/LaunchProvisioningStepResponse' },
        type: 'array',
      },
      parity_check: {
        anyOf: [
          { $ref: '#/components/schemas/LaunchParityCheckResponse' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'id',
      'operation_id',
      'operation_state',
      'provisioning_state',
      'activation_state',
      'fencing_token',
    ],
  },
  LaunchProvisioningStepResponse: {
    properties: {
      step_type: {
        type: 'string',
        enum: [
          'create_campaign',
          'create_adset',
          'create_ad',
          'readback_campaign',
          'readback_adset',
          'readback_ad',
        ],
      },
      state: { type: 'string' },
      remote_object_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['step_type', 'state'],
  },
  LaunchRecoveryResponse: {
    properties: {
      id: { type: 'string' },
      original_operation_id: { type: 'string' },
      recovery_operation_id: { type: 'string' },
      state: { type: 'string' },
      resolution: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['id', 'original_operation_id', 'recovery_operation_id', 'state'],
  },
  LearningsBasis: {
    properties: {
      snapshot_id: { type: 'string' },
      evidence_mode: { type: 'string' },
      explicit: { type: 'boolean', default: false },
      winners: { items: { type: 'string' }, type: 'array' },
      losers: { items: { type: 'string' }, type: 'array' },
      no_difference: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
    required: ['snapshot_id', 'evidence_mode'],
    description:
      'The learnings a plan was drafted with (Issue #166): labels of the\nwinning / losing / not-different values, the snapshot and its evidence\npartition; ``explicit`` is true for "按结论生成下一轮".',
  },
  LibraryCommandNotApplied: {
    properties: {
      contract_schema_version: { type: 'integer' },
      command_kind: { type: 'string' },
      command_identity: { type: 'string' },
      created_at: { type: 'string', format: 'date-time' },
      updated_at: { type: 'string', format: 'date-time' },
      reconciled_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      observed_at: { type: 'string', format: 'date-time' },
      outcome: { type: 'string', const: 'not_applied' },
    },
    type: 'object',
    required: [
      'contract_schema_version',
      'command_kind',
      'command_identity',
      'created_at',
      'updated_at',
      'observed_at',
      'outcome',
    ],
  },
  LibraryCommandPending: {
    properties: {
      contract_schema_version: { type: 'integer' },
      command_kind: { type: 'string' },
      command_identity: { type: 'string' },
      created_at: { type: 'string', format: 'date-time' },
      updated_at: { type: 'string', format: 'date-time' },
      reconciled_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      observed_at: { type: 'string', format: 'date-time' },
      outcome: { type: 'string', const: 'pending' },
    },
    type: 'object',
    required: [
      'contract_schema_version',
      'command_kind',
      'command_identity',
      'created_at',
      'updated_at',
      'observed_at',
      'outcome',
    ],
  },
  LibraryCommandProblem: {
    properties: {
      contract_schema_version: { type: 'integer' },
      command_kind: { type: 'string' },
      command_identity: { type: 'string' },
      created_at: { type: 'string', format: 'date-time' },
      updated_at: { type: 'string', format: 'date-time' },
      reconciled_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      observed_at: { type: 'string', format: 'date-time' },
      outcome: { type: 'string', const: 'problem' },
      problem: { $ref: '#/components/schemas/LibraryProblemDetails' },
    },
    type: 'object',
    required: [
      'contract_schema_version',
      'command_kind',
      'command_identity',
      'created_at',
      'updated_at',
      'observed_at',
      'outcome',
      'problem',
    ],
  },
  LibraryCommandReconciliationRequired: {
    properties: {
      contract_schema_version: { type: 'integer' },
      command_kind: { type: 'string' },
      command_identity: { type: 'string' },
      created_at: { type: 'string', format: 'date-time' },
      updated_at: { type: 'string', format: 'date-time' },
      reconciled_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      observed_at: { type: 'string', format: 'date-time' },
      outcome: { type: 'string', const: 'reconciliation_required' },
      reason_code: { type: 'string' },
    },
    type: 'object',
    required: [
      'contract_schema_version',
      'command_kind',
      'command_identity',
      'created_at',
      'updated_at',
      'observed_at',
      'outcome',
      'reason_code',
    ],
  },
  LibraryCommandSucceeded: {
    properties: {
      contract_schema_version: { type: 'integer' },
      command_kind: { type: 'string' },
      command_identity: { type: 'string' },
      created_at: { type: 'string', format: 'date-time' },
      updated_at: { type: 'string', format: 'date-time' },
      reconciled_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      observed_at: { type: 'string', format: 'date-time' },
      outcome: { type: 'string', const: 'succeeded' },
      result: { additionalProperties: true, type: 'object' },
      result_reference: {
        anyOf: [
          { additionalProperties: { type: 'string' }, type: 'object' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'contract_schema_version',
      'command_kind',
      'command_identity',
      'created_at',
      'updated_at',
      'observed_at',
      'outcome',
    ],
  },
  LibraryEntryDetail: {
    properties: {
      id: { type: 'string' },
      entry_type: { type: 'string' },
      display_name: { type: 'string' },
      lifecycle: { type: 'string' },
      current_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      draft: {
        anyOf: [
          { $ref: '#/components/schemas/DraftSummary' },
          { type: 'null' },
        ],
      },
      versions: {
        items: { $ref: '#/components/schemas/LibraryVersionSummary' },
        type: 'array',
      },
      revision: { type: 'integer', default: 1 },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['id', 'entry_type', 'display_name', 'lifecycle'],
    description:
      "``GET /library/entries/{id}`` and the create/save/archive/fork/clone result.\n\n``revision`` tracks the entry's open Draft (defaults to 1 once archived\nand the Draft has been dropped) and is the value clients must echo back\nvia ``If-Match`` on the next mutating command.",
  },
  LibraryEntryTypeList: {
    properties: { entry_types: { items: { type: 'string' }, type: 'array' } },
    type: 'object',
    required: ['entry_types'],
    description:
      '``GET /library/entry-types`` -- service.contracts.iter_entry_types() is a\nflat sorted list of type keys (e.g. "product", "person"), not objects.',
  },
  LibraryProblemDetails: {
    properties: {
      type: { type: 'string' },
      title: { type: 'string' },
      status: { type: 'integer' },
      detail: { type: 'string' },
      instance: { type: 'string', default: '' },
      code: { type: 'string' },
      trace_id: { type: 'string', default: '' },
      retryable: { type: 'boolean', default: false },
      field_errors: {
        items: { additionalProperties: true, type: 'object' },
        type: 'array',
      },
    },
    additionalProperties: true,
    type: 'object',
    required: ['type', 'title', 'status', 'detail', 'code'],
    description: 'Persisted RFC 9457-compatible terminal problem.',
  },
  LibraryValidationResult: {
    properties: {
      entry_type: { type: 'string' },
      ok: { type: 'boolean' },
      content_hash: { type: 'string' },
      blocking: {
        items: { additionalProperties: true, type: 'object' },
        type: 'array',
      },
      warnings: {
        items: { additionalProperties: true, type: 'object' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['entry_type', 'ok', 'content_hash'],
    description:
      '``POST /library/entries/{id}/validate`` result (contracts.ValidationReport.as_dict).',
  },
  LibraryVersionDetail: {
    properties: {
      id: { type: 'string' },
      entry_id: { type: 'string' },
      entry_type: { type: 'string' },
      version_no: { type: 'integer' },
      state: { type: 'string' },
      eligibility: { type: 'string' },
      content_schema_version: { type: 'integer', default: 1 },
      lifecycle_revision: { type: 'integer', default: 1 },
      content: { additionalProperties: true, type: 'object' },
      content_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      origin_command: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      base_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      published_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      published_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      withdraw_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'entry_id',
      'entry_type',
      'version_no',
      'state',
      'eligibility',
    ],
    description:
      '``GET /library/versions/{id}`` and the publish/withdraw command result.\n\nVersions are immutable once published (no draft-style revision counter),\nso there is no ``revision``/If-Match contract here -- ``version_no`` is\ninformational only. ``allowed_actions`` is server-computed.',
  },
  LibraryVersionImpact: {
    properties: {
      version_id: { type: 'string' },
      entry_id: { type: 'string' },
      entry_type: { type: 'string' },
      state: { type: 'string' },
      eligibility: { type: 'string' },
      dependents: {
        items: { $ref: '#/components/schemas/ImpactDependent' },
        type: 'array',
      },
      reason: { type: 'string', default: '' },
    },
    type: 'object',
    required: ['version_id', 'entry_id', 'entry_type', 'state', 'eligibility'],
    description:
      '``GET /library/versions/{id}/impact`` (service._impact_payload).',
  },
  LibraryVersionSummary: {
    properties: {
      id: { type: 'string' },
      entry_id: { type: 'string' },
      entry_type: { type: 'string' },
      version_no: { type: 'integer' },
      state: { type: 'string' },
      eligibility: { type: 'string' },
      content_schema_version: { type: 'integer', default: 1 },
      lifecycle_revision: { type: 'integer', default: 1 },
      content: { additionalProperties: true, type: 'object' },
      content_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      origin_command: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      base_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      published_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      published_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      withdraw_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'id',
      'entry_id',
      'entry_type',
      'version_no',
      'state',
      'eligibility',
    ],
    description: 'Version row embedded in ``LibraryEntryDetail.versions``.',
  },
  LocalizationAction: {
    properties: {
      action: { type: 'string' },
      allowed: { type: 'boolean' },
      reason_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['action', 'allowed'],
  },
  LocalizationAssetBinding: {
    properties: {
      id: { type: 'string' },
      kind: { type: 'string' },
      bound_asset_version_id: { type: 'string' },
      review_state: { type: 'string' },
      truth_freshness: { type: 'string' },
      ready_for_experiment: { type: 'boolean' },
    },
    type: 'object',
    required: [
      'id',
      'kind',
      'bound_asset_version_id',
      'review_state',
      'truth_freshness',
      'ready_for_experiment',
    ],
  },
  LocalizationAssetLineage: {
    properties: {
      source_asset_version_id: { type: 'string' },
      parent_asset_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      decision_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      truth_package_id: { type: 'string' },
      copy_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      localized_asset_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      attempt_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      output_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      publication_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'source_asset_version_id',
      'decision_id',
      'offer_version_id',
      'truth_package_id',
    ],
  },
  LocalizationAttemptOutput: {
    properties: {
      id: { type: 'string' },
      content_sha256: { type: 'string' },
      byte_size: { type: 'integer' },
      mime_type: { type: 'string' },
      isolated: { type: 'boolean' },
    },
    type: 'object',
    required: ['id', 'content_sha256', 'byte_size', 'mime_type', 'isolated'],
  },
  LocalizationAttemptSummary: {
    properties: {
      id: { type: 'string' },
      attempt_number: { type: 'integer' },
      state: { type: 'string' },
      provider: { type: 'string' },
      model: { type: 'string' },
      created_at: { type: 'string' },
      output: {
        anyOf: [
          { $ref: '#/components/schemas/LocalizationAttemptOutput' },
          { type: 'null' },
        ],
      },
      publication: {
        anyOf: [
          { $ref: '#/components/schemas/LocalizationOutputPublication' },
          { type: 'null' },
        ],
      },
      qc: {
        items: { $ref: '#/components/schemas/LocalizationQcCheck' },
        type: 'array',
      },
      provider_calls: {
        items: { $ref: '#/components/schemas/LocalizationProviderCall' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'attempt_number',
      'state',
      'provider',
      'model',
      'created_at',
    ],
  },
  LocalizationBatchDetail: {
    properties: {
      id: { type: 'string' },
      phase: { type: 'string' },
      preparation_stage: {
        anyOf: [
          {
            type: 'string',
            enum: ['analysis', 'truth', 'claim_review', 'copy', 'complete'],
          },
          { type: 'null' },
        ],
      },
      outcome: { type: 'string' },
      truth_freshness: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      truth_status: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      truth_valid_until: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      provider_execution_mode: { type: 'string' },
      evidence_tier: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source_count: { type: 'integer' },
      market_count: { type: 'integer' },
      item_count: { type: 'integer' },
      item_state_counts: {
        additionalProperties: { type: 'integer' },
        type: 'object',
      },
      revision: { type: 'integer' },
      created_at: { type: 'string' },
      updated_at: { type: 'string' },
      allowed_actions: {
        items: { $ref: '#/components/schemas/LocalizationAction' },
        type: 'array',
      },
      source_market_snapshot: { additionalProperties: true, type: 'object' },
      truth_package_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      truth_data_mode: { type: 'string' },
      max_attempts: { type: 'integer' },
      frozen_recipe: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      frozen_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      sources: {
        items: { $ref: '#/components/schemas/LocalizationBatchSource' },
        type: 'array',
      },
      markets: {
        items: { $ref: '#/components/schemas/LocalizationTruthMarket' },
        type: 'array',
      },
      copies: {
        items: { $ref: '#/components/schemas/LocalizedCopySummary' },
        type: 'array',
      },
      items: {
        items: { $ref: '#/components/schemas/LocalizationBatchItemSummary' },
        type: 'array',
      },
      commands: {
        items: { $ref: '#/components/schemas/LocalizationCommandSummary' },
        type: 'array',
      },
      inventories: {
        items: { $ref: '#/components/schemas/LocalizationSourceInventory' },
        type: 'array',
      },
      claim_decision_sets: {
        items: {
          $ref: '#/components/schemas/LocalizationClaimDecisionSetSummary',
        },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'phase',
      'outcome',
      'product_version_id',
      'offer_version_id',
      'provider_execution_mode',
      'source_count',
      'market_count',
      'item_count',
      'revision',
      'created_at',
      'updated_at',
      'truth_data_mode',
      'max_attempts',
    ],
  },
  LocalizationBatchItemSummary: {
    properties: {
      id: { type: 'string' },
      state: { type: 'string' },
      target_market: { type: 'string' },
      source_asset_version_id: { type: 'string' },
      attempt_count: { type: 'integer' },
      reason_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reason_detail: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      recoverable: { anyOf: [{ type: 'boolean' }, { type: 'null' }] },
      next_action: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      revision: { type: 'integer' },
      allowed_actions: {
        items: { $ref: '#/components/schemas/LocalizationAction' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'state',
      'target_market',
      'source_asset_version_id',
      'attempt_count',
      'revision',
    ],
  },
  LocalizationBatchPage: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/LocalizationBatchSummary' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
  },
  LocalizationBatchSource: {
    properties: {
      id: { type: 'string' },
      asset_version_id: { type: 'string' },
      decision_id: { type: 'string' },
      experiment_id: { type: 'string' },
      experiment_version_id: { type: 'string' },
      source_generation_mode: { type: 'string' },
      experiment_evidence_mode: { type: 'string' },
      source_market_snapshot: {
        additionalProperties: { type: 'string' },
        type: 'object',
      },
      analysis_recipe_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      inventory_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reusable_inventories: {
        items: { $ref: '#/components/schemas/LocalizationReusableInventory' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'asset_version_id',
      'decision_id',
      'experiment_id',
      'experiment_version_id',
      'source_generation_mode',
      'experiment_evidence_mode',
    ],
  },
  LocalizationBatchSummary: {
    properties: {
      id: { type: 'string' },
      phase: { type: 'string' },
      preparation_stage: {
        anyOf: [
          {
            type: 'string',
            enum: ['analysis', 'truth', 'claim_review', 'copy', 'complete'],
          },
          { type: 'null' },
        ],
      },
      outcome: { type: 'string' },
      truth_freshness: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      truth_status: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      truth_valid_until: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      provider_execution_mode: { type: 'string' },
      evidence_tier: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source_count: { type: 'integer' },
      market_count: { type: 'integer' },
      item_count: { type: 'integer' },
      item_state_counts: {
        additionalProperties: { type: 'integer' },
        type: 'object',
      },
      revision: { type: 'integer' },
      created_at: { type: 'string' },
      updated_at: { type: 'string' },
      allowed_actions: {
        items: { $ref: '#/components/schemas/LocalizationAction' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'phase',
      'outcome',
      'product_version_id',
      'offer_version_id',
      'provider_execution_mode',
      'source_count',
      'market_count',
      'item_count',
      'revision',
      'created_at',
      'updated_at',
    ],
  },
  LocalizationClaimDecisionSetSummary: {
    properties: {
      id: { type: 'string' },
      inventory_id: { type: 'string' },
      source_asset_version_id: { type: 'string' },
      target_market: { type: 'string' },
      decision_count: { type: 'integer' },
    },
    type: 'object',
    required: [
      'id',
      'inventory_id',
      'source_asset_version_id',
      'target_market',
      'decision_count',
    ],
  },
  LocalizationCommandSummary: {
    properties: {
      id: { type: 'string' },
      command_kind: { type: 'string' },
      item_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      actor_id: { type: 'string' },
      created_at: { type: 'string' },
    },
    type: 'object',
    required: ['id', 'command_kind', 'actor_id', 'created_at'],
  },
  LocalizationConfigurationVersion: {
    properties: {
      id: { type: 'string' },
      entry_id: { type: 'string' },
      entry_type: { type: 'string' },
      display_name: { type: 'string' },
      fact_policies: {
        items: { $ref: '#/components/schemas/LocalizationPolicyFact' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['id', 'entry_id', 'entry_type', 'display_name'],
    description:
      "A published, non-secret LibraryVersion usable by Localization.\n\nThe workspace projection exposes identity, display metadata and the\npublished policy's non-secret fact requirements. Adapter credentials\nremain behind the domain/KMS boundary.",
  },
  LocalizationCostLedgerEntry: {
    properties: {
      entry_type: { type: 'string' },
      direction: { type: 'string' },
      amount: { type: 'string' },
      currency: { type: 'string' },
      provider: { type: 'string' },
      model: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { type: 'string' },
    },
    type: 'object',
    required: [
      'entry_type',
      'direction',
      'amount',
      'currency',
      'provider',
      'created_at',
    ],
  },
  LocalizationCostTotal: {
    properties: { amount: { type: 'string' }, currency: { type: 'string' } },
    type: 'object',
    required: ['amount', 'currency'],
  },
  LocalizationItemCost: {
    properties: {
      totals: {
        items: { $ref: '#/components/schemas/LocalizationCostTotal' },
        type: 'array',
      },
      ledger: {
        items: { $ref: '#/components/schemas/LocalizationCostLedgerEntry' },
        type: 'array',
      },
    },
    type: 'object',
  },
  LocalizationItemDetail: {
    properties: {
      id: { type: 'string' },
      batch_id: { type: 'string' },
      state: { type: 'string' },
      target_market: { type: 'string' },
      source_asset_version_id: { type: 'string' },
      decision_id: { type: 'string' },
      truth_package_id: { type: 'string' },
      truth_package_market_id: { type: 'string' },
      market_policy_version_id: { type: 'string' },
      inventory_id: { type: 'string' },
      claim_decision_set_id: { type: 'string' },
      copy_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      attempt_count: { type: 'integer' },
      max_attempts: { type: 'integer' },
      reason_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reason_detail: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      recoverable: { anyOf: [{ type: 'boolean' }, { type: 'null' }] },
      next_action: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      revision: { type: 'integer' },
      localized_copy: {
        anyOf: [
          { $ref: '#/components/schemas/LocalizedCopyDetail' },
          { type: 'null' },
        ],
      },
      attempts: {
        items: { $ref: '#/components/schemas/LocalizationAttemptSummary' },
        type: 'array',
      },
      cost: { $ref: '#/components/schemas/LocalizationItemCost' },
      binding: {
        anyOf: [
          { $ref: '#/components/schemas/LocalizationAssetBinding' },
          { type: 'null' },
        ],
      },
      lineage: { $ref: '#/components/schemas/LocalizationAssetLineage' },
      allowed_actions: {
        items: { $ref: '#/components/schemas/LocalizationAction' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'batch_id',
      'state',
      'target_market',
      'source_asset_version_id',
      'decision_id',
      'truth_package_id',
      'truth_package_market_id',
      'market_policy_version_id',
      'inventory_id',
      'claim_decision_set_id',
      'attempt_count',
      'max_attempts',
      'revision',
      'lineage',
    ],
  },
  LocalizationMarket: {
    properties: {
      market: { type: 'string' },
      locale: { type: 'string' },
      country: { type: 'string' },
      currency: { type: 'string' },
      market_policy_version_id: { type: 'string' },
      requires_local_copy: { type: 'boolean' },
      base_tone: { type: 'string' },
    },
    type: 'object',
    required: [
      'market',
      'locale',
      'country',
      'currency',
      'market_policy_version_id',
      'requires_local_copy',
      'base_tone',
    ],
  },
  LocalizationMaximumCost: {
    properties: {
      amount: { type: 'string' },
      currency: { type: 'string' },
      state: { type: 'string' },
    },
    type: 'object',
    required: ['amount', 'currency', 'state'],
  },
  LocalizationOutputPublication: {
    properties: {
      id: { type: 'string' },
      publication_kind: { type: 'string' },
      asset_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { type: 'string' },
    },
    type: 'object',
    required: ['id', 'publication_kind', 'created_at'],
  },
  LocalizationPolicyFact: {
    properties: {
      fact_type: { type: 'string' },
      on_failure: { type: 'string', enum: ['block', 'degrade', 'skip'] },
    },
    type: 'object',
    required: ['fact_type', 'on_failure'],
  },
  LocalizationProviderCall: {
    properties: {
      step: { type: 'string' },
      dispatch_state: { type: 'string' },
      chargeability: { type: 'string' },
      provider: { type: 'string' },
      model: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      error_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      maximum_cost: {
        anyOf: [
          { $ref: '#/components/schemas/LocalizationMaximumCost' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['step', 'dispatch_state', 'chargeability', 'provider'],
  },
  LocalizationQcCheck: {
    properties: {
      id: { type: 'string' },
      check_kind: { type: 'string' },
      check_slot_key: { type: 'string' },
      severity: { type: 'string' },
      result: { type: 'string' },
      expected: {},
      observed: {},
      rule_version: { type: 'string' },
    },
    type: 'object',
    required: [
      'id',
      'check_kind',
      'check_slot_key',
      'severity',
      'result',
      'expected',
      'observed',
      'rule_version',
    ],
  },
  LocalizationReusableInventory: {
    properties: {
      id: { type: 'string' },
      analysis_recipe_hash: { type: 'string' },
      version_number: { type: 'integer' },
      claim_types: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
    required: ['id', 'analysis_recipe_hash', 'version_number', 'claim_types'],
  },
  LocalizationSourceClaim: {
    properties: {
      claim_index: { type: 'integer' },
      claim_type: { type: 'string' },
      candidate_fact_types: { items: { type: 'string' }, type: 'array' },
      market_sensitive: { type: 'boolean', default: false },
      scope_key: { type: 'string' },
      ocr_text: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confidence: { anyOf: [{ type: 'number' }, { type: 'null' }] },
      risk: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      conflicting: { type: 'boolean', default: false },
      region: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['claim_index', 'claim_type', 'scope_key'],
  },
  LocalizationSourceContextProblem: {
    properties: {
      code: {
        type: 'string',
        enum: [
          'missing_source_context',
          'invalid_source_context',
          'conflicting_source_context',
        ],
      },
      missing_fields: {
        items: {
          type: 'string',
          enum: ['market', 'placement', 'locale', 'country', 'currency'],
        },
        type: 'array',
      },
      invalid_fields: {
        items: {
          type: 'string',
          enum: ['market', 'placement', 'locale', 'country', 'currency'],
        },
        type: 'array',
      },
      conflicting_fields: {
        items: {
          type: 'string',
          enum: ['market', 'placement', 'locale', 'country', 'currency'],
        },
        type: 'array',
      },
    },
    type: 'object',
    required: ['code'],
  },
  LocalizationSourceInventory: {
    properties: {
      id: { type: 'string' },
      asset_version_id: { type: 'string' },
      analysis_recipe_hash: { type: 'string' },
      version_number: { type: 'integer' },
      provider: { type: 'string' },
      model: { type: 'string' },
      analysis_execution_mode: { type: 'string' },
      evidence_hash: { type: 'string' },
      claims: {
        items: { $ref: '#/components/schemas/LocalizationSourceClaim' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'asset_version_id',
      'analysis_recipe_hash',
      'version_number',
      'provider',
      'model',
      'analysis_execution_mode',
      'evidence_hash',
    ],
  },
  LocalizationTruthFact: {
    properties: {
      id: { type: 'string' },
      fact_type: { type: 'string' },
      scope_key: { type: 'string' },
      value: {},
      external_object_ref: { type: 'string' },
      accepted_source_attempt_id: { type: 'string' },
      requested_context: { additionalProperties: true, type: 'object' },
      evidence_hash: { type: 'string' },
      mock_scenario: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      mock_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      observed_at: { type: 'string' },
      valid_until: { type: 'string' },
      source_valid_until: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'id',
      'fact_type',
      'scope_key',
      'value',
      'external_object_ref',
      'accepted_source_attempt_id',
      'evidence_hash',
      'observed_at',
      'valid_until',
    ],
  },
  LocalizationTruthMarket: {
    properties: {
      market: { type: 'string' },
      market_policy_version_id: { type: 'string' },
      readiness: { type: 'string' },
      readiness_detail: { additionalProperties: true, type: 'object' },
      locale: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      country: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      currency: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      truth_package_market_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      facts: {
        items: { $ref: '#/components/schemas/LocalizationTruthFact' },
        type: 'array',
      },
      source_attempts: {
        items: { $ref: '#/components/schemas/LocalizationTruthSourceAttempt' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['market', 'market_policy_version_id', 'readiness'],
  },
  LocalizationTruthSourceAttempt: {
    properties: {
      id: { type: 'string' },
      fact_type: { type: 'string' },
      adapter_kind: { type: 'string' },
      data_source_version_id: { type: 'string' },
      outcome: { type: 'string' },
      failure_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      skip_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'id',
      'fact_type',
      'adapter_kind',
      'data_source_version_id',
      'outcome',
    ],
  },
  LocalizationWorkspace: {
    properties: {
      role: { type: 'string' },
      eligible_sources: {
        items: { $ref: '#/components/schemas/EligibleLocalizationSource' },
        type: 'array',
      },
      markets: {
        items: { $ref: '#/components/schemas/LocalizationMarket' },
        type: 'array',
      },
      truth_resolution_policies: {
        items: {
          $ref: '#/components/schemas/LocalizationConfigurationVersion',
        },
        type: 'array',
      },
      data_sources: {
        items: {
          $ref: '#/components/schemas/LocalizationConfigurationVersion',
        },
        type: 'array',
      },
    },
    type: 'object',
    required: ['role'],
  },
  LocalizedCopyDetail: {
    properties: {
      id: { type: 'string' },
      state: { type: 'string' },
      origin: { type: 'string' },
      content: { additionalProperties: true, type: 'object' },
      evidence_hash: { type: 'string' },
    },
    type: 'object',
    required: ['id', 'state', 'origin', 'evidence_hash'],
  },
  LocalizedCopySummary: {
    properties: {
      id: { type: 'string' },
      source_asset_version_id: { type: 'string' },
      target_market: { type: 'string' },
      state: { type: 'string' },
      copy_origin: { type: 'string' },
      content: { additionalProperties: true, type: 'object' },
      evidence_hash: { type: 'string' },
    },
    type: 'object',
    required: [
      'id',
      'source_asset_version_id',
      'target_market',
      'state',
      'copy_origin',
      'evidence_hash',
    ],
  },
  ManualFactImportInput: {
    properties: {
      upload_intent_id: { type: 'string' },
      purpose: {
        type: 'string',
        maxLength: 100,
        minLength: 1,
        default: 'library_manual_fact_import',
      },
    },
    type: 'object',
    required: ['upload_intent_id'],
  },
  ManualResultRow: {
    properties: {
      asset_code: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
      ad_name: {
        anyOf: [{ type: 'string', maxLength: 500 }, { type: 'null' }],
      },
      ad_id: { anyOf: [{ type: 'string', maxLength: 100 }, { type: 'null' }] },
      date_start: { type: 'string', maxLength: 20, minLength: 1 },
      date_end: { type: 'string', maxLength: 20, minLength: 1 },
      impressions: { anyOf: [{ type: 'integer' }, { type: 'string' }] },
      clicks: { anyOf: [{ type: 'integer' }, { type: 'string' }] },
      spend: {
        anyOf: [
          { type: 'string' },
          { type: 'number' },
          { type: 'integer' },
          { type: 'null' },
        ],
      },
      conversions: {
        anyOf: [
          { type: 'string' },
          { type: 'number' },
          { type: 'integer' },
          { type: 'null' },
        ],
      },
      revenue: {
        anyOf: [
          { type: 'string' },
          { type: 'number' },
          { type: 'integer' },
          { type: 'null' },
        ],
      },
      currency: { anyOf: [{ type: 'string', maxLength: 8 }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['date_start', 'date_end', 'impressions', 'clicks'],
    description:
      "One ad's results over a date range. Identify the image by its asset\ncode (``MK-XXXXXX``, from the template), an ad name containing that\ncode, or a Meta ``ad_id`` bound to it. ``clicks`` are link clicks.",
  },
  MarketingBoard: {
    properties: {
      version: { type: 'string', const: 'marketing-board-v1' },
      question: { type: 'string' },
      stage: {
        type: 'string',
        enum: [
          'screen_angles',
          'match_expressions',
          'confirm_combination',
          'optimize_elements',
        ],
      },
      comparison_kind: {
        type: 'string',
        enum: ['whole_combinations', 'specific_factor', 'business_retest'],
      },
      primary_comparison: {
        anyOf: [
          { $ref: '#/components/schemas/BetweenGroups' },
          { $ref: '#/components/schemas/WithinAngle' },
          { $ref: '#/components/schemas/Retest' },
          { $ref: '#/components/schemas/FactorComparison' },
        ],
      },
      secondary_observation: {
        anyOf: [
          { $ref: '#/components/schemas/SecondaryObservation' },
          { type: 'null' },
        ],
      },
      tree: { $ref: '#/components/schemas/BoardTree' },
      matrix: { $ref: '#/components/schemas/BoardMatrix' },
      product_appearance: {
        $ref: '#/components/schemas/BoardAppearanceSummary',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'version',
      'question',
      'stage',
      'comparison_kind',
      'primary_comparison',
      'secondary_observation',
      'tree',
      'matrix',
      'product_appearance',
    ],
  },
  MarketingProjectDetail: {
    properties: {
      id: { type: 'string' },
      revision: { type: 'integer' },
      state: {
        type: 'string',
        enum: [
          'input',
          'understanding_draft',
          'understanding_confirmed',
          'plan_draft',
          'plan_confirmed',
        ],
      },
      source_text: { type: 'string' },
      product_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      understanding: {
        anyOf: [
          { $ref: '#/components/schemas/Understanding' },
          { type: 'null' },
        ],
      },
      plan: {
        anyOf: [
          { $ref: '#/components/schemas/FrozenTestPlan' },
          { type: 'null' },
        ],
      },
      language_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      plan_review_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      brand_style_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      brand_kit: {
        anyOf: [
          { $ref: '#/components/schemas/BrandKitVersion' },
          { type: 'null' },
        ],
      },
      competitor_review_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      competitor_snapshot_stale: {
        anyOf: [{ type: 'boolean' }, { type: 'null' }],
      },
      competitor_market_unmatched: {
        anyOf: [{ type: 'boolean' }, { type: 'null' }],
      },
      evidence_status: {
        type: 'string',
        const: 'unvalidated',
        default: 'unvalidated',
      },
      latest_call: {
        anyOf: [
          { $ref: '#/components/schemas/PlanningCallSummary' },
          { type: 'null' },
        ],
      },
      generation_draft_ids: { items: { type: 'string' }, type: 'array' },
      product_references: {
        $ref: '#/components/schemas/ProductReferenceSelection',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'revision',
      'state',
      'source_text',
      'product_id',
      'understanding',
      'plan',
      'latest_call',
      'generation_draft_ids',
    ],
  },
  MarketingProjectPage: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/MarketingProjectSummary' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['items'],
  },
  MarketingProjectSummary: {
    properties: {
      id: { type: 'string' },
      revision: { type: 'integer' },
      state: { type: 'string' },
      product_name: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'revision', 'state', 'product_name'],
  },
  MarketingRound: {
    properties: {
      id: { type: 'string' },
      ordinal: { type: 'integer' },
      state: {
        type: 'string',
        enum: ['reviewing', 'handoff', 'awaiting_data', 'evaluated', 'closed'],
      },
      close: {
        anyOf: [{ $ref: '#/components/schemas/RoundClose' }, { type: 'null' }],
      },
      image_edits: {
        items: { $ref: '#/components/schemas/WorkflowImageEdit' },
        type: 'array',
      },
      continuation: {
        anyOf: [
          { $ref: '#/components/schemas/RoundContinuation' },
          { type: 'null' },
        ],
      },
      revision: { type: 'integer' },
      plan_revision: { type: 'integer' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      snapshot: { $ref: '#/components/schemas/RoundSnapshot' },
      start_date: { type: 'string' },
      end_date: { type: 'string' },
      timezone: { type: 'string' },
      assets: {
        items: { $ref: '#/components/schemas/WorkflowAsset' },
        type: 'array',
      },
      handoff: {
        anyOf: [
          { $ref: '#/components/schemas/WorkflowHandoff' },
          { type: 'null' },
        ],
      },
      bindings: {
        items: { $ref: '#/components/schemas/DeliveryBinding' },
        type: 'array',
      },
      batches: {
        items: { $ref: '#/components/schemas/WorkflowBatch' },
        type: 'array',
      },
      evaluation: {
        anyOf: [
          { $ref: '#/components/schemas/WorkflowEvaluation' },
          { type: 'null' },
        ],
      },
      explanation: {
        anyOf: [
          { $ref: '#/components/schemas/WorkflowExplanation' },
          { type: 'null' },
        ],
      },
      feedback_drafts: {
        items: { $ref: '#/components/schemas/FeedbackDraft' },
        type: 'array',
      },
      board: {
        anyOf: [
          { $ref: '#/components/schemas/MarketingBoard' },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'ordinal',
      'state',
      'close',
      'revision',
      'plan_revision',
      'execution_mode',
      'snapshot',
      'start_date',
      'end_date',
      'timezone',
      'assets',
      'handoff',
      'bindings',
      'batches',
      'evaluation',
      'explanation',
      'feedback_drafts',
    ],
  },
  MatrixCol: {
    properties: {
      angle_id: { type: 'string' },
      proposition: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['angle_id', 'proposition'],
  },
  MatrixRow: {
    properties: {
      expression_id: { type: 'string' },
      name: { type: 'string' },
      visual_mechanism: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      angle_ids: { items: { type: 'string' }, type: 'array' },
      details_missing: { type: 'boolean' },
      shared: { type: 'boolean' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'expression_id',
      'name',
      'visual_mechanism',
      'angle_ids',
      'details_missing',
      'shared',
    ],
  },
  MaxCost: {
    properties: { amount: { type: 'string' }, currency: { type: 'string' } },
    type: 'object',
    required: ['amount', 'currency'],
  },
  MaximumCost: {
    properties: { amount: { type: 'string' }, currency: { type: 'string' } },
    type: 'object',
    required: ['amount', 'currency'],
    description:
      'Cost ceiling for a preview, expressed as a Decimal string with currency.',
  },
  MetaModeRequest: {
    properties: { meta_mode: { type: 'string' } },
    type: 'object',
    required: ['meta_mode'],
    description:
      'Body for POST routes that otherwise carry no payload but still need\nto bind the request to a Meta mode (see `_effective_context` below).',
  },
  MetricComparison: {
    properties: {
      metric: { type: 'string', enum: ['ctr', 'cpa'] },
      verdict: {
        type: 'string',
        enum: ['winner', 'loser', 'no_difference', 'insufficient_data'],
      },
      estimate: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      interval_low: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      interval_high: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      baseline: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      lift: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      lift_low: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      lift_high: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confidence: { type: 'string' },
      adjusted_alpha: { type: 'string' },
      reasons: { items: { type: 'string' }, type: 'array' },
      flags: { items: { type: 'string' }, type: 'array' },
      balance: {
        anyOf: [
          { $ref: '#/components/schemas/BalanceCheck' },
          { type: 'null' },
        ],
      },
      n: { $ref: '#/components/schemas/SampleSize' },
      baseline_n: { $ref: '#/components/schemas/SampleSize' },
      shortfall: {
        additionalProperties: { items: { type: 'string' }, type: 'array' },
        type: 'object',
      },
    },
    type: 'object',
    required: [
      'metric',
      'verdict',
      'confidence',
      'adjusted_alpha',
      'n',
      'baseline_n',
    ],
    description:
      "One metric of one value (or direction) against the rest.\n\n``estimate`` / ``interval_*``: the value's own rate (CTR: Wilson; CPA:\nseeded bootstrap). ``lift*``: ratio to the pooled other values minus\none, with the 95% interval (Bonferroni within the factor); for CPA a\nnegative lift is better. ``reasons`` explain ``insufficient_data``.",
  },
  MetricTotals: {
    properties: {
      days: { type: 'integer' },
      impressions: { type: 'integer' },
      clicks: { type: 'integer' },
      spend: { type: 'string' },
      purchases: { type: 'string' },
      purchase_value: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'days',
      'impressions',
      'clicks',
      'spend',
      'purchases',
      'purchase_value',
    ],
  },
  MetricsIssue: {
    properties: {
      row: { type: 'integer' },
      field: { type: 'string' },
      code: { type: 'string' },
      params: {
        anyOf: [
          {
            additionalProperties: {
              anyOf: [
                { type: 'string' },
                { type: 'integer' },
                { items: { type: 'string' }, type: 'array' },
              ],
            },
            type: 'object',
          },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['row', 'field', 'code'],
    description:
      'One preflight finding; the browser words it from ``code`` (#255).',
  },
  MetricsMetadata: {
    properties: {
      filename: { type: 'string' },
      ad_account_id: { type: 'string' },
      account_timezone: { type: 'string' },
      currency: {
        type: 'string',
        enum: [
          'AED',
          'AUD',
          'BRL',
          'CAD',
          'CHF',
          'CNY',
          'EUR',
          'GBP',
          'HKD',
          'IDR',
          'INR',
          'JPY',
          'KRW',
          'MXN',
          'MYR',
          'NZD',
          'PHP',
          'PLN',
          'SAR',
          'SEK',
          'SGD',
          'THB',
          'TWD',
          'USD',
          'VND',
          'ZAR',
        ],
      },
      attribution_window: {
        type: 'string',
        enum: [
          '1d_click',
          '7d_click',
          '1d_view',
          '1d_click_1d_view',
          '7d_click_1d_view',
        ],
      },
      source_mode: {
        type: 'string',
        enum: ['customer_file', 'controlled_mock'],
      },
      field_mapping: {
        additionalProperties: { type: 'string' },
        type: 'object',
      },
      resolved_mapping: {
        additionalProperties: { type: 'string' },
        type: 'object',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'filename',
      'ad_account_id',
      'account_timezone',
      'currency',
      'attribution_window',
      'source_mode',
      'field_mapping',
    ],
  },
  ModalityTokenUsage: {
    properties: {
      modality: { type: 'string' },
      tokenCount: { type: 'integer' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['modality', 'tokenCount'],
  },
  ModelVariant: {
    properties: {
      label: { type: 'string' },
      specs: { additionalProperties: { type: 'string' }, type: 'object' },
      appearance: {
        items: { $ref: '#/components/schemas/MustMatchView' },
        type: 'array',
      },
      replaces_must_match_ids: { items: { type: 'string' }, type: 'array' },
      image_ids: { items: { type: 'string' }, type: 'array' },
      default_image_ids: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
    required: ['label'],
    description:
      "One variant the source sells: a model, size or power, or a colour /\nappearance edition of the same model (Issue #192). ``specs`` are only the\nspecs that differ; ``appearance`` are its own must-match items (colour,\nmaterial, pattern) and ``replaces_must_match_ids`` the shared items they\nsupersede once it is chosen; ``image_ids`` are the candidate images that\nshow it (an image no variant claims is general), and\n``default_image_ids`` what the card preselects when it is chosen: its\nown and the general images, never another variant's.",
  },
  MultiCandidateConfirmInput: {
    properties: {
      meta_mode: { type: 'string' },
      preview_hash: { type: 'string', maxLength: 64, minLength: 64 },
    },
    type: 'object',
    required: ['meta_mode', 'preview_hash'],
  },
  MultiCandidateConfirmationResponse: {
    properties: {
      id: { type: 'string' },
      workspace_id: { type: 'string' },
      meta_mode: { type: 'string' },
      meta_connection_id: { type: 'string' },
      meta_binding_id: { type: 'string' },
      experiment_id: { type: 'string' },
      experiment_version_id: { type: 'string' },
      candidate_count: { type: 'integer' },
      campaign_config: { additionalProperties: true, type: 'object' },
      daily_budget_minor: { type: 'integer' },
      total_daily_cost_minor: { type: 'integer' },
      currency: { type: 'string' },
      preview_hash: { type: 'string' },
      state: { type: 'string' },
      confirmed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      expires_at: { type: 'string', format: 'date-time' },
      created_at: { type: 'string', format: 'date-time' },
      candidates: {
        items: { $ref: '#/components/schemas/LaunchCandidateResponse' },
        type: 'array',
      },
      confirmation_token: { type: 'string' },
      confirmation_expires_at: { type: 'string', format: 'date-time' },
      confirmation_context_hash: { type: 'string' },
    },
    type: 'object',
    required: [
      'id',
      'workspace_id',
      'meta_mode',
      'meta_connection_id',
      'meta_binding_id',
      'experiment_id',
      'experiment_version_id',
      'candidate_count',
      'daily_budget_minor',
      'total_daily_cost_minor',
      'currency',
      'preview_hash',
      'state',
      'expires_at',
      'created_at',
      'confirmation_token',
      'confirmation_expires_at',
      'confirmation_context_hash',
    ],
  },
  MultiCandidateExecuteInput: {
    properties: {
      meta_mode: { type: 'string' },
      confirmation_token: { type: 'string', minLength: 32 },
    },
    type: 'object',
    required: ['meta_mode', 'confirmation_token'],
  },
  MultiCandidatePreviewInput: {
    properties: {
      experiment_id: { type: 'string' },
      experiment_version_id: { type: 'string' },
      connection_id: { type: 'string' },
      meta_mode: { type: 'string' },
      ad_account_id: { type: 'string' },
      candidates: {
        items: { $ref: '#/components/schemas/CandidateInput' },
        type: 'array',
      },
      daily_budget_minor: { type: 'integer' },
      currency: { type: 'string' },
      campaign_config: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: [
      'experiment_id',
      'experiment_version_id',
      'connection_id',
      'meta_mode',
      'ad_account_id',
      'candidates',
      'daily_budget_minor',
      'currency',
    ],
  },
  MultiCandidatePreviewResponse: {
    properties: {
      id: { type: 'string' },
      workspace_id: { type: 'string' },
      meta_mode: { type: 'string' },
      meta_connection_id: { type: 'string' },
      meta_binding_id: { type: 'string' },
      experiment_id: { type: 'string' },
      experiment_version_id: { type: 'string' },
      candidate_count: { type: 'integer' },
      campaign_config: { additionalProperties: true, type: 'object' },
      daily_budget_minor: { type: 'integer' },
      total_daily_cost_minor: { type: 'integer' },
      currency: { type: 'string' },
      preview_hash: { type: 'string' },
      state: { type: 'string' },
      confirmed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      expires_at: { type: 'string', format: 'date-time' },
      created_at: { type: 'string', format: 'date-time' },
      candidates: {
        items: { $ref: '#/components/schemas/LaunchCandidateResponse' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'workspace_id',
      'meta_mode',
      'meta_connection_id',
      'meta_binding_id',
      'experiment_id',
      'experiment_version_id',
      'candidate_count',
      'daily_budget_minor',
      'total_daily_cost_minor',
      'currency',
      'preview_hash',
      'state',
      'expires_at',
      'created_at',
    ],
  },
  MustMatchEdit: {
    properties: {
      id: { anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }] },
      type: {
        type: 'string',
        enum: ['color', 'logo', 'visual', 'material', 'text', 'other'],
        default: 'visual',
      },
      description: { type: 'string', maxLength: 500, minLength: 1 },
      severity: {
        anyOf: [
          { type: 'string', enum: ['blocking', 'warning'] },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['description'],
  },
  MustMatchView: {
    properties: {
      id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      type: { type: 'string' },
      description: { type: 'string' },
      severity: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      proposed_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confirmed: { anyOf: [{ type: 'boolean' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['type', 'description'],
  },
  OperationAccepted: {
    properties: {
      operation_id: { type: 'string' },
      state: { type: 'string', default: 'queued' },
      command_type: { type: 'string' },
    },
    type: 'object',
    required: ['operation_id', 'command_type'],
    description:
      '202 Accepted envelope for async domain commands (connection-checks, manual-fact-imports, reference-gate).',
  },
  OperationDetailResponse: {
    properties: {
      operation_id: { type: 'string' },
      command_type: { type: 'string' },
      state: {
        type: 'string',
        enum: [
          'admitted',
          'queued',
          'running',
          'partially_succeeded',
          'succeeded',
          'failed',
          'cancelled',
          'uncertain',
        ],
      },
      result: {
        anyOf: [
          { $ref: '#/components/schemas/OperationResultRefResponse' },
          { type: 'null' },
        ],
      },
      created_at: { type: 'string', format: 'date-time' },
      updated_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'operation_id',
      'command_type',
      'state',
      'result',
      'created_at',
      'updated_at',
    ],
    description:
      'Declared only so `generate_openapi.py` can extract a real response\nschema (see the identical note on `IdentityProbeResponse`); the handler\nreturns a `JSONResponse` directly, so this documents the contract\nwithout changing runtime behavior.',
  },
  OperationRef: {
    properties: {
      operation_id: { type: 'string' },
      generation_run_id: { type: 'string' },
    },
    type: 'object',
    required: ['operation_id', 'generation_run_id'],
    description:
      '``POST /generation/runs`` result: the Operation created by Atomic Submission.',
  },
  OperationResultRefResponse: {
    properties: {
      resource_type: { type: 'string' },
      resource_id: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['resource_type', 'resource_id'],
    description:
      'Resource type + id (spec §5.1) -- never a URL the browser could not resolve.',
  },
  Page: {
    properties: {
      items: { items: {}, type: 'array' },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['items'],
    description:
      'Generic ``Page[T]`` envelope (spec §2): ``{ items, next_cursor }``.',
  },
  PairedQCWaiting: {
    properties: {
      asset_version_id: { type: 'string' },
      awaiting_combination_ids: { items: { type: 'string' }, type: 'array' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['asset_version_id', 'awaiting_combination_ids'],
    description:
      'An image whose paired check waits for usable images of these counterpart combinations.',
  },
  PlanFailure: {
    properties: {
      code: { type: 'string' },
      message: { type: 'string' },
      detail: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      retryable: { type: 'boolean', default: true },
    },
    type: 'object',
    required: ['code', 'message'],
  },
  PlannerCapabilities: {
    properties: {
      paid: { $ref: '#/components/schemas/PlannerCapability' },
      mock_available: { type: 'boolean', const: true, default: true },
      media_currencies: {
        items: {
          type: 'string',
          enum: [
            'AED',
            'AUD',
            'BRL',
            'CAD',
            'CHF',
            'CNY',
            'EUR',
            'GBP',
            'HKD',
            'IDR',
            'INR',
            'JPY',
            'KRW',
            'MXN',
            'MYR',
            'NZD',
            'PHP',
            'PLN',
            'SAR',
            'SEK',
            'SGD',
            'THB',
            'TWD',
            'USD',
            'VND',
            'ZAR',
          ],
        },
        type: 'array',
      },
      attribution_windows: {
        items: {
          type: 'string',
          enum: [
            '1d_click',
            '7d_click',
            '1d_view',
            '1d_click_1d_view',
            '7d_click_1d_view',
          ],
        },
        type: 'array',
      },
      attribution_min_lag_days: {
        additionalProperties: { type: 'integer' },
        type: 'object',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['paid'],
  },
  PlannerCapability: {
    properties: {
      provider: { type: 'string' },
      model: { type: 'string' },
      available: { type: 'boolean' },
      unavailable_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      maximum_cost: { type: 'string' },
      currency: { type: 'string', const: 'USD', default: 'USD' },
      quote_hash: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'provider',
      'model',
      'available',
      'unavailable_reason',
      'maximum_cost',
      'quote_hash',
    ],
  },
  PlanningCallSummary: {
    properties: {
      id: { type: 'string' },
      operation_id: { type: 'string' },
      kind: { type: 'string', enum: ['understanding', 'plan'] },
      state: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      error_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      maximum_cost: { type: 'string', default: '0' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'operation_id', 'kind', 'state', 'execution_mode'],
  },
  PrepareFeedback: {
    properties: {
      asset_version_id: { type: 'string', maxLength: 64, minLength: 1 },
      note: { type: 'string', maxLength: 1200, default: '' },
      provider: {
        type: 'string',
        enum: ['gemini', 'openai', 'seedream', 'mock'],
      },
      model: { type: 'string', maxLength: 200, minLength: 1 },
      image_tool: {
        anyOf: [
          { $ref: '#/components/schemas/ImageToolSpec' },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['asset_version_id', 'provider', 'model'],
  },
  PrepareGeneration: {
    properties: {
      product_references: {
        $ref: '#/components/schemas/ProductReferenceSelection',
      },
      provider: {
        type: 'string',
        enum: ['gemini', 'openai', 'seedream', 'mock'],
      },
      model: { type: 'string', maxLength: 200, minLength: 1 },
      aspect_ratio: {
        type: 'string',
        enum: ['1:1', '4:5', '9:16', '16:9'],
        default: '4:5',
      },
      image_size: { type: 'string', enum: ['1K', '2K'], default: '1K' },
      variants: {
        anyOf: [{ type: 'integer', maximum: 4, minimum: 1 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['provider', 'model'],
  },
  PreviewItemSummary: {
    properties: {
      ordinal: { type: 'integer' },
      dimension_tags: {
        additionalProperties: { type: 'string' },
        type: 'object',
      },
      reference_asset_ids: { items: { type: 'string' }, type: 'array' },
      excluded: { type: 'boolean', default: false },
      exclusion_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      stable_dimension_signature: { type: 'string', default: '' },
    },
    type: 'object',
    required: ['ordinal'],
    description:
      "Per-combination detail inside a resolved preview.\n\n``stable_dimension_signature`` (Issue #49 Task 8) is the combination's\ndimension-only fingerprint -- the identity Loser exclusion matches\nagainst a signature's newest produced AssetVersion.",
  },
  PreviewMetrics: {
    properties: {
      csv_text: { type: 'string', maxLength: 262144, minLength: 1 },
      filename: { type: 'string', maxLength: 160, minLength: 1 },
      ad_account_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^(act_)?[0-9]+$',
      },
      account_timezone: { type: 'string', maxLength: 64 },
      currency: {
        type: 'string',
        enum: [
          'AED',
          'AUD',
          'BRL',
          'CAD',
          'CHF',
          'CNY',
          'EUR',
          'GBP',
          'HKD',
          'IDR',
          'INR',
          'JPY',
          'KRW',
          'MXN',
          'MYR',
          'NZD',
          'PHP',
          'PLN',
          'SAR',
          'SEK',
          'SGD',
          'THB',
          'TWD',
          'USD',
          'VND',
          'ZAR',
        ],
      },
      attribution_window: {
        type: 'string',
        enum: [
          '1d_click',
          '7d_click',
          '1d_view',
          '1d_click_1d_view',
          '7d_click_1d_view',
        ],
      },
      source_mode: {
        type: 'string',
        enum: ['customer_file', 'controlled_mock'],
      },
      field_mapping: {
        additionalProperties: { type: 'string' },
        type: 'object',
        maxProperties: 10,
      },
      replaces_batch_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'csv_text',
      'filename',
      'ad_account_id',
      'account_timezone',
      'currency',
      'attribution_window',
      'source_mode',
    ],
  },
  ProblemDetails: {
    type: 'object',
    required: ['type', 'title', 'status', 'code', 'trace_id', 'retryable'],
    properties: {
      type: { type: 'string' },
      title: { type: 'string' },
      status: { type: 'integer' },
      detail: { type: 'string' },
      instance: { type: 'string' },
      code: { type: 'string' },
      params: { type: 'object', additionalProperties: true },
      trace_id: { type: 'string' },
      retryable: { type: 'boolean' },
      field_errors: {
        type: 'array',
        items: {
          type: 'object',
          properties: {
            field: { type: 'string' },
            code: { type: 'string' },
            detail: { type: 'string' },
            params: { type: 'object', additionalProperties: true },
          },
        },
      },
    },
  },
  ProductBrandKit: {
    properties: {
      product_id: { type: 'string' },
      revision: { type: 'integer' },
      current: {
        anyOf: [
          { $ref: '#/components/schemas/BrandKitVersion' },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id', 'revision'],
  },
  ProductCardDraft: {
    properties: {
      name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      description: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      specs: { additionalProperties: { type: 'string' }, type: 'object' },
      model_variants: {
        items: { $ref: '#/components/schemas/ModelVariant' },
        type: 'array',
      },
      requires_variant_selection: { type: 'boolean', default: false },
      selling_points: { items: { type: 'string' }, type: 'array' },
      audiences: {
        items: { $ref: '#/components/schemas/AudienceCard' },
        type: 'array',
      },
      must_match: {
        items: { $ref: '#/components/schemas/MustMatchView' },
        type: 'array',
      },
      images: {
        items: { $ref: '#/components/schemas/IntakeImage' },
        type: 'array',
      },
      default_image_ids: { items: { type: 'string' }, type: 'array' },
      source_url: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      dropped_commercial_specs: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
    description:
      'The AI\'s first draft. Every field is editable on confirm; ``must_match``\nitems carry ``proposed_by: "ai"`` and ``confirmed: false`` until then.',
  },
  ProductCardEdits: {
    properties: {
      name: {
        anyOf: [
          { type: 'string', maxLength: 200, minLength: 1 },
          { type: 'null' },
        ],
      },
      description: {
        anyOf: [
          { type: 'string', maxLength: 2000, minLength: 1 },
          { type: 'null' },
        ],
      },
      specs: {
        anyOf: [
          { additionalProperties: { type: 'string' }, type: 'object' },
          { type: 'null' },
        ],
      },
      selling_points: {
        anyOf: [
          { items: { type: 'string' }, type: 'array', maxItems: 10 },
          { type: 'null' },
        ],
      },
      audiences: {
        anyOf: [
          {
            items: { $ref: '#/components/schemas/AudienceCardEdit' },
            type: 'array',
            maxItems: 5,
          },
          { type: 'null' },
        ],
      },
      primary_audience_index: {
        anyOf: [{ type: 'integer', minimum: 0 }, { type: 'null' }],
      },
      must_match: {
        anyOf: [
          {
            items: { $ref: '#/components/schemas/MustMatchEdit' },
            type: 'array',
            maxItems: 20,
          },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    description:
      "The user's edits; an omitted field keeps the AI draft's value.",
  },
  ProductCompetitorSettings: {
    properties: {
      product_id: { type: 'string' },
      competitor_snapshot_in_drafts: { type: 'boolean' },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['product_id', 'competitor_snapshot_in_drafts'],
    description:
      "Whether drafting cites the latest competitor snapshot of the plan's\nmarket (spec §7.2; on unless a member turned it off).",
  },
  ProductCompetitorSettingsUpdate: {
    properties: { competitor_snapshot_in_drafts: { type: 'boolean' } },
    additionalProperties: false,
    type: 'object',
    required: ['competitor_snapshot_in_drafts'],
  },
  ProductIntakeAccepted: {
    properties: {
      intake_id: { type: 'string' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: { type: 'string' },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['intake_id', 'status'],
    description:
      '202: the intake and the Operation to poll (``GET /operations/{id}``)\n-- or ``GET /products/intake/{intake_id}`` for the card itself.\n``replayed`` is true when the same input (within 24 h) or the same\nIdempotency-Key returned an existing intake.',
  },
  ProductIntakeConfirmRequest: {
    properties: {
      card: {
        anyOf: [
          { $ref: '#/components/schemas/ProductCardEdits' },
          { type: 'null' },
        ],
      },
      selected_variant: {
        anyOf: [{ type: 'string', maxLength: 100 }, { type: 'null' }],
      },
      image_ids: {
        anyOf: [
          { items: { type: 'string' }, type: 'array', maxItems: 8 },
          { type: 'null' },
        ],
      },
      entry_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
      acknowledged_warnings: {
        items: { type: 'string' },
        type: 'array',
        maxItems: 20,
      },
    },
    additionalProperties: false,
    type: 'object',
    description:
      "Confirm (and publish) the card. ``selected_variant`` is required when\nthe draft has more than one ``model_variants`` entry; only that variant\nenters the version. ``image_ids`` defaults to the chosen variant's\n``default_image_ids`` (``card.default_image_ids`` without variants);\nan omitted ``card.must_match`` defaults to the shared items plus the\nchosen variant's ``appearance``, and an omitted ``card.specs`` to the\nshared specs with the variant's laid over them. ``entry_id`` updates an\nexisting product instead of creating one.",
  },
  ProductIntakeCreateRequest: {
    properties: {
      url: { anyOf: [{ type: 'string', maxLength: 2048 }, { type: 'null' }] },
      upload_intent_ids: {
        anyOf: [
          { items: { type: 'string' }, type: 'array', maxItems: 12 },
          { type: 'null' },
        ],
      },
      text: { anyOf: [{ type: 'string', maxLength: 20000 }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    description:
      'At least one of ``url``, ``upload_intent_ids`` (finalized uploads of\npurpose ``product_intake_image``) or ``text``.',
  },
  ProductIntakeDetail: {
    properties: {
      intake_id: { type: 'string' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: { type: 'string' },
      current_step: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      execution_mode: { type: 'string' },
      input: { $ref: '#/components/schemas/IntakeInputSummary' },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/IntakeFailure' },
          { type: 'null' },
        ],
      },
      evidence: { additionalProperties: true, type: 'object' },
      card: {
        anyOf: [
          { $ref: '#/components/schemas/ProductCardDraft' },
          { type: 'null' },
        ],
      },
      confirmation: {
        anyOf: [
          { $ref: '#/components/schemas/ConfirmedProduct' },
          { type: 'null' },
        ],
      },
      product_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['intake_id', 'status', 'execution_mode', 'input'],
    description:
      '``status``: ``queued`` -> ``fetching`` -> ``extracting`` ->\n``preparing_images`` -> ``ready`` (card available) -> ``confirmed``;\nor ``fetch_failed`` / ``extraction_failed`` / ``failed`` with\n``failure``. ``evidence`` holds per-step outcome, duration and digests.',
  },
  ProductLatestRun: {
    properties: {
      status: { type: 'string' },
      done: { type: 'integer', default: 0 },
      total: { type: 'integer', default: 0 },
      failed: { type: 'integer', default: 0 },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['status'],
    description:
      "The product's most recent Generation Run, summarized for the home card.",
  },
  ProductLearnings: {
    properties: {
      product_id: { type: 'string' },
      product_name: { type: 'string', default: '' },
      status: { type: 'string', enum: ['no_data', 'ready'] },
      evidence_mode: {
        anyOf: [
          { type: 'string', enum: ['production', 'sandbox', 'mock'] },
          { type: 'null' },
        ],
      },
      evidence_notice: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      available_modes: { items: { type: 'string' }, type: 'array' },
      snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      computed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      algorithm_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      thresholds: { additionalProperties: { type: 'string' }, type: 'object' },
      currency: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      highlights: { items: { type: 'string' }, type: 'array' },
      rounds: {
        items: { $ref: '#/components/schemas/RoundLearning' },
        type: 'array',
      },
      unattributable: {
        items: { $ref: '#/components/schemas/UnattributableAsset' },
        type: 'array',
      },
      can_seed_next_round: { type: 'boolean', default: false },
      review_signals: {
        anyOf: [
          { $ref: '#/components/schemas/ReviewSignals' },
          { type: 'null' },
        ],
      },
      recent_imports: {
        items: { $ref: '#/components/schemas/RecentImport' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['product_id', 'status'],
    description:
      '「这个产品我们已经学到了什么」. ``status`` ``no_data`` (show\n"尚无投放数据" and ``review_signals``) or ``ready``. ``evidence_mode``\nis the partition shown (production first); ``evidence_notice`` must be\ndisplayed when present (demo data). ``rounds`` newest first.',
  },
  ProductList: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/ProductSummary' },
        type: 'array',
      },
    },
    type: 'object',
  },
  ProductPhotoLineage: {
    properties: {
      policy: {
        anyOf: [
          { type: 'string', const: 'marketing-product-photos-v1' },
          { type: 'null' },
        ],
      },
      reference_actor_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      sent_to_provider: { anyOf: [{ type: 'boolean' }, { type: 'null' }] },
      delivery: {
        anyOf: [
          { $ref: '#/components/schemas/ReferenceDelivery' },
          { type: 'null' },
        ],
      },
      references: {
        items: { $ref: '#/components/schemas/ProductPhotoReference' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
  },
  ProductPhotoReference: {
    properties: {
      reference_version_id: { type: 'string' },
      content_sha256: { type: 'string' },
      content_hash: { type: 'string' },
      source: { type: 'string' },
      attestation: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'reference_version_id',
      'content_sha256',
      'content_hash',
      'source',
      'attestation',
    ],
  },
  ProductReferenceItem: {
    properties: {
      item_id: { type: 'string' },
      reference_object_id: { type: 'string' },
      reference_version_id: { type: 'string' },
      model_variant: { type: 'string', default: '' },
      role: {
        type: 'string',
        enum: ['primary', 'detail', 'logo', 'certification_mark'],
      },
      identity_status: {
        type: 'string',
        enum: ['verified_physical', 'unverified'],
      },
      note: { type: 'string', default: '' },
      added_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      added_at: { type: 'string' },
      available: { type: 'boolean' },
    },
    type: 'object',
    required: [
      'item_id',
      'reference_object_id',
      'reference_version_id',
      'role',
      'identity_status',
      'added_at',
      'available',
    ],
  },
  ProductReferenceSelection: {
    properties: {
      reference_version_ids: {
        items: {
          type: 'string',
          maxLength: 64,
          minLength: 1,
          pattern: '^[a-zA-Z0-9_-]+$',
        },
        type: 'array',
        maxItems: 3,
      },
      source: { type: 'string', maxLength: 1200, default: '' },
      confirms_real_product: { type: 'boolean', default: false },
    },
    additionalProperties: false,
    type: 'object',
  },
  ProductReferenceSet: {
    properties: {
      product_id: { type: 'string' },
      entry_id: { type: 'string' },
      version_id: { type: 'string' },
      revision: { type: 'integer' },
      model_variants: { items: { type: 'string' }, type: 'array' },
      has_unpublished_changes: { type: 'boolean', default: false },
      items: {
        items: { $ref: '#/components/schemas/ProductReferenceItem' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'product_id',
      'entry_id',
      'version_id',
      'revision',
      'model_variants',
      'items',
    ],
    description:
      "A projection of the product's immutable Library version/bindings.\n\n``revision`` is the If-Match value for edits. Deleted items remain in\nprior ProductVersions; ``version_id`` can be frozen by later consumers.",
  },
  ProductSummary: {
    properties: {
      product_id: { type: 'string' },
      entry_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      variant_label: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ready: { type: 'boolean', default: false },
      primary_reference_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      latest_run: {
        anyOf: [
          { $ref: '#/components/schemas/ProductLatestRun' },
          { type: 'null' },
        ],
      },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['product_id'],
    description:
      "One product as the front flow lists it (Issue #162). ``name`` is the\neffective ProductVersion's name (``null`` only when nothing names it);\n``ready`` is whether a new Generation draft may use it;\n``primary_reference_version_id`` previews through\n``create_library_reference_download_intent``.",
  },
  PromoteReferenceBindInput: {
    properties: {
      entry_id: { type: 'string', minLength: 1 },
      purpose: {
        anyOf: [{ type: 'string', maxLength: 100 }, { type: 'null' }],
      },
      viewpoint: {
        anyOf: [{ type: 'string', maxLength: 100 }, { type: 'null' }],
      },
      priority: { anyOf: [{ type: 'integer', minimum: 0 }, { type: 'null' }] },
      note: { anyOf: [{ type: 'string', maxLength: 1000 }, { type: 'null' }] },
      conditions: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      expected_revision: {
        anyOf: [{ type: 'integer', minimum: 1 }, { type: 'null' }],
      },
    },
    type: 'object',
    required: ['entry_id'],
    description:
      'Every field but ``entry_id`` is nullable without a default: the server\nfills the defaults (``service.PROMOTE_DEFAULT_PURPOSE`` / ``_VIEWPOINT``,\npriority 0, empty note/conditions), and the generated TypeScript type stays\noptional so the automatic post-upload bind sends only the entry id.',
  },
  PromoteReferenceObjectInput: {
    properties: {
      display_name: {
        anyOf: [{ type: 'string', maxLength: 300 }, { type: 'null' }],
      },
      rights: {
        anyOf: [
          { $ref: '#/components/schemas/ReferenceRightsDeclarationInput' },
          { type: 'null' },
        ],
      },
      bind: {
        anyOf: [
          { $ref: '#/components/schemas/PromoteReferenceBindInput' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
  },
  ProviderReceipt: {
    properties: {
      usage: { additionalProperties: { type: 'integer' }, type: 'object' },
      request_id: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['usage', 'request_id'],
  },
  PublishInput: {
    properties: {
      acknowledged_warnings: { items: { type: 'string' }, type: 'array' },
      impact_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      impact_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
  },
  QcCheckSummary: {
    properties: {
      name: { type: 'string' },
      passed: { type: 'boolean' },
      detail: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      verdict: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['name', 'passed'],
    description: 'Individual QC check result (``verdict`` since Issue #169).',
  },
  QcResultSummary: {
    properties: {
      state: { type: 'string' },
      checks: {
        items: { $ref: '#/components/schemas/QcCheckSummary' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['state'],
    description: 'Latest QC attempt state and check results for a RunItem.',
  },
  ReadabilityRules: {
    properties: {
      min_text_height_ratio: {
        type: 'number',
        maximum: 0.2,
        minimum: 0.01,
        default: 0.03,
      },
      min_contrast_ratio: {
        type: 'number',
        maximum: 21,
        minimum: 3,
        default: 4.5,
      },
      safe_area_ratio: {
        type: 'number',
        maximum: 0.2,
        minimum: 0,
        default: 0.05,
      },
    },
    additionalProperties: false,
    type: 'object',
  },
  ReapplyPreviewInput: {
    properties: { existing_schema_version_id: { type: 'string' } },
    type: 'object',
    required: ['existing_schema_version_id'],
  },
  RecentImport: {
    properties: {
      import_id: { type: 'string' },
      source: { type: 'string' },
      file_name: { type: 'string', default: '' },
      committed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      rows: { type: 'integer', default: 0 },
    },
    type: 'object',
    required: ['import_id', 'source'],
  },
  ReconcileRunInput: {
    properties: {
      uncertain_targets: { items: { type: 'string' }, type: 'array' },
      operator_note: { type: 'string', default: '' },
    },
    type: 'object',
  },
  RecoveryPreviewInput: {
    properties: {
      command: { type: 'string', minLength: 1 },
      selection: { $ref: '#/components/schemas/RecoveryPreviewSelection' },
    },
    type: 'object',
    required: ['command', 'selection'],
  },
  RecoveryPreviewSelection: {
    properties: {
      kind: {
        type: 'string',
        minLength: 1,
        description: 'item_ids | all_eligible_failed | all_resumable_paused',
      },
      item_ids: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
    required: ['kind'],
  },
  ReferenceDelivery: {
    properties: {
      reference_delivery_policy: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      reference_input_sha256: { items: { type: 'string' }, type: 'array' },
    },
    additionalProperties: false,
    type: 'object',
  },
  ReferenceDerivativeFacts: {
    properties: {
      mime_type: { type: 'string' },
      byte_size: { type: 'integer' },
      width: { type: 'integer' },
      height: { type: 'integer' },
    },
    type: 'object',
    required: ['mime_type', 'byte_size', 'width', 'height'],
  },
  ReferenceDownloadGrant: {
    properties: {
      version_id: { type: 'string' },
      object_id: { type: 'string' },
      download_url: { type: 'string' },
      content_disposition: { type: 'string' },
      expires_at: { type: 'string' },
      derivative_mime: { type: 'string' },
      derivative_byte_size: { type: 'integer' },
    },
    type: 'object',
    required: [
      'version_id',
      'object_id',
      'download_url',
      'content_disposition',
      'expires_at',
      'derivative_mime',
      'derivative_byte_size',
    ],
    description:
      '201 response for ``POST /versions/{id}/download-intent``.\n\n``download_url`` is a short-lived signed capability issued by the\n``BlobStore`` adapter for the controlled derivative -- the browser opens it\ndirectly against the storage origin (``NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS``),\nexactly like generated-asset downloads; it never transits FastAPI or Next.js.',
  },
  ReferenceGateCheck: {
    properties: {
      name: { type: 'string' },
      passed: { type: 'boolean' },
      detail: { type: 'string', default: '' },
    },
    type: 'object',
    required: ['name', 'passed'],
    description: 'One upload-gate check result (contract §7.2).',
  },
  ReferenceObjectDetail: {
    properties: {
      object_id: { type: 'string' },
      state: { type: 'string' },
      execution_mode: { type: 'string' },
      filename: { type: 'string', default: '' },
      declared_mime: { type: 'string', default: '' },
      mime_type: { type: 'string', default: '' },
      byte_size: { type: 'integer', default: 0 },
      width: { type: 'integer', default: 0 },
      height: { type: 'integer', default: 0 },
      content_sha256: { type: 'string', default: '' },
      checks: {
        items: { $ref: '#/components/schemas/ReferenceGateCheck' },
        type: 'array',
      },
      failure_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      failure_detail: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      content_safety_unavailable: { type: 'boolean', default: false },
      rights_declared: { type: 'boolean', default: false },
      promoted_entry_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      derivative: {
        anyOf: [
          { $ref: '#/components/schemas/ReferenceDerivativeFacts' },
          { type: 'null' },
        ],
      },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['object_id', 'state', 'execution_mode'],
    description:
      '``GET /library/reference-objects/{id}``: gate evidence for one object.\n\n``failure_code`` is the stable code the browser keys its copy on when the\ngate Operation failed (``content_safety_unavailable`` means the deployment\nselected ``real`` content safety without an adapter -- the object is still\n``quarantined``, not rejected). Object keys and signed URLs never appear\nhere.',
  },
  ReferencePromotionBinding: {
    properties: {
      entry_id: { type: 'string' },
      draft_revision: { type: 'integer' },
      purpose: { type: 'string' },
      viewpoint: { type: 'string' },
      already_bound: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['entry_id', 'draft_revision', 'purpose', 'viewpoint'],
  },
  ReferencePromotionResult: {
    properties: {
      object_id: { type: 'string' },
      entry_id: { type: 'string' },
      version_id: { type: 'string' },
      version_no: { type: 'integer' },
      display_name: { type: 'string' },
      created: { type: 'boolean' },
      binding: {
        anyOf: [
          { $ref: '#/components/schemas/ReferencePromotionBinding' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'object_id',
      'entry_id',
      'version_id',
      'version_no',
      'display_name',
      'created',
    ],
    description:
      '201 response for ``POST /library/reference-objects/{id}/promote``.\n\n``created`` is false when the object had already been promoted: the same\nentry and its current effective version are returned instead of a second\npublication. ``binding`` describes the product/person Draft the version\nwas appended to when ``bind`` was requested.',
  },
  ReferenceRightsDeclarationInput: {
    properties: {
      scope: { type: 'string', maxLength: 200, minLength: 1 },
      valid_to: {
        type: 'string',
        maxLength: 10,
        minLength: 10,
        description: 'ISO date (YYYY-MM-DD)',
      },
      evidence: {
        anyOf: [{ type: 'string', maxLength: 1000 }, { type: 'null' }],
      },
    },
    type: 'object',
    required: ['scope', 'valid_to'],
    description:
      "The uploader's rights declaration (contract §7.2: 权利状态、授权范围、\n有效期和操作者声明). ``state`` is always ``granted`` and ``declared_by`` is\nalways the current initiator -- neither is accepted from the client.",
  },
  RegenerationDraftBatchInput: {
    properties: {
      action: { type: 'string', minLength: 1 },
      candidate_version_ids: {
        items: { type: 'string' },
        type: 'array',
        minItems: 1,
      },
    },
    type: 'object',
    required: ['action', 'candidate_version_ids'],
  },
  RegenerationDraftBatchItem: {
    properties: {
      generation_draft_id: { type: 'string' },
      source_asset_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      stable_dimension_signature: { type: 'string', default: '' },
      requested_variant_count: {
        anyOf: [{ type: 'integer' }, { type: 'null' }],
      },
    },
    type: 'object',
    required: ['generation_draft_id'],
    description:
      "One created Draft in a review-summary batch result (Issue #49 Task 7):\nthe Draft id, the source AssetVersion chosen for its\nstable-dimension-signature group, that group's signature, and the\nvariant count the batch action requested for it (2 for\n``regenerate_needs_changes``, 3 for ``supplement_approved``).",
  },
  RegenerationDraftBatchResult: {
    properties: {
      drafts: {
        items: { $ref: '#/components/schemas/RegenerationDraftBatchItem' },
        type: 'array',
      },
    },
    type: 'object',
    description:
      "``POST /generation/regeneration-draft-batches`` result (Issue #49\nTask 7, spec §0.3/§0.4). Browser Task 10's ``ReviewSummary`` consumes\nthis.",
  },
  RejectedReason: {
    properties: {
      code: { type: 'string' },
      reason: { type: 'string' },
      count: { type: 'integer' },
    },
    type: 'object',
    required: ['code', 'reason', 'count'],
  },
  RemapTarget: {
    properties: {
      concept_key_id: { type: 'string' },
      concept_key_name: { type: 'string' },
    },
    type: 'object',
    required: ['concept_key_id', 'concept_key_name'],
  },
  RenderedText: {
    properties: {
      headline: { type: 'string', maxLength: 2000 },
      body_copy: { type: 'string', maxLength: 2000 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['headline', 'body_copy'],
    description:
      'The headline and body text as the image renders them, transcribed verbatim; empty when not shown or unreadable.',
  },
  RenderedTextDifference: {
    properties: {
      field: { type: 'string', enum: ['headline', 'body_copy'] },
      expected: { type: 'string' },
      rendered: { type: 'string' },
      changes: {
        items: { $ref: '#/components/schemas/CopyChange' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['field', 'expected', 'rendered'],
    description:
      'One copy field whose verbatim transcription differs from the confirmed text, or was not transcribed (`rendered` empty).',
  },
  RequestExplanation: {
    properties: {
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      quote_hash: { type: 'string', maxLength: 64, default: '' },
      strategy_language: {
        anyOf: [{ type: 'string', enum: ['zh-CN', 'en-US'] }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['execution_mode'],
  },
  ResolutionEvidenceRow: {
    properties: {
      duplicate_group_hash: { type: 'string' },
      dimension_key: { type: 'string' },
      concept_key_id: { type: 'string' },
      concept_key_name: { type: 'string' },
      action: { type: 'string' },
      reason: { type: 'string' },
      affected_candidate_fingerprint: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      remap_destination_concept_key_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      remap_destination_concept_key_name: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      actor_role: { type: 'string' },
      resolved_at: { type: 'string', format: 'date-time' },
    },
    type: 'object',
    required: [
      'duplicate_group_hash',
      'dimension_key',
      'concept_key_id',
      'concept_key_name',
      'action',
      'reason',
      'actor_role',
      'resolved_at',
    ],
  },
  ResolutionSummary: {
    properties: {
      id: { type: 'string' },
      operation_id: { type: 'string' },
      call_id: { type: 'string' },
      state: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'operation_id', 'call_id', 'state'],
  },
  ResolveCall: {
    properties: {
      kind: { type: 'string', enum: ['planning', 'ai_qc'] },
      maximum_cost: {
        anyOf: [
          { type: 'number', minimum: 0 },
          {
            type: 'string',
            pattern:
              '^(?!^[-+.]*$)[+-]?0*(?:\\d{0,8}|(?=[\\d.]{1,15}0*$)\\d{0,8}\\.\\d{0,6}0*$)',
          },
        ],
      },
      acknowledge_estimated_cost: { type: 'boolean', const: true },
      note: { type: 'string', maxLength: 2000, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['kind', 'maximum_cost', 'acknowledge_estimated_cost', 'note'],
  },
  ResourceEnvelope: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  ResultsImportCreateRequest: {
    properties: {
      product_id: { type: 'string', maxLength: 64, minLength: 1 },
      source: { type: 'string', enum: ['csv', 'manual', 'meta_sync'] },
      csv_text: {
        anyOf: [{ type: 'string', maxLength: 300000 }, { type: 'null' }],
      },
      upload_intent_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
      rows: {
        anyOf: [
          {
            items: { $ref: '#/components/schemas/ManualResultRow' },
            type: 'array',
            maxItems: 2000,
          },
          { type: 'null' },
        ],
      },
      currency: { anyOf: [{ type: 'string', maxLength: 3 }, { type: 'null' }] },
      file_name: {
        anyOf: [{ type: 'string', maxLength: 300 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id', 'source'],
    description:
      "``csv``: exactly one of ``csv_text`` (a pasted table or a small file\nread by the browser, <= 300,000 characters) or ``upload_intent_id`` (a\nfinalized ``results_import`` upload, <= 10 MB, <= 5,000 rows).\n``manual``: ``rows`` (<= 2,000).\n``meta_sync``: nothing else -- the synced Meta metrics of ads bound to\nthis product's images. ``currency`` applies to rows without one.",
  },
  ResultsImportRow: {
    properties: {
      line: { type: 'integer' },
      status: {
        type: 'string',
        enum: ['ready', 'replaces', 'unattributable', 'rejected'],
      },
      status_label: { type: 'string' },
      reason_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reason_params: {
        anyOf: [
          { additionalProperties: { type: 'string' }, type: 'object' },
          { type: 'null' },
        ],
      },
      asset_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      asset_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      description: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      description_code: {
        anyOf: [
          { type: 'string', enum: ['external_asset', 'other_asset'] },
          { type: 'null' },
        ],
      },
      round_number: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      evidence_mode: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      input: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['line', 'status', 'status_label'],
  },
  ResultsImportSummary: {
    properties: {
      total_rows: { type: 'integer', default: 0 },
      importable_rows: { type: 'integer', default: 0 },
      by_status: { additionalProperties: { type: 'integer' }, type: 'object' },
      rejected_reasons: {
        items: { $ref: '#/components/schemas/RejectedReason' },
        type: 'array',
      },
      evidence_modes: {
        additionalProperties: { type: 'integer' },
        type: 'object',
      },
      impressions: { type: 'integer', default: 0 },
      clicks: { type: 'integer', default: 0 },
      attributable_assets: { type: 'integer', default: 0 },
      columns: { additionalProperties: { type: 'string' }, type: 'object' },
      ignored_columns: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
    description:
      'Counts by row status (``ready`` / ``replaces`` / ``unattributable``\nimport; ``rejected`` does not), why rows were rejected, rows per\nevidence partition, and which columns were read or ignored.',
  },
  ResultsImportView: {
    properties: {
      import_id: { type: 'string' },
      product_id: { type: 'string' },
      source: { type: 'string' },
      status: { type: 'string', enum: ['previewed', 'committed'] },
      file_name: { type: 'string', default: '' },
      currency: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      summary: { $ref: '#/components/schemas/ResultsImportSummary' },
      rows: {
        items: { $ref: '#/components/schemas/ResultsImportRow' },
        type: 'array',
      },
      rows_truncated: { type: 'boolean', default: false },
      snapshot_ids: { items: { type: 'string' }, type: 'array' },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      committed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      duplicate: { type: 'boolean', default: false },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['import_id', 'product_id', 'source', 'status', 'summary'],
    description:
      'A previewed (``previewed``) or committed (``committed``) import.\n``rows`` lists every rejected row and the first accepted ones\n(``rows_truncated``); ``duplicate`` is true when the same rows were\nalready imported for this product (the existing import is returned).',
  },
  ResultsTemplate: {
    properties: {
      product_id: { type: 'string' },
      file_name: { type: 'string' },
      columns: {
        items: { $ref: '#/components/schemas/TemplateColumn' },
        type: 'array',
      },
      currency: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      rows: {
        items: { $ref: '#/components/schemas/TemplateRow' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['product_id', 'file_name', 'columns'],
    description:
      'The CSV template: ``columns`` in order and one row per image of the\nproduct (code + what it shows); the browser writes the file.',
  },
  ResumeItemResult: {
    properties: {
      item_id: { type: 'string' },
      state: { type: 'string' },
      replayed: { type: 'boolean' },
    },
    type: 'object',
    required: ['item_id', 'state', 'replayed'],
  },
  ResumePausedItemsInput: {
    properties: {
      preview_id: { type: 'string', minLength: 1 },
      preview_hash: { type: 'string', minLength: 1 },
    },
    type: 'object',
    required: ['preview_id', 'preview_hash'],
  },
  Retest: {
    properties: {
      kind: { type: 'string', const: 'retest' },
      original_asset_version_ids: { items: { type: 'string' }, type: 'array' },
      new_execution_ids: { items: { type: 'string' }, type: 'array' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['kind', 'original_asset_version_ids', 'new_execution_ids'],
  },
  RetryFailedItemsInput: {
    properties: {
      preview_id: { type: 'string', minLength: 1 },
      preview_hash: { type: 'string', minLength: 1 },
    },
    type: 'object',
    required: ['preview_id', 'preview_hash'],
  },
  RetryItemResult: {
    properties: {
      item_id: { type: 'string' },
      attempt_number: { type: 'integer' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean' },
    },
    type: 'object',
    required: ['item_id', 'attempt_number', 'replayed'],
  },
  ReuseCreative: {
    properties: {
      asset_version_id: { type: 'string', maxLength: 64, minLength: 1 },
      combination_id: { type: 'string', maxLength: 64, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['asset_version_id', 'combination_id'],
  },
  ReviewCreative: {
    properties: {
      ai_qc_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
      paired_ai_qc_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
      override_note: { type: 'string', maxLength: 2000, default: '' },
      asset_version_id: { type: 'string', maxLength: 64, minLength: 1 },
      quality: {
        type: 'string',
        enum: ['approved', 'needs_changes', 'rejected'],
      },
      checks: {
        additionalProperties: {
          type: 'string',
          enum: ['pass', 'fail', 'unknown'],
        },
        type: 'object',
        maxProperties: 30,
      },
      note: { type: 'string', maxLength: 2000, minLength: 1 },
      compared_asset_version_ids: {
        items: { type: 'string' },
        type: 'array',
        maxItems: 20,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['asset_version_id', 'quality', 'checks', 'note'],
  },
  ReviewItem: {
    properties: {
      id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      kind: {
        type: 'string',
        enum: [
          'angle_overlap',
          'expression_duplicate',
          'evidence_gap',
          'budget_insufficient',
          'image_brief_stale',
          'competitor_crowded',
          'competitor_gap',
          'competitor_copy_reuse',
        ],
      },
      severity: { type: 'string', enum: ['blocking', 'advisory'] },
      target_ids: {
        items: {
          type: 'string',
          maxLength: 64,
          minLength: 1,
          pattern: '^[a-zA-Z0-9_-]+$',
        },
        type: 'array',
        maxItems: 10,
      },
      finding: { type: 'string', maxLength: 2000, minLength: 1 },
      suggestion: { type: 'string', maxLength: 2000, minLength: 1 },
      source: { type: 'string', enum: ['ai', 'system'], default: 'ai' },
      resolved: { type: 'boolean', default: false },
      observations: {
        items: { $ref: '#/components/schemas/ReviewObservation' },
        type: 'array',
        maxItems: 10,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'kind', 'severity', 'finding', 'suggestion'],
    description:
      'A pre-generation finding. System items are recomputed deterministically.',
  },
  ReviewObservation: {
    properties: {
      ad_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      cluster_id: {
        anyOf: [
          {
            type: 'string',
            maxLength: 64,
            minLength: 1,
            pattern: '^[a-zA-Z0-9_-]+$',
          },
          { type: 'null' },
        ],
      },
      advertiser_name: { type: 'string', maxLength: 300, default: '' },
      library_url: { type: 'string', maxLength: 2000, default: '' },
      days_running: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['ad_id'],
    description:
      'A competitor observation a system finding points at: what others publicly run, never evidence (ADR 0014).',
  },
  ReviewSignalFactor: {
    properties: {
      key: { type: 'string' },
      label: { type: 'string' },
      values: {
        items: { $ref: '#/components/schemas/ReviewSignalValue' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['key', 'label'],
  },
  ReviewSignalValue: {
    properties: {
      value_id: { type: 'string' },
      label: { type: 'string' },
      approved: { type: 'integer', default: 0 },
      needs_changes: { type: 'integer', default: 0 },
      rejected: { type: 'integer', default: 0 },
      assets: { type: 'integer', default: 0 },
    },
    type: 'object',
    required: ['value_id', 'label'],
  },
  ReviewSignals: {
    properties: {
      round_number: { type: 'integer' },
      note: { type: 'string' },
      factors: {
        items: { $ref: '#/components/schemas/ReviewSignalFactor' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['round_number', 'note'],
    description:
      'Human review counts per value of the latest round -- a weak signal\nshown only while there is no ad data.',
  },
  RevokeCredentialRequest: {
    properties: { reason: { type: 'string' } },
    additionalProperties: false,
    type: 'object',
    required: ['reason'],
  },
  RightsDeclaration: {
    properties: {
      made_by: {
        type: 'string',
        enum: ['self', 'designer', 'agency', 'creator', 'ai_tool', 'other'],
        default: 'self',
      },
      permission_confirmed: { type: 'boolean', default: false },
      note: { anyOf: [{ type: 'string', maxLength: 500 }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    description:
      "Who made the images and the uploader's confirmation that the\nWorkspace may use them in ads (``permission_confirmed`` must be true).",
  },
  RightsView: {
    properties: {
      made_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      permission_confirmed: { anyOf: [{ type: 'boolean' }, { type: 'null' }] },
      note: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      declared_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      declared_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
  },
  RotateCredentialRequest: {
    properties: { new_plaintext_b64: { type: 'string' } },
    additionalProperties: false,
    type: 'object',
    required: ['new_plaintext_b64'],
  },
  RoundClose: {
    properties: {
      reason: {
        type: 'string',
        enum: [
          'completed',
          'asset_only',
          'no_qualified',
          'insufficient_evidence',
          'user_stopped',
        ],
      },
      note: { type: 'string', maxLength: 2000, minLength: 1 },
      evaluation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      plan_hash: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      closed_at: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'reason',
      'note',
      'evaluation_id',
      'plan_hash',
      'execution_mode',
      'closed_at',
    ],
  },
  RoundContinuation: {
    properties: {
      can_plan: { type: 'boolean' },
      remaining_budget: { type: 'string' },
      remaining_rounds: { type: 'integer' },
      limit_reached: { type: 'boolean' },
      completed: { type: 'boolean' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'can_plan',
      'remaining_budget',
      'remaining_rounds',
      'limit_reached',
      'completed',
    ],
  },
  RoundLearning: {
    properties: {
      round_id: { type: 'string' },
      plan_id: { type: 'string' },
      round_number: { type: 'integer' },
      label: { type: 'string' },
      goal: { type: 'string' },
      plan_created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      totals: { $ref: '#/components/schemas/RoundTotals' },
      factors: {
        items: { $ref: '#/components/schemas/FactorLearning' },
        type: 'array',
      },
      directions: {
        items: { $ref: '#/components/schemas/DirectionLearning' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'round_id',
      'plan_id',
      'round_number',
      'label',
      'goal',
      'totals',
    ],
    description: 'One test plan\'s results ("第 N 轮").',
  },
  RoundSnapshot: {
    properties: {
      version: { type: 'integer', const: 1 },
      plan: { $ref: '#/components/schemas/FrozenTestPlan' },
      plan_hash: { type: 'string' },
      understanding: { $ref: '#/components/schemas/Understanding' },
      protocol: { $ref: '#/components/schemas/EvaluationProtocol-Output' },
      currency: {
        type: 'string',
        enum: [
          'AED',
          'AUD',
          'BRL',
          'CAD',
          'CHF',
          'CNY',
          'EUR',
          'GBP',
          'HKD',
          'IDR',
          'INR',
          'JPY',
          'KRW',
          'MXN',
          'MYR',
          'NZD',
          'PHP',
          'PLN',
          'SAR',
          'SEK',
          'SGD',
          'THB',
          'TWD',
          'USD',
          'VND',
          'ZAR',
        ],
      },
      attribution_window: {
        type: 'string',
        enum: [
          '1d_click',
          '7d_click',
          '1d_view',
          '1d_click_1d_view',
          '7d_click_1d_view',
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'version',
      'plan',
      'plan_hash',
      'understanding',
      'protocol',
      'currency',
      'attribution_window',
    ],
  },
  RoundSummary: {
    properties: {
      id: { type: 'string' },
      ordinal: { type: 'integer' },
      state: {
        type: 'string',
        enum: ['reviewing', 'handoff', 'awaiting_data', 'evaluated', 'closed'],
      },
      close: {
        anyOf: [{ $ref: '#/components/schemas/RoundClose' }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'ordinal', 'state', 'close'],
  },
  RoundTotals: {
    properties: {
      impressions: { type: 'integer', default: 0 },
      clicks: { type: 'integer', default: 0 },
      spend: { type: 'string', default: '0.00' },
      conversions: { type: 'string', default: '0' },
      assets: { type: 'integer', default: 0 },
      ads: { type: 'integer', default: 0 },
      ctr: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      cpa: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      cpa_assets: { type: 'integer', default: 0 },
    },
    type: 'object',
  },
  RunCommandResult: {
    properties: {
      operation_id: { type: 'string' },
      run_id: { type: 'string' },
    },
    type: 'object',
    required: ['operation_id', 'run_id'],
    description:
      'Result of a Run mutation command (cancel/retry/resume/reconcile).',
  },
  RunFailure: {
    properties: {
      code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      detail: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
  },
  SampleSize: {
    properties: {
      impressions: { type: 'integer', default: 0 },
      clicks: { type: 'integer', default: 0 },
      spend: { type: 'string', default: '0.00' },
      conversions: { type: 'string', default: '0' },
      assets: { type: 'integer', default: 0 },
      ads: { type: 'integer', default: 0 },
    },
    type: 'object',
  },
  SaveDraftInput: {
    properties: { content: { additionalProperties: true, type: 'object' } },
    type: 'object',
    required: ['content'],
  },
  SecondaryObservation: {
    properties: {
      kind: { type: 'string', enum: ['expression_coverage', 'same_angle'] },
      angle_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['kind', 'angle_id'],
  },
  StageCounts: {
    properties: {
      scale: { type: 'string' },
      cap: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      combinations: { type: 'integer' },
      variants: { type: 'integer' },
      images: { type: 'integer' },
      max_cost: { $ref: '#/components/schemas/MaxCost' },
      untested: { $ref: '#/components/schemas/UntestedSummary' },
    },
    type: 'object',
    required: [
      'scale',
      'combinations',
      'variants',
      'images',
      'max_cost',
      'untested',
    ],
    description:
      "One scale's planner output priced with the current defaults:\n``images = combinations x variants``; ``max_cost`` is 0 under Mock.",
  },
  StartAIQC: {
    properties: {
      quote_hash: { type: 'string', maxLength: 64, minLength: 64 },
      kind: { type: 'string', enum: ['single', 'paired'], default: 'single' },
      strategy_language: {
        anyOf: [{ type: 'string', enum: ['zh-CN', 'en-US'] }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['quote_hash'],
  },
  StartExperimentRequest: {
    properties: { expected_revision: { type: 'integer', minimum: 1 } },
    type: 'object',
    required: ['expected_revision'],
  },
  StartPreparationInput: {
    properties: {
      truth_resolution_policy_version_id: { type: 'string' },
      manual_fact_set_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      analysis_recipe_hash: { type: 'string' },
      analysis_provider: { type: 'string' },
      analysis_model: { type: 'string' },
      analysis_adapter_version: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'truth_resolution_policy_version_id',
      'analysis_recipe_hash',
      'analysis_provider',
      'analysis_model',
      'analysis_adapter_version',
    ],
  },
  StartPreparationResult: {
    properties: {
      batch_id: { type: 'string' },
      truth_package_id: { type: 'string' },
      truth_status: { type: 'string' },
      preparation_stage: {
        type: 'string',
        enum: ['analysis', 'truth', 'claim_review', 'copy', 'complete'],
      },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      missing_analysis: { items: { type: 'string' }, type: 'array' },
      replayed: { type: 'boolean' },
    },
    type: 'object',
    required: [
      'batch_id',
      'truth_package_id',
      'truth_status',
      'preparation_stage',
      'replayed',
    ],
  },
  StrategyApplied: {
    properties: {
      kind: { type: 'string' },
      cap: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      deprecated_shape: { type: 'boolean', default: false },
      full_count: { type: 'integer', default: 0 },
      candidate_count: { type: 'integer', default: 0 },
      selected_count: { type: 'integer', default: 0 },
    },
    type: 'object',
    required: ['kind'],
    description:
      'The combination strategy the planner actually applied (Issue #157).\n\n``kind`` is ``full`` or ``coverage``; ``cap`` is set for ``coverage``.\n``deprecated_shape`` is ``True`` when the Draft still carries the legacy\n``{mode, sample_cap}`` shape the server mapped onto ``kind``/``cap``.\n``full_count`` is the Cartesian product, ``candidate_count`` what was\nleft after template constraints and exclusion rules, ``selected_count``\nwhat sampling kept (equals ``combination_count`` unless Loser exclusion\nremoved more).',
  },
  SubmitClaimDecisionsInput: {
    properties: {
      asset_version_id: { type: 'string' },
      target_market: { type: 'string' },
      analysis_recipe_hash: { type: 'string' },
      decisions: {
        items: { $ref: '#/components/schemas/ClaimDecisionInputModel' },
        type: 'array',
        minItems: 1,
      },
      copy_policy_snapshot: { additionalProperties: true, type: 'object' },
      source_locale: { type: 'string' },
      target_locale: { type: 'string' },
      translation_recipe: {
        anyOf: [
          { $ref: '#/components/schemas/TranslationRecipeInput' },
          { type: 'null' },
        ],
      },
      requested_evidence_tier: {
        type: 'string',
        enum: ['mock', 'sandbox', 'production'],
      },
    },
    type: 'object',
    required: [
      'asset_version_id',
      'target_market',
      'analysis_recipe_hash',
      'decisions',
      'source_locale',
      'target_locale',
      'requested_evidence_tier',
    ],
  },
  SubmitClaimDecisionsResult: {
    properties: {
      batch_id: { type: 'string' },
      decision_set_id: { type: 'string' },
      copy_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      copy_origin: { type: 'string' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean' },
    },
    type: 'object',
    required: ['batch_id', 'decision_set_id', 'copy_origin', 'replayed'],
  },
  SubmitRunInput: {
    properties: {
      preview_id: { type: 'string', minLength: 1 },
      preview_hash: { type: 'string', minLength: 1 },
    },
    type: 'object',
    required: ['preview_id', 'preview_hash'],
  },
  SuggestedTag: {
    properties: {
      factor_id: { type: 'string' },
      value_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confidence: { type: 'number', default: 0 },
      reason: { type: 'string', default: '' },
    },
    type: 'object',
    required: ['factor_id'],
    description:
      'The model\'s pick for one factor: ``value_id`` null means "none of these".',
  },
  SupersedeBindingRequest: {
    properties: {
      ad_account_id: { type: 'string' },
      meta_mode: { type: 'string' },
      access_token_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      app_secret_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['ad_account_id', 'meta_mode'],
  },
  TagChoice: {
    properties: {
      factor_id: { type: 'string', maxLength: 200, minLength: 1 },
      value_id: {
        anyOf: [{ type: 'string', maxLength: 32 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['factor_id'],
  },
  TagConfirmation: {
    properties: {
      revision: { type: 'integer' },
      tags: {
        items: { $ref: '#/components/schemas/ConfirmedTag' },
        type: 'array',
      },
      direction: {
        anyOf: [
          { $ref: '#/components/schemas/ConfirmedDirection' },
          { type: 'null' },
        ],
      },
      plan_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      round: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      attributable: { type: 'boolean', default: false },
      experiment_ready: { type: 'boolean', default: false },
      concept_count: { type: 'integer', default: 0 },
      stable_dimension_signature: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      review_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confirmed_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confirmed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['revision'],
    description:
      'The latest confirmed tags. ``experiment_ready`` says the image has\nconcept identity (it can be frozen into an experiment);\n``attributable`` says learnings can place it in a test round.',
  },
  TagSuggestion: {
    properties: {
      factors: {
        items: { $ref: '#/components/schemas/SuggestedTag' },
        type: 'array',
      },
      summary: { type: 'string', default: '' },
      provider: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      model: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      execution_mode: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      preselect_confidence: { type: 'number', default: 0.5 },
    },
    type: 'object',
  },
  TargetListResponse: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/TargetResponse' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['items'],
  },
  TargetPublicationPreviewRequest: {
    properties: {
      product_version_id: { type: 'string', minLength: 1 },
      offer_version_id: { type: 'string', minLength: 1 },
      market_code: { type: 'string', maxLength: 10, minLength: 1 },
      currency: { type: 'string', maxLength: 3, minLength: 3 },
      target_cpa: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      breakeven_roas: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      minimum_economic_spend: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'product_version_id',
      'offer_version_id',
      'market_code',
      'currency',
    ],
  },
  TargetPublicationPreviewResponse: {
    properties: {
      id: { type: 'string' },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      market_code: { type: 'string' },
      currency: { type: 'string' },
      target_cpa: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      breakeven_roas: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      minimum_economic_spend: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      affected_enrollment_count: { type: 'integer' },
      policy_floor_check_passed: { type: 'boolean' },
      impact_hash: { type: 'string' },
      expires_at: { type: 'string', format: 'date-time' },
      created_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'product_version_id',
      'offer_version_id',
      'market_code',
      'currency',
      'target_cpa',
      'breakeven_roas',
      'minimum_economic_spend',
      'affected_enrollment_count',
      'policy_floor_check_passed',
      'impact_hash',
      'expires_at',
      'created_at',
    ],
  },
  TargetPublishRequest: {
    properties: {
      publication_preview_id: { type: 'string', maxLength: 64, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['publication_preview_id'],
  },
  TargetResponse: {
    properties: {
      id: { type: 'string' },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      market_code: { type: 'string' },
      currency: { type: 'string' },
      target_cpa: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      breakeven_roas: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      minimum_economic_spend: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      lifecycle_state: { type: 'string' },
      creator_id: { type: 'string' },
      revision: { type: 'integer' },
      published_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      withdrawn_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      created_at: { type: 'string', format: 'date-time' },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibilityModel' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'product_version_id',
      'offer_version_id',
      'market_code',
      'currency',
      'target_cpa',
      'breakeven_roas',
      'minimum_economic_spend',
      'lifecycle_state',
      'creator_id',
      'revision',
      'published_at',
      'withdrawn_at',
      'created_at',
      'allowed_actions',
    ],
  },
  TargetWithdrawRequest: {
    properties: {
      withdrawal_preview_id: { type: 'string', maxLength: 64, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['withdrawal_preview_id'],
  },
  TargetWithdrawalPreviewRequest: {
    properties: { reason: { type: 'string', maxLength: 200, minLength: 1 } },
    additionalProperties: false,
    type: 'object',
    required: ['reason'],
  },
  TargetWithdrawalPreviewResponse: {
    properties: {
      id: { type: 'string' },
      target_version_id: { type: 'string' },
      reason: { type: 'string' },
      affected_enrollment_count: { type: 'integer' },
      dependent_advice_thread_count: { type: 'integer' },
      impact_hash: { type: 'string' },
      expires_at: { type: 'string', format: 'date-time' },
      created_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'target_version_id',
      'reason',
      'affected_enrollment_count',
      'dependent_advice_thread_count',
      'impact_hash',
      'expires_at',
      'created_at',
    ],
  },
  TemplateColumn: {
    properties: { key: { type: 'string' }, label: { type: 'string' } },
    type: 'object',
    required: ['key', 'label'],
  },
  TemplateDiffReportResponse: {
    properties: {
      base_template_version_id: { type: 'string' },
      new_template_version_id: { type: 'string' },
      schema_version_id: { type: 'string' },
      items: {
        items: { additionalProperties: true, type: 'object' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'base_template_version_id',
      'new_template_version_id',
      'schema_version_id',
    ],
    description:
      '``POST /library/versions/{id}/reapply-preview`` (templates.reapply_template).',
  },
  TemplateRow: {
    properties: {
      asset_code: { type: 'string' },
      round_number: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      direction: { type: 'string' },
      combination: { type: 'string' },
      attributable: { type: 'boolean' },
      evidence_mode: { type: 'string' },
    },
    type: 'object',
    required: [
      'asset_code',
      'direction',
      'combination',
      'attributable',
      'evidence_mode',
    ],
  },
  TestGroup: {
    properties: {
      id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[a-zA-Z0-9_-]+$',
      },
      name: { type: 'string', maxLength: 2000, minLength: 1 },
      combination_ids: {
        items: {
          type: 'string',
          maxLength: 64,
          minLength: 1,
          pattern: '^[a-zA-Z0-9_-]+$',
        },
        type: 'array',
        maxItems: 30,
        minItems: 1,
      },
      rationale: { type: 'string', maxLength: 2000, minLength: 1 },
      style: { type: 'string', maxLength: 300, default: '' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'name', 'combination_ids', 'rationale'],
  },
  TestPlan: {
    properties: {
      stage: {
        type: 'string',
        enum: [
          'screen_angles',
          'match_expressions',
          'confirm_combination',
          'optimize_elements',
        ],
      },
      question: { type: 'string', maxLength: 2000, minLength: 1 },
      comparison_kind: {
        type: 'string',
        enum: ['whole_combinations', 'specific_factor', 'business_retest'],
      },
      changed: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
        minItems: 1,
      },
      allowed_changes: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
      },
      held_constant: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
        minItems: 1,
      },
      limitations: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
        minItems: 1,
      },
      limited_expression_coverage: { type: 'boolean', default: false },
      conclusion_scope: {
        type: 'string',
        enum: ['asset', 'combination', 'specific_factor'],
      },
      audience: { type: 'string', maxLength: 2000, minLength: 1 },
      market: { type: 'string', maxLength: 2000, minLength: 1 },
      ad_language: {
        type: 'string',
        maxLength: 35,
        minLength: 2,
        pattern: '^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$',
      },
      strategy_language: {
        type: 'string',
        enum: ['zh-CN', 'en-US'],
        default: 'en-US',
      },
      language_confirmation_required: { type: 'boolean', default: false },
      placement: { type: 'string', maxLength: 2000, minLength: 1 },
      offer: { type: 'string', maxLength: 2000, minLength: 1 },
      landing_page: { type: 'string', maxLength: 2000, minLength: 1 },
      baseline: { type: 'string', maxLength: 2000, minLength: 1 },
      currency: {
        type: 'string',
        enum: [
          'AED',
          'AUD',
          'BRL',
          'CAD',
          'CHF',
          'CNY',
          'EUR',
          'GBP',
          'HKD',
          'IDR',
          'INR',
          'JPY',
          'KRW',
          'MXN',
          'MYR',
          'NZD',
          'PHP',
          'PLN',
          'SAR',
          'SEK',
          'SGD',
          'THB',
          'TWD',
          'USD',
          'VND',
          'ZAR',
        ],
        default: 'USD',
      },
      media_budget: {
        anyOf: [
          { type: 'number', exclusiveMinimum: 0 },
          {
            type: 'string',
            pattern:
              '^(?!^[-+.]*$)[+-]?0*(?:\\d{0,12}|(?=[\\d.]{1,15}0*$)\\d{0,12}\\.\\d{0,2}0*$)',
          },
        ],
      },
      small_round_budget: {
        anyOf: [
          { type: 'number', exclusiveMinimum: 0 },
          {
            type: 'string',
            pattern:
              '^(?!^[-+.]*$)[+-]?0*(?:\\d{0,12}|(?=[\\d.]{1,15}0*$)\\d{0,12}\\.\\d{0,2}0*$)',
          },
        ],
      },
      duration_days: { type: 'integer', maximum: 90, minimum: 1 },
      allocation: { type: 'string', maxLength: 2000, minLength: 1 },
      rules: { $ref: '#/components/schemas/DecisionRules-Input' },
      executions_per_combination: {
        type: 'integer',
        maximum: 4,
        minimum: 1,
        default: 2,
      },
      angles: {
        items: { $ref: '#/components/schemas/Angle' },
        type: 'array',
        maxItems: 8,
        minItems: 1,
      },
      combinations: {
        items: { $ref: '#/components/schemas/CreativeCombination' },
        type: 'array',
        maxItems: 30,
        minItems: 1,
      },
      groups: {
        items: { $ref: '#/components/schemas/TestGroup' },
        type: 'array',
        maxItems: 12,
        minItems: 1,
      },
      plan_review: {
        items: { $ref: '#/components/schemas/ReviewItem' },
        type: 'array',
        maxItems: 20,
      },
      guiding_questions: {
        items: { $ref: '#/components/schemas/GuidingQuestion' },
        type: 'array',
        maxItems: 3,
      },
      style_variation: {
        type: 'string',
        enum: ['held', 'tested'],
        default: 'held',
      },
      style_notes: { type: 'string', maxLength: 500, default: '' },
      brand_kit: {
        anyOf: [{ $ref: '#/components/schemas/BrandKitRef' }, { type: 'null' }],
      },
      competitor_snapshot_id: {
        anyOf: [
          {
            type: 'string',
            maxLength: 64,
            minLength: 1,
            pattern: '^[a-zA-Z0-9_-]+$',
          },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'stage',
      'question',
      'comparison_kind',
      'changed',
      'held_constant',
      'limitations',
      'conclusion_scope',
      'audience',
      'market',
      'ad_language',
      'placement',
      'offer',
      'landing_page',
      'baseline',
      'media_budget',
      'small_round_budget',
      'duration_days',
      'allocation',
      'rules',
      'angles',
      'combinations',
      'groups',
      'plan_review',
      'guiding_questions',
    ],
  },
  TestPlanAccepted: {
    properties: {
      plan_id: { type: 'string' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: { type: 'string' },
      revision: { type: 'integer' },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['plan_id', 'status', 'revision'],
    description:
      '202: poll ``GET /operations/{operation_id}`` or ``GET /test-plans/{plan_id}``.',
  },
  TestPlanCreateRequest: {
    properties: {
      product_id: { type: 'string', maxLength: 64, minLength: 1 },
      goal: {
        type: 'string',
        enum: ['explore', 'optimize'],
        default: 'explore',
      },
      audience_text: {
        anyOf: [{ type: 'string', maxLength: 500 }, { type: 'null' }],
      },
      audience_card_index: {
        anyOf: [{ type: 'integer', maximum: 20, minimum: 0 }, { type: 'null' }],
      },
      scale: {
        type: 'string',
        enum: ['sample', 'small', 'full'],
        default: 'sample',
      },
      seed_asset_version_ids: {
        items: { type: 'string' },
        type: 'array',
        maxItems: 10,
      },
      from_learnings_snapshot_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id'],
    description:
      "The three questions (contract §3.2), each with a default: ``goal``\n``explore``; ``audience_card_index`` the product's first audience card\n(or ``audience_text`` to word it yourself); ``scale`` ``sample``.\n``optimize`` needs 1-10 ``seed_asset_version_ids`` (Approved assets or\nWinners of this Workspace).",
  },
  TestPlanDetail: {
    properties: {
      plan_id: { type: 'string' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: { type: 'string' },
      revision: { type: 'integer' },
      product_id: { type: 'string' },
      product_entry_id: { type: 'string' },
      product_version_id: { type: 'string' },
      product_name: { type: 'string' },
      goal: { type: 'string' },
      audience: { $ref: '#/components/schemas/AudienceView' },
      scale: { type: 'string' },
      seed_asset_version_ids: { items: { type: 'string' }, type: 'array' },
      learnings_basis: {
        anyOf: [
          { $ref: '#/components/schemas/LearningsBasis' },
          { type: 'null' },
        ],
      },
      failure: {
        anyOf: [{ $ref: '#/components/schemas/PlanFailure' }, { type: 'null' }],
      },
      factors: {
        items: { $ref: '#/components/schemas/FactorView' },
        type: 'array',
      },
      constraints: {
        items: { additionalProperties: true, type: 'object' },
        type: 'array',
      },
      directions: {
        items: { $ref: '#/components/schemas/DirectionView' },
        type: 'array',
      },
      stages: {
        additionalProperties: { $ref: '#/components/schemas/StageCounts' },
        type: 'object',
      },
      selected_stage: {
        anyOf: [{ $ref: '#/components/schemas/StageCounts' }, { type: 'null' }],
      },
      expansion: {
        anyOf: [{ $ref: '#/components/schemas/Expansion' }, { type: 'null' }],
      },
      defaults: { $ref: '#/components/schemas/GenerationDefaultsView' },
      runs: {
        items: { $ref: '#/components/schemas/TestPlanRunView' },
        type: 'array',
      },
      drafting: { additionalProperties: true, type: 'object' },
      planner_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      drafted_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'plan_id',
      'status',
      'revision',
      'product_id',
      'product_entry_id',
      'product_version_id',
      'product_name',
      'goal',
      'audience',
      'scale',
      'defaults',
    ],
    description:
      '``status``: ``drafting`` (poll ``operation_id``) -> ``ready`` or\n``failed``. ``stages`` has ``sample`` / ``small`` / ``full`` counts and\nmax cost; ``selected_stage`` is the plan\'s own scale; ``expansion`` is\nwhat "展开完整测试" would cost now; ``runs`` are the linked Runs.',
  },
  TestPlanList: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/TestPlanSummary' },
        type: 'array',
      },
    },
    type: 'object',
  },
  TestPlanResults: {
    properties: {
      plan_id: { type: 'string' },
      product_id: { type: 'string' },
      status: { type: 'string', enum: ['no_data', 'ready'] },
      evidence_mode: {
        anyOf: [
          { type: 'string', enum: ['production', 'sandbox', 'mock'] },
          { type: 'null' },
        ],
      },
      evidence_notice: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      available_modes: { items: { type: 'string' }, type: 'array' },
      snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      computed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      thresholds: { additionalProperties: { type: 'string' }, type: 'object' },
      round: {
        anyOf: [
          { $ref: '#/components/schemas/RoundLearning' },
          { type: 'null' },
        ],
      },
      can_seed_next_round: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['plan_id', 'product_id', 'status'],
    description:
      "One plan's round of the latest snapshot (directions with metrics).",
  },
  TestPlanReviseRequest: {
    properties: {
      remove: { items: { type: 'string' }, type: 'array', maxItems: 8 },
      restore: { items: { type: 'string' }, type: 'array', maxItems: 8 },
      edit: {
        items: { $ref: '#/components/schemas/DirectionEdit' },
        type: 'array',
        maxItems: 8,
      },
      add: {
        items: { $ref: '#/components/schemas/DirectionAdd' },
        type: 'array',
        maxItems: 8,
      },
      scale: {
        anyOf: [
          { type: 'string', enum: ['sample', 'small', 'full'] },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    description:
      'Remove / restore / edit / add directions and optionally change the\nscale; the plan is re-planned and its counts and cost updated.',
  },
  TestPlanRunRequest: {
    properties: { stage: { type: 'string', enum: ['sample', 'full'] } },
    additionalProperties: false,
    type: 'object',
    required: ['stage'],
    description:
      "``sample``: one combination per direction. ``full``: the plan's\nsmall/full selection (``full`` when the plan's scale is ``sample``),\nskipping combinations the sample Run already produced.",
  },
  TestPlanRunResult: {
    properties: {
      stage: { type: 'string' },
      target_scale: { type: 'string' },
      status: { type: 'string' },
      plan_revision: { type: 'integer' },
      run_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      run_status: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      items_total: { type: 'integer', default: 0 },
      items_done: { type: 'integer', default: 0 },
      items_failed: { type: 'integer', default: 0 },
      counts: { additionalProperties: true, type: 'object' },
      defaults: { additionalProperties: true, type: 'object' },
      template_entry_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      template_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      generation_draft_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      preview_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      failure: {
        anyOf: [{ $ref: '#/components/schemas/RunFailure' }, { type: 'null' }],
      },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      plan_id: { type: 'string' },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['stage', 'target_scale', 'status', 'plan_revision', 'plan_id'],
  },
  TestPlanRunView: {
    properties: {
      stage: { type: 'string' },
      target_scale: { type: 'string' },
      status: { type: 'string' },
      plan_revision: { type: 'integer' },
      run_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      run_status: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      items_total: { type: 'integer', default: 0 },
      items_done: { type: 'integer', default: 0 },
      items_failed: { type: 'integer', default: 0 },
      counts: { additionalProperties: true, type: 'object' },
      defaults: { additionalProperties: true, type: 'object' },
      template_entry_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      template_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      generation_draft_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      preview_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      failure: {
        anyOf: [{ $ref: '#/components/schemas/RunFailure' }, { type: 'null' }],
      },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['stage', 'target_scale', 'status', 'plan_revision'],
    description:
      "A plan stage's Run. ``status``: ``materializing`` -> ``submitted``\n(or ``failed``, retryable by replaying the stage). ``run_status`` and\n``items_*`` mirror the GenerationRun; poll ``operation_id`` through\n``GET /operations/{id}`` or the Run through ``GET /generation/runs/{id}``.",
  },
  TestPlanSummary: {
    properties: {
      plan_id: { type: 'string' },
      status: { type: 'string' },
      revision: { type: 'integer' },
      product_id: { type: 'string' },
      product_name: { type: 'string' },
      goal: { type: 'string' },
      scale: { type: 'string' },
      direction_count: { type: 'integer' },
      run_stages: { items: { type: 'string' }, type: 'array' },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'plan_id',
      'status',
      'revision',
      'product_id',
      'product_name',
      'goal',
      'scale',
      'direction_count',
    ],
  },
  TextProviderReceipt: {
    properties: {
      request_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      response_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finish_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      usage: {
        anyOf: [
          {
            additionalProperties: {
              anyOf: [
                { type: 'integer' },
                { additionalProperties: { type: 'integer' }, type: 'object' },
                {
                  items: { $ref: '#/components/schemas/ModalityTokenUsage' },
                  type: 'array',
                },
              ],
            },
            type: 'object',
          },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
  },
  ThumbnailLink: {
    properties: {
      url: { type: 'string' },
      expires_at: { type: 'string' },
      mime_type: { type: 'string' },
      width: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      height: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['url', 'expires_at', 'mime_type'],
  },
  TranslationRecipeInput: {
    properties: {
      execution_mode: { type: 'string', enum: ['byok', 'sandbox', 'mock'] },
      provider: { type: 'string' },
      model: { type: 'string' },
      adapter_version: { type: 'string' },
    },
    type: 'object',
    required: ['execution_mode', 'provider', 'model', 'adapter_version'],
  },
  UnattributableAsset: {
    properties: {
      asset_code: { type: 'string' },
      reason_code: { type: 'string' },
      reason: { type: 'string' },
      impressions: { type: 'integer', default: 0 },
    },
    type: 'object',
    required: ['asset_code', 'reason_code', 'reason'],
  },
  Understanding: {
    properties: {
      product_name: { type: 'string', maxLength: 160, minLength: 1 },
      category_need: { type: 'string', maxLength: 2000, minLength: 1 },
      product_choice_reason: { type: 'string', maxLength: 2000, minLength: 1 },
      audience: { type: 'string', maxLength: 2000, minLength: 1 },
      situations: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
        minItems: 1,
      },
      objections: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
      },
      facts: {
        items: { $ref: '#/components/schemas/Fact' },
        type: 'array',
        maxItems: 30,
      },
      inferences: {
        items: { $ref: '#/components/schemas/Inference' },
        type: 'array',
        maxItems: 30,
        minItems: 1,
      },
      unknowns: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 30,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'product_name',
      'category_need',
      'product_choice_reason',
      'audience',
      'situations',
      'inferences',
    ],
  },
  UntestedSummary: {
    properties: {
      count: { type: 'integer' },
      space_count: { type: 'integer' },
      constraint_excluded_count: { type: 'integer' },
      by_direction: {
        items: { $ref: '#/components/schemas/UntestedSummaryDirection' },
        type: 'array',
      },
      examples: {
        items: { additionalProperties: { type: 'string' }, type: 'object' },
        type: 'array',
      },
      summary: { type: 'string' },
    },
    type: 'object',
    required: ['count', 'space_count', 'constraint_excluded_count', 'summary'],
    description:
      'Combinations a scale does not generate (shown on the plan card).',
  },
  UntestedSummaryDirection: {
    properties: {
      direction_id: { type: 'string' },
      title: { type: 'string' },
      untested_count: { type: 'integer' },
    },
    type: 'object',
    required: ['direction_id', 'title', 'untested_count'],
  },
  UpdateDraftInput: {
    properties: {
      product_id: {
        anyOf: [{ type: 'string', minLength: 1 }, { type: 'null' }],
      },
      provider: { anyOf: [{ type: 'string', minLength: 1 }, { type: 'null' }] },
      model: { anyOf: [{ type: 'string', minLength: 1 }, { type: 'null' }] },
      execution_mode: {
        anyOf: [{ type: 'string', minLength: 1 }, { type: 'null' }],
      },
      selectors: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      combination_strategy: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      format_config: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      exclusions: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
  },
  UpdateExperimentContextRequest: {
    properties: {
      draft_context: { additionalProperties: true, type: 'object' },
      expected_revision: { type: 'integer', minimum: 1 },
    },
    type: 'object',
    required: ['draft_context', 'expected_revision'],
  },
  UpdateExperimentEntryRequest: {
    properties: {
      asset_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      marketing_vars: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      expected_revision: { type: 'integer', minimum: 1 },
    },
    type: 'object',
    required: ['expected_revision'],
  },
  UpdateProductReference: {
    properties: {
      role: {
        anyOf: [
          {
            type: 'string',
            enum: ['primary', 'detail', 'logo', 'certification_mark'],
          },
          { type: 'null' },
        ],
      },
      identity_status: {
        anyOf: [
          { type: 'string', enum: ['verified_physical', 'unverified'] },
          { type: 'null' },
        ],
      },
      note: { anyOf: [{ type: 'string', maxLength: 1200 }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
  },
  UploadGrantResponse: {
    properties: {
      kind: { type: 'string', enum: ['presigned_post', 'signed_put'] },
      url: { type: 'string' },
      fields: {
        anyOf: [
          { additionalProperties: { type: 'string' }, type: 'object' },
          { type: 'null' },
        ],
      },
      headers: {
        anyOf: [
          { additionalProperties: { type: 'string' }, type: 'object' },
          { type: 'null' },
        ],
      },
      expires_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['kind', 'url', 'expires_at'],
  },
  UploadIntentResponse: {
    properties: {
      intent_id: { type: 'string' },
      state: {
        type: 'string',
        enum: ['pending', 'verifying', 'finalized', 'rejected', 'expired'],
      },
      purpose: { type: 'string' },
      expires_at: { type: 'string', format: 'date-time' },
      upload_grant: { $ref: '#/components/schemas/UploadGrantResponse' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['intent_id', 'state', 'purpose', 'expires_at', 'upload_grant'],
    description:
      'Declared only so `generate_openapi.py` can extract a real response\nschema (see the identical note on `IdentityProbeResponse`); the handler\nreturns a `JSONResponse` directly.',
  },
  ValueLearning: {
    properties: {
      value_id: { type: 'string' },
      label: { type: 'string' },
      compared_with: { type: 'string' },
      verdict: {
        type: 'string',
        enum: ['winner', 'loser', 'no_difference', 'insufficient_data'],
      },
      verdict_metric: { type: 'string', enum: ['ctr', 'cpa'] },
      flags: { items: { type: 'string' }, type: 'array' },
      sentence: { type: 'string' },
      n: { $ref: '#/components/schemas/SampleSize' },
      ctr: { $ref: '#/components/schemas/MetricComparison' },
      cpa: { $ref: '#/components/schemas/MetricComparison' },
    },
    type: 'object',
    required: [
      'value_id',
      'label',
      'compared_with',
      'verdict',
      'verdict_metric',
      'sentence',
      'n',
      'ctr',
      'cpa',
    ],
    description:
      'A factor value: headline ``verdict`` on ``verdict_metric`` and the\none-line ``sentence``; both metrics in full.',
  },
  Vocabulary: {
    properties: {
      factors: {
        items: { $ref: '#/components/schemas/VocabularyFactor' },
        type: 'array',
      },
      directions: {
        items: { $ref: '#/components/schemas/VocabularyDirection' },
        type: 'array',
      },
    },
    type: 'object',
  },
  VocabularyDirection: {
    properties: {
      plan_id: { type: 'string' },
      round: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      direction_key: { type: 'string' },
      title: { type: 'string' },
    },
    type: 'object',
    required: ['plan_id', 'direction_key', 'title'],
  },
  VocabularyFactor: {
    properties: {
      factor_id: { type: 'string' },
      label: { type: 'string' },
      source: { type: 'string' },
      values: {
        items: { $ref: '#/components/schemas/VocabularyValue' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['factor_id', 'label', 'source'],
    description:
      "A factor of the product's vocabulary. ``source`` is ``test_plan``\n(merged across the product's test plans by key),\n``dimension_template`` or ``expression_catalog`` (the expression\nmechanism catalog, one factor every product has; Issue #329).",
  },
  VocabularyValue: {
    properties: { value_id: { type: 'string' }, label: { type: 'string' } },
    type: 'object',
    required: ['value_id', 'label'],
  },
  WithdrawInput: {
    properties: {
      reason: { type: 'string', maxLength: 1000, minLength: 1 },
      impact_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      impact_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['reason'],
  },
  WithinAngle: {
    properties: {
      kind: { type: 'string', const: 'within_angle' },
      angle_id: { type: 'string' },
      expression_ids: { items: { type: 'string' }, type: 'array' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['kind', 'angle_id', 'expression_ids'],
  },
  WorkflowAIQCCall: {
    properties: {
      id: { type: 'string' },
      kind: {
        anyOf: [
          { type: 'string', enum: ['single', 'paired', 'combined'] },
          { type: 'null' },
        ],
      },
      language: {
        anyOf: [{ type: 'string', enum: ['zh-CN', 'en-US'] }, { type: 'null' }],
      },
      state: { type: 'string' },
      provider: { type: 'string' },
      model: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      policy_version: { type: 'string' },
      input_hash: { type: 'string' },
      result: {
        anyOf: [
          { $ref: '#/components/schemas/EffectiveReport' },
          { type: 'null' },
        ],
      },
      error_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      error_detail: {
        anyOf: [
          { $ref: '#/components/schemas/AIQCErrorDetail' },
          { type: 'null' },
        ],
      },
      maximum_cost: { type: 'string' },
      current: { type: 'boolean' },
      compared_asset_version_ids: { items: { type: 'string' }, type: 'array' },
      product_reference_version_ids: {
        items: { type: 'string' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'state',
      'provider',
      'model',
      'execution_mode',
      'policy_version',
      'input_hash',
      'result',
      'error_code',
      'maximum_cost',
      'current',
      'compared_asset_version_ids',
    ],
  },
  WorkflowAsset: {
    properties: {
      parent_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      asset_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      edit_operation: {
        anyOf: [
          {
            type: 'string',
            enum: [
              'generated',
              'crop_rotate',
              'upscale',
              'remove_background',
              'erase',
              'modify',
              'reuse',
            ],
          },
          { type: 'null' },
        ],
      },
      asset_version_id: { type: 'string' },
      combination_id: { type: 'string' },
      headline: { type: 'string' },
      body_copy: { type: 'string' },
      required_checks: {
        additionalProperties: { type: 'string' },
        type: 'object',
      },
      quality: {
        anyOf: [
          { $ref: '#/components/schemas/CreativeQuality' },
          { type: 'null' },
        ],
      },
      ready: { type: 'boolean' },
      mime_type: { type: 'string' },
      ai_qc: {
        anyOf: [
          { $ref: '#/components/schemas/WorkflowAIQCCall' },
          { type: 'null' },
        ],
      },
      paired_ai_qc: {
        anyOf: [
          { $ref: '#/components/schemas/WorkflowAIQCCall' },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'asset_version_id',
      'combination_id',
      'headline',
      'body_copy',
      'required_checks',
      'quality',
      'ready',
      'mime_type',
      'ai_qc',
    ],
  },
  WorkflowBatch: {
    properties: {
      id: { type: 'string' },
      state: { type: 'string' },
      raw_hash: { type: 'string' },
      metadata: { $ref: '#/components/schemas/MetricsMetadata' },
      row_count: { type: 'integer' },
      issues: {
        items: { $ref: '#/components/schemas/MetricsIssue' },
        type: 'array',
      },
      replaces_batch_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'state',
      'raw_hash',
      'metadata',
      'row_count',
      'issues',
      'replaces_batch_id',
    ],
  },
  WorkflowEnvelope: {
    properties: {
      project_id: { type: 'string' },
      rounds: {
        items: { $ref: '#/components/schemas/RoundSummary' },
        type: 'array',
      },
      current: {
        anyOf: [
          { $ref: '#/components/schemas/MarketingRound' },
          { type: 'null' },
        ],
      },
      next_action: { type: 'string' },
      poll_after_ms: { type: 'integer', minimum: 0, default: 0 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['project_id', 'rounds', 'current', 'next_action'],
  },
  WorkflowEvaluation: {
    properties: {
      current: { type: 'boolean', default: true },
      id: { type: 'string' },
      input_hash: { type: 'string' },
      algorithm_version: { type: 'string', const: 'marketing-eval-v1' },
      metric: { type: 'string', enum: ['cpa', 'roas', 'ctr'] },
      threshold: { type: 'string' },
      currency: {
        type: 'string',
        enum: [
          'AED',
          'AUD',
          'BRL',
          'CAD',
          'CHF',
          'CNY',
          'EUR',
          'GBP',
          'HKD',
          'IDR',
          'INR',
          'JPY',
          'KRW',
          'MXN',
          'MYR',
          'NZD',
          'PHP',
          'PLN',
          'SAR',
          'SEK',
          'SGD',
          'THB',
          'TWD',
          'USD',
          'VND',
          'ZAR',
        ],
      },
      attribution_window: {
        type: 'string',
        enum: [
          '1d_click',
          '7d_click',
          '1d_view',
          '1d_click_1d_view',
          '7d_click_1d_view',
        ],
      },
      account_timezone: { type: 'string' },
      evidence_status: {
        type: 'string',
        enum: ['simulated', 'user_supplied_delivery', 'not_supplied'],
      },
      results: {
        items: { $ref: '#/components/schemas/EvaluationResult' },
        type: 'array',
      },
      relative_status: {
        type: 'string',
        enum: [
          'not_compared',
          'insufficient',
          'no_clear_difference',
          'separated_under_observed_conditions',
        ],
      },
      relative_group_ids: { items: { type: 'string' }, type: 'array' },
      qualified_asset_version_ids: { items: { type: 'string' }, type: 'array' },
      confirmed_combinations: {
        items: { $ref: '#/components/schemas/ConfirmedCombination' },
        type: 'array',
      },
      all_assets_failed: { type: 'boolean' },
      round_spend: { type: 'string' },
      small_round_budget: { type: 'string' },
      big_round_spend: { type: 'string' },
      big_round_budget: { type: 'string' },
      mature: { type: 'boolean' },
      next_action: { type: 'string' },
      limitations: { items: { type: 'string' }, type: 'array' },
      factor_conclusion: {
        type: 'string',
        enum: ['conditional_association_only', 'not_established'],
      },
      conditions: { $ref: '#/components/schemas/DeliveryConditions' },
      budget_exhausted: { type: 'boolean' },
      round_limit_reached: { type: 'boolean' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'input_hash',
      'algorithm_version',
      'metric',
      'threshold',
      'currency',
      'attribution_window',
      'account_timezone',
      'evidence_status',
      'results',
      'relative_status',
      'relative_group_ids',
      'qualified_asset_version_ids',
      'confirmed_combinations',
      'all_assets_failed',
      'round_spend',
      'small_round_budget',
      'big_round_spend',
      'big_round_budget',
      'mature',
      'next_action',
      'limitations',
      'factor_conclusion',
      'conditions',
      'budget_exhausted',
      'round_limit_reached',
    ],
  },
  WorkflowExplanation: {
    properties: {
      id: { type: 'string' },
      state: { type: 'string' },
      language: {
        anyOf: [{ type: 'string', enum: ['zh-CN', 'en-US'] }, { type: 'null' }],
      },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      provider: { type: 'string' },
      model: { type: 'string' },
      maximum_cost: { type: 'string' },
      error_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      current: { type: 'boolean' },
      data: {
        anyOf: [
          { $ref: '#/components/schemas/ExplanationData' },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'state',
      'execution_mode',
      'provider',
      'model',
      'maximum_cost',
      'error_code',
      'current',
      'data',
    ],
  },
  WorkflowExplanationReport: {
    properties: {
      focuses: {
        items: { $ref: '#/components/schemas/ExplanationFocus' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['focuses'],
  },
  WorkflowHandoff: {
    properties: {
      quality_current: { type: 'boolean', default: true },
      id: { type: 'string' },
      state: { type: 'string' },
      error_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      manifest_hash: { type: 'string' },
      language: {
        anyOf: [{ type: 'string', enum: ['en', 'zh-CN'] }, { type: 'null' }],
      },
      entries: {
        items: { $ref: '#/components/schemas/HandoffEntry' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'state', 'error_code', 'manifest_hash', 'entries'],
  },
  WorkflowImageEdit: {
    properties: {
      id: { type: 'string' },
      operation_id: { type: 'string' },
      source_asset_version_id: { type: 'string' },
      result_asset_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      state: {
        type: 'string',
        enum: ['queued', 'succeeded', 'unchanged', 'failed'],
      },
      error_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'operation_id',
      'source_asset_version_id',
      'result_asset_version_id',
      'state',
      'error_code',
    ],
  },
  app__generation__response_models__DownloadIntentResponse: {
    properties: {
      intent_id: { type: 'string' },
      expires_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      download_grant: {
        anyOf: [
          { $ref: '#/components/schemas/DownloadGrant' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['intent_id'],
    description:
      'Shared shape for asset-version/export download-intent results.\n\n``download_grant`` is optional because the export-package route\n(``create_generation_export_download_intent``) does not yet issue one\n(Issue #49 Task 9 wires that); the asset-version route always populates\nit.',
  },
  app__generation__v1_router__CreateDraftInput: {
    properties: {
      product_id: { type: 'string', minLength: 1 },
      provider: { type: 'string', minLength: 1 },
      model: { type: 'string', minLength: 1 },
      execution_mode: { type: 'string', minLength: 1 },
      combination_strategy: { additionalProperties: true, type: 'object' },
      format_config: { additionalProperties: true, type: 'object' },
      exclusions: { additionalProperties: true, type: 'object' },
      selectors: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['product_id', 'provider', 'model', 'execution_mode'],
  },
  app__localization__routes__ConfirmInput: {
    properties: {
      edit_provider: { type: 'string' },
      edit_model: { type: 'string' },
      edit_adapter_version: { type: 'string' },
      external_qc_steps: { items: { type: 'string' }, type: 'array' },
      max_attempts: { type: 'integer', maximum: 3, minimum: 1, default: 3 },
    },
    type: 'object',
    required: ['edit_provider', 'edit_model', 'edit_adapter_version'],
  },
  app__localization__routes__CreateDraftInput: {
    properties: {
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      truth_data_mode: { type: 'string', enum: ['real', 'mock'] },
      provider_execution_mode: {
        type: 'string',
        enum: ['byok', 'sandbox', 'mock'],
      },
      sources: {
        items: { $ref: '#/components/schemas/BatchSourceInputModel' },
        type: 'array',
        minItems: 1,
      },
      market_targets: {
        items: { $ref: '#/components/schemas/BatchMarketTargetInputModel' },
        type: 'array',
        minItems: 1,
      },
      requested_evidence_tier: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'product_version_id',
      'offer_version_id',
      'truth_data_mode',
      'provider_execution_mode',
      'sources',
      'market_targets',
    ],
  },
  app__main__DownloadIntentResponse: {
    properties: {
      intent_id: { type: 'string' },
      expires_at: { type: 'string', format: 'date-time' },
      download_grant: { $ref: '#/components/schemas/DownloadGrantResponse' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['intent_id', 'expires_at', 'download_grant'],
    description:
      'See the note on `UploadIntentResponse` -- the handler returns a\n`JSONResponse` directly.',
  },
  app__marketing__api__PlanRequest: {
    properties: {
      kind: { type: 'string', enum: ['understanding', 'plan'] },
      ad_language: {
        anyOf: [
          {
            type: 'string',
            maxLength: 35,
            minLength: 2,
            pattern: '^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$',
          },
          { type: 'null' },
        ],
      },
      strategy_language: {
        type: 'string',
        enum: ['zh-CN', 'en-US'],
        default: 'en-US',
      },
      direction: {
        anyOf: [
          {
            type: 'string',
            enum: [
              'screen_angles',
              'match_expressions',
              'confirm_combination',
              'optimize_elements',
            ],
          },
          { type: 'null' },
        ],
      },
      decision_question: { type: 'string', maxLength: 1200, default: '' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      quote_hash: { type: 'string', maxLength: 64, default: '' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['kind', 'execution_mode'],
  },
};
for (const [componentName, componentSchema] of Object.entries(
  COMPONENT_SCHEMAS
)) {
  ajv.addSchema(componentSchema, `#/components/schemas/${componentName}`);
}

const SCHEMAS: Record<string, object> = {
  problem_details: {
    type: 'object',
    required: ['type', 'title', 'status', 'code', 'trace_id', 'retryable'],
    properties: {
      type: { type: 'string' },
      title: { type: 'string' },
      status: { type: 'integer' },
      detail: { type: 'string' },
      instance: { type: 'string' },
      code: { type: 'string' },
      params: { type: 'object', additionalProperties: true },
      trace_id: { type: 'string' },
      retryable: { type: 'boolean' },
      field_errors: {
        type: 'array',
        items: {
          type: 'object',
          properties: {
            field: { type: 'string' },
            code: { type: 'string' },
            detail: { type: 'string' },
            params: { type: 'object', additionalProperties: true },
          },
        },
      },
    },
  },
  activate_scale_enrollment_200: {
    properties: {
      id: { type: 'string' },
      meta_mode: { type: 'string' },
      meta_connection_id: { type: 'string' },
      scenario_scope_key: { type: 'string' },
      ad_account_id: { type: 'string' },
      winner_decision_id: { type: 'string' },
      winner_confirmation_id: { type: 'string' },
      asset_version_id: { type: 'string' },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      market_code: { type: 'string' },
      landing_page_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      market_asset_binding_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      campaign_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ad_set_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ad_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      remote_binding_key: { type: 'string' },
      lifecycle: { type: 'string' },
      transition_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      current_episode_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      creator_id: { type: 'string' },
      original_initiator_id: { type: 'string' },
      revision: { type: 'integer' },
      evaluation_cancellation_generation: { type: 'integer' },
      created_at: { type: 'string', format: 'date-time' },
      updated_at: { type: 'string', format: 'date-time' },
      closed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibilityModel' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'meta_mode',
      'meta_connection_id',
      'scenario_scope_key',
      'ad_account_id',
      'winner_decision_id',
      'winner_confirmation_id',
      'asset_version_id',
      'product_version_id',
      'offer_version_id',
      'market_code',
      'landing_page_version_id',
      'market_asset_binding_id',
      'campaign_remote_id',
      'ad_set_remote_id',
      'ad_remote_id',
      'remote_binding_key',
      'lifecycle',
      'transition_reason',
      'current_episode_id',
      'creator_id',
      'original_initiator_id',
      'revision',
      'evaluation_cancellation_generation',
      'created_at',
      'updated_at',
      'closed_at',
      'allowed_actions',
    ],
  },
  activate_scale_enrollment_request: {
    properties: {
      activation_preview_id: { type: 'string', maxLength: 64, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['activation_preview_id'],
    description:
      'Issue #86 Task 11: the only field a caller may supply is the id of an\nalready-consumed-server-side ``ScaleActivationPreviewRecord`` --\n``delivery_config``/``episode_context`` (the pre-hardening shape a\nbrowser could use to fabricate any lineage/evidence it liked) are\nrejected as unknown fields by ``_Base``\'s ``extra="forbid"``, surfacing\nas a 422.',
  },
  add_experiment_entry_201: {
    properties: {
      id: { type: 'string' },
      experiment_id: { type: 'string' },
      asset_version_id: { type: 'string' },
      position: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      marketing_vars: { additionalProperties: true, type: 'object' },
      revision: { type: 'integer' },
    },
    type: 'object',
    required: ['id', 'experiment_id', 'asset_version_id', 'revision'],
  },
  add_experiment_entry_request: {
    properties: {
      asset_version_id: { type: 'string', minLength: 1 },
      marketing_vars: { additionalProperties: true, type: 'object' },
      expected_revision: { type: 'integer', minimum: 1 },
    },
    type: 'object',
    required: ['asset_version_id', 'expected_revision'],
  },
  add_product_reference_200: {
    properties: {
      product_id: { type: 'string' },
      entry_id: { type: 'string' },
      version_id: { type: 'string' },
      revision: { type: 'integer' },
      model_variants: { items: { type: 'string' }, type: 'array' },
      has_unpublished_changes: { type: 'boolean', default: false },
      items: {
        items: { $ref: '#/components/schemas/ProductReferenceItem' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'product_id',
      'entry_id',
      'version_id',
      'revision',
      'model_variants',
      'items',
    ],
    description:
      "A projection of the product's immutable Library version/bindings.\n\n``revision`` is the If-Match value for edits. Deleted items remain in\nprior ProductVersions; ``version_id`` can be frozen by later consumers.",
  },
  add_product_reference_request: {
    properties: {
      reference_object_id: {
        anyOf: [
          { type: 'string', maxLength: 64, minLength: 1 },
          { type: 'null' },
        ],
      },
      reference_version_id: {
        anyOf: [
          { type: 'string', maxLength: 64, minLength: 1 },
          { type: 'null' },
        ],
      },
      model_variant: { type: 'string', maxLength: 100, default: '' },
      role: {
        type: 'string',
        enum: ['primary', 'detail', 'logo', 'certification_mark'],
        default: 'detail',
      },
      identity_status: {
        type: 'string',
        enum: ['verified_physical', 'unverified'],
        default: 'unverified',
      },
      note: { type: 'string', maxLength: 1200, default: '' },
    },
    additionalProperties: false,
    type: 'object',
  },
  adopt_localization_isolated_output_200: {
    properties: {
      item_id: { type: 'string' },
      attempt_id: { type: 'string' },
      replayed: { type: 'boolean' },
      asset_version_id: { type: 'string', default: '' },
      publication_id: { type: 'string', default: '' },
      binding_id: { type: 'string', default: '' },
    },
    type: 'object',
    required: ['item_id', 'attempt_id', 'replayed'],
  },
  analyze_experiment_duplicates_200: {
    properties: {
      experiment_id: { type: 'string' },
      analyzed_revision: { type: 'integer' },
      candidate_set_hash: { type: 'string', maxLength: 64, minLength: 64 },
      has_duplicates: { type: 'boolean' },
      duplicate_groups: {
        items: { $ref: '#/components/schemas/DuplicateGroup' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'experiment_id',
      'analyzed_revision',
      'candidate_set_hash',
      'has_duplicates',
    ],
  },
  apply_library_template_201: {
    properties: {
      id: { type: 'string' },
      entry_type: { type: 'string' },
      display_name: { type: 'string' },
      lifecycle: { type: 'string' },
      current_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      draft: {
        anyOf: [
          { $ref: '#/components/schemas/DraftSummary' },
          { type: 'null' },
        ],
      },
      versions: {
        items: { $ref: '#/components/schemas/LibraryVersionSummary' },
        type: 'array',
      },
      revision: { type: 'integer', default: 1 },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['id', 'entry_type', 'display_name', 'lifecycle'],
    description:
      "``GET /library/entries/{id}`` and the create/save/archive/fork/clone result.\n\n``revision`` tracks the entry's open Draft (defaults to 1 once archived\nand the Draft has been dropped) and is the value clients must echo back\nvia ``If-Match`` on the next mutating command.",
  },
  apply_library_template_request: {
    properties: {
      product_entry_id: { type: 'string' },
      display_name: { type: 'string', maxLength: 300, minLength: 1 },
    },
    type: 'object',
    required: ['product_entry_id', 'display_name'],
  },
  approve_localization_copy_200: {
    properties: {
      batch_id: { type: 'string' },
      copy_version_id: { type: 'string' },
      approval_id: { type: 'string' },
    },
    type: 'object',
    required: ['batch_id', 'copy_version_id', 'approval_id'],
  },
  approve_localization_copy_request: {
    properties: { copy_version_id: { type: 'string' } },
    type: 'object',
    required: ['copy_version_id'],
  },
  archive_competitor_200: {
    properties: {
      competitor_id: { type: 'string' },
      product_id: { type: 'string' },
      role: { type: 'string', enum: ['competitor', 'self'] },
      status: { type: 'string', enum: ['active', 'archived'] },
      name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      website_domain: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      input_kind: {
        type: 'string',
        enum: ['name', 'facebook_page', 'ad_library'],
      },
      input_page_alias: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confirmation_status: {
        type: 'string',
        enum: ['confirmed', 'unconfirmed'],
      },
      confirmation: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorConfirmation' },
          { type: 'null' },
        ],
      },
      observed_page_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      last_scanned_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      latest_resolution: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorResolutionSummary' },
          { type: 'null' },
        ],
      },
      can_resolve: { type: 'boolean', default: false },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      archived_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      archived_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'competitor_id',
      'product_id',
      'role',
      'status',
      'input_kind',
      'confirmation_status',
    ],
    description:
      "A competitor of a product (or ``role=self``, the product's own page).\n``input_kind`` is how it was added; ``can_resolve`` says whether a\nname search is possible (a name, or the alias of a pasted page link).\n\nTwo read-only projections from the product's scans, neither a call nor\na change to the confirmation: ``observed_page_name`` is the advertiser\nname of the confirmed page's newest observation (a link confirms a page\nwithout its name; null until a scan sees the page), and\n``last_scanned_at`` is when the newest finished scan (``completed``,\n``partial``, ``failed`` or ``uncertain``) that included this competitor\nfinished (null when none has).",
  },
  archive_library_entry_200: {
    properties: {
      id: { type: 'string' },
      entry_type: { type: 'string' },
      display_name: { type: 'string' },
      lifecycle: { type: 'string' },
      current_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      draft: {
        anyOf: [
          { $ref: '#/components/schemas/DraftSummary' },
          { type: 'null' },
        ],
      },
      versions: {
        items: { $ref: '#/components/schemas/LibraryVersionSummary' },
        type: 'array',
      },
      revision: { type: 'integer', default: 1 },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['id', 'entry_type', 'display_name', 'lifecycle'],
    description:
      "``GET /library/entries/{id}`` and the create/save/archive/fork/clone result.\n\n``revision`` tracks the entry's open Draft (defaults to 1 once archived\nand the Draft has been dropped) and is the value clients must echo back\nvia ``If-Match`` on the next mutating command.",
  },
  archive_library_entry_request: {
    properties: {
      impact_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      impact_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
  },
  authorize_localization_isolated_qc_200: {
    properties: {
      item_id: { type: 'string' },
      attempt_id: { type: 'string' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean' },
      authorized_steps: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
    required: ['item_id', 'attempt_id', 'replayed'],
  },
  bind_credential_200: {},
  bind_credential_request: {
    properties: { provider: { type: 'string' } },
    additionalProperties: false,
    type: 'object',
    required: ['provider'],
  },
  bind_experiment_candidate_200: {
    properties: {
      candidate_id: { type: 'string' },
      ad_account_id: { type: 'string' },
      ad_id: { type: 'string' },
    },
    type: 'object',
    required: ['candidate_id', 'ad_account_id', 'ad_id'],
  },
  bind_experiment_candidate_request: {
    properties: {
      ad_account_id: { type: 'string', minLength: 1 },
      ad_id: { type: 'string', minLength: 1 },
      source: { type: 'string', default: 'manual_read_only' },
      expected_revision: { type: 'integer', minimum: 1 },
    },
    type: 'object',
    required: ['ad_account_id', 'ad_id', 'expected_revision'],
  },
  bind_library_data_source_credential_201: {
    properties: {
      id: { type: 'string' },
      data_source_entry_id: { type: 'string' },
      credential_version_id: { type: 'string' },
      revision: { type: 'integer' },
      state: { type: 'string' },
    },
    type: 'object',
    required: [
      'id',
      'data_source_entry_id',
      'credential_version_id',
      'revision',
      'state',
    ],
    description:
      'Shape of ``datasources.bind_credential`` / ``get_active_binding``.',
  },
  bind_library_data_source_credential_request: {
    properties: { credential_version_id: { type: 'string' } },
    type: 'object',
    required: ['credential_version_id'],
  },
  bind_library_reference_201: {
    properties: {
      id: { type: 'string' },
      entry_id: { type: 'string' },
      revision: { type: 'integer' },
      state: { type: 'string' },
      content_schema_version: { type: 'integer', default: 1 },
      base_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      origin_command: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      content: { additionalProperties: true, type: 'object' },
      validation: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['id', 'entry_id', 'revision', 'state'],
    description:
      'Shape of ``service._draft_dict`` -- also the ``bind`` command result.',
  },
  bind_library_reference_request: {
    properties: {
      reference_version_id: { type: 'string' },
      purpose: { type: 'string', maxLength: 100, minLength: 1 },
      viewpoint: { type: 'string', maxLength: 100, minLength: 1 },
      priority: { type: 'integer', minimum: 0, default: 0 },
      note: { type: 'string', default: '' },
      conditions: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['reference_version_id', 'purpose', 'viewpoint'],
  },
  bind_marketing_delivery_201: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  bind_marketing_delivery_request: {
    properties: {
      entry_id: { type: 'string', maxLength: 64, minLength: 1 },
      ad_account_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^(act_)?[0-9]+$',
      },
      ad_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^[0-9]+$',
      },
      conditions_match: { type: 'boolean' },
      deviations: {
        items: { type: 'string', maxLength: 2000, minLength: 1 },
        type: 'array',
        maxItems: 20,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['entry_id', 'ad_account_id', 'ad_id', 'conditions_match'],
  },
  cancel_generation_run_200: {
    properties: {
      operation_id: { type: 'string' },
      run_id: { type: 'string' },
    },
    type: 'object',
    required: ['operation_id', 'run_id'],
    description:
      'Result of a Run mutation command (cancel/retry/resume/reconcile).',
  },
  cancel_localization_batch_200: {
    properties: {
      batch_id: { type: 'string' },
      cancelled_item_ids: { items: { type: 'string' }, type: 'array' },
      already_terminal: { type: 'boolean' },
    },
    type: 'object',
    required: ['batch_id', 'already_terminal'],
  },
  check_library_data_source_connection_202: {
    properties: {
      operation_id: { type: 'string' },
      state: { type: 'string', default: 'queued' },
      command_type: { type: 'string' },
    },
    type: 'object',
    required: ['operation_id', 'command_type'],
    description:
      '202 Accepted envelope for async domain commands (connection-checks, manual-fact-imports, reference-gate).',
  },
  check_library_data_source_connection_request: {
    properties: {
      execution_mode: {
        anyOf: [{ type: 'string', maxLength: 40 }, { type: 'null' }],
        description:
          "Ignored. A data source is checked in its published version's execution mode.",
        deprecated: true,
      },
    },
    type: 'object',
  },
  clone_library_product_201: {
    properties: {
      id: { type: 'string' },
      entry_type: { type: 'string' },
      display_name: { type: 'string' },
      lifecycle: { type: 'string' },
      current_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      draft: {
        anyOf: [
          { $ref: '#/components/schemas/DraftSummary' },
          { type: 'null' },
        ],
      },
      versions: {
        items: { $ref: '#/components/schemas/LibraryVersionSummary' },
        type: 'array',
      },
      revision: { type: 'integer', default: 1 },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['id', 'entry_type', 'display_name', 'lifecycle'],
    description:
      "``GET /library/entries/{id}`` and the create/save/archive/fork/clone result.\n\n``revision`` tracks the entry's open Draft (defaults to 1 once archived\nand the Draft has been dropped) and is the value clients must echo back\nvia ``If-Match`` on the next mutating command.",
  },
  clone_library_product_request: {
    properties: {
      display_name: { type: 'string', maxLength: 300, minLength: 1 },
      include_optional: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
    required: ['display_name'],
  },
  close_marketing_round_200: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  close_marketing_round_request: {
    properties: {
      reason: {
        type: 'string',
        enum: [
          'completed',
          'asset_only',
          'no_qualified',
          'insufficient_evidence',
          'user_stopped',
        ],
      },
      note: { type: 'string', maxLength: 2000, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['reason', 'note'],
  },
  close_scale_enrollment_200: {
    properties: {
      id: { type: 'string' },
      meta_mode: { type: 'string' },
      meta_connection_id: { type: 'string' },
      scenario_scope_key: { type: 'string' },
      ad_account_id: { type: 'string' },
      winner_decision_id: { type: 'string' },
      winner_confirmation_id: { type: 'string' },
      asset_version_id: { type: 'string' },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      market_code: { type: 'string' },
      landing_page_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      market_asset_binding_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      campaign_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ad_set_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ad_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      remote_binding_key: { type: 'string' },
      lifecycle: { type: 'string' },
      transition_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      current_episode_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      creator_id: { type: 'string' },
      original_initiator_id: { type: 'string' },
      revision: { type: 'integer' },
      evaluation_cancellation_generation: { type: 'integer' },
      created_at: { type: 'string', format: 'date-time' },
      updated_at: { type: 'string', format: 'date-time' },
      closed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibilityModel' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'meta_mode',
      'meta_connection_id',
      'scenario_scope_key',
      'ad_account_id',
      'winner_decision_id',
      'winner_confirmation_id',
      'asset_version_id',
      'product_version_id',
      'offer_version_id',
      'market_code',
      'landing_page_version_id',
      'market_asset_binding_id',
      'campaign_remote_id',
      'ad_set_remote_id',
      'ad_remote_id',
      'remote_binding_key',
      'lifecycle',
      'transition_reason',
      'current_episode_id',
      'creator_id',
      'original_initiator_id',
      'revision',
      'evaluation_cancellation_generation',
      'created_at',
      'updated_at',
      'closed_at',
      'allowed_actions',
    ],
  },
  close_scale_enrollment_request: {
    properties: { reason: { type: 'string', default: 'user_requested' } },
    additionalProperties: false,
    type: 'object',
  },
  commit_results_import_200: {
    properties: {
      import_id: { type: 'string' },
      product_id: { type: 'string' },
      source: { type: 'string' },
      status: { type: 'string', enum: ['previewed', 'committed'] },
      file_name: { type: 'string', default: '' },
      currency: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      summary: { $ref: '#/components/schemas/ResultsImportSummary' },
      rows: {
        items: { $ref: '#/components/schemas/ResultsImportRow' },
        type: 'array',
      },
      rows_truncated: { type: 'boolean', default: false },
      snapshot_ids: { items: { type: 'string' }, type: 'array' },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      committed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      duplicate: { type: 'boolean', default: false },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['import_id', 'product_id', 'source', 'status', 'summary'],
    description:
      'A previewed (``previewed``) or committed (``committed``) import.\n``rows`` lists every rejected row and the first accepted ones\n(``rows_truncated``); ``duplicate`` is true when the same rows were\nalready imported for this product (the existing import is returned).',
  },
  confirm_competitor_200: {
    properties: {
      competitor_id: { type: 'string' },
      product_id: { type: 'string' },
      role: { type: 'string', enum: ['competitor', 'self'] },
      status: { type: 'string', enum: ['active', 'archived'] },
      name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      website_domain: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      input_kind: {
        type: 'string',
        enum: ['name', 'facebook_page', 'ad_library'],
      },
      input_page_alias: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confirmation_status: {
        type: 'string',
        enum: ['confirmed', 'unconfirmed'],
      },
      confirmation: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorConfirmation' },
          { type: 'null' },
        ],
      },
      observed_page_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      last_scanned_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      latest_resolution: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorResolutionSummary' },
          { type: 'null' },
        ],
      },
      can_resolve: { type: 'boolean', default: false },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      archived_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      archived_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'competitor_id',
      'product_id',
      'role',
      'status',
      'input_kind',
      'confirmation_status',
    ],
    description:
      "A competitor of a product (or ``role=self``, the product's own page).\n``input_kind`` is how it was added; ``can_resolve`` says whether a\nname search is possible (a name, or the alias of a pasted page link).\n\nTwo read-only projections from the product's scans, neither a call nor\na change to the confirmation: ``observed_page_name`` is the advertiser\nname of the confirmed page's newest observation (a link confirms a page\nwithout its name; null until a scan sees the page), and\n``last_scanned_at`` is when the newest finished scan (``completed``,\n``partial``, ``failed`` or ``uncertain``) that included this competitor\nfinished (null when none has).",
  },
  confirm_competitor_request: {
    properties: {
      resolve_job_id: {
        anyOf: [
          { type: 'string', maxLength: 64, minLength: 1 },
          { type: 'null' },
        ],
      },
      page_id: {
        anyOf: [
          { type: 'string', maxLength: 32, minLength: 1 },
          { type: 'null' },
        ],
      },
      page_url: {
        anyOf: [
          { type: 'string', maxLength: 2048, minLength: 1 },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    description:
      'Either a candidate of a reusable resolve (``resolve_job_id`` +\n``page_id``) or a page link carrying a page id (``page_url``: a\n``profile.php?id=`` link or an Ad Library ``view_all_page_id`` link).\nConfirming the page that is already confirmed from the same source\nreturns that revision instead of appending another.',
  },
  confirm_competitor_ad_tags_200: {
    properties: {
      assessment_id: { type: 'string' },
      ad_id: { type: 'string' },
      scan_id: { type: 'string' },
      kind: { type: 'string', const: 'tags' },
      revision: { type: 'integer' },
      source: { type: 'string', enum: ['ai', 'rule', 'user'] },
      ai_suggestion: { type: 'boolean' },
      factors: {
        items: { $ref: '#/components/schemas/CompetitorAdTagFactor' },
        type: 'array',
      },
      summary: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      language: {
        anyOf: [{ type: 'string', enum: ['en', 'zh-CN'] }, { type: 'null' }],
      },
      vocabulary_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      images: {
        anyOf: [
          {
            items: { $ref: '#/components/schemas/CompetitorAdTagImage' },
            type: 'array',
          },
          { type: 'null' },
        ],
      },
      previous: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdTagPrevious' },
          { type: 'null' },
        ],
      },
      evidence: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdAssessmentEvidence' },
          { type: 'null' },
        ],
      },
      attempt_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'assessment_id',
      'ad_id',
      'scan_id',
      'kind',
      'revision',
      'source',
      'ai_suggestion',
    ],
    description:
      "One append-only tags revision: the AI's suggestion (``ai_suggestion``;\nper factor a value, confidence and reason in the scan's language, a\none-sentence ``summary`` and the card images it saw) or a member's\nconfirmation (``created_by``, ``previous``).",
  },
  confirm_competitor_ad_tags_request: {
    properties: {
      tags: {
        items: { $ref: '#/components/schemas/CompetitorTagInput' },
        type: 'array',
        maxItems: 24,
      },
    },
    additionalProperties: false,
    type: 'object',
    description:
      "A member's tags for one observation, against the scan's frozen\nvocabulary: one value per factor (``value_id`` null or ``none`` = none of\nthese); factors left out are \"none\". Confirming the AI's values as they\nare is a revision too.",
  },
  confirm_experiment_proposal_200: {
    properties: {
      confirmation_id: { type: 'string' },
      decisions: {
        items: { $ref: '#/components/schemas/ExperimentDecision' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['confirmation_id'],
  },
  confirm_experiment_proposal_request: {
    properties: { expected_proposal_sequence: { type: 'integer', minimum: 1 } },
    type: 'object',
    required: ['expected_proposal_sequence'],
  },
  confirm_external_creative_200: {
    properties: {
      external_creative_id: { type: 'string' },
      product_id: { type: 'string' },
      status: { type: 'string' },
      current_step: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      execution_mode: { type: 'string' },
      filename: { type: 'string', default: '' },
      mime_type: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      byte_size: { type: 'integer', default: 0 },
      width: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      height: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      aspect_ratio: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      rights: { $ref: '#/components/schemas/RightsView' },
      asset_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      asset_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      asset_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      thumbnail: {
        anyOf: [
          { $ref: '#/components/schemas/ThumbnailLink' },
          { type: 'null' },
        ],
      },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/CreativeFailure' },
          { type: 'null' },
        ],
      },
      vocabulary: { $ref: '#/components/schemas/Vocabulary' },
      suggestion: {
        anyOf: [
          { $ref: '#/components/schemas/TagSuggestion' },
          { type: 'null' },
        ],
      },
      confirmation: {
        anyOf: [
          { $ref: '#/components/schemas/TagConfirmation' },
          { type: 'null' },
        ],
      },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'external_creative_id',
      'product_id',
      'status',
      'execution_mode',
      'rights',
      'vocabulary',
    ],
    description:
      '``status``: ``queued`` -> ``gating`` -> ``preparing_asset`` ->\n``tagging`` -> ``ready`` (suggestion available) -> ``confirmed``; or\n``rejected`` (upload gate), ``failed``, ``tagging_failed`` (the asset\nexists; tag it by hand). ``asset_code`` (``MK-XXXXXX``) is what a\nresults sheet matches.',
  },
  confirm_external_creative_request: {
    properties: {
      tags: {
        items: { $ref: '#/components/schemas/TagChoice' },
        type: 'array',
        maxItems: 24,
      },
      direction: {
        anyOf: [
          { $ref: '#/components/schemas/DirectionChoice' },
          { type: 'null' },
        ],
      },
      approve: { type: 'boolean', default: true },
    },
    additionalProperties: false,
    type: 'object',
    description:
      'The reviewed tags (one per factor; ``value_id`` null or omitted factor\n= "none of these"), an optional direction, and whether confirming also\napproves the image for tests (default true).',
  },
  confirm_localization_batch_200: {
    properties: {
      batch_id: { type: 'string' },
      item_ids: { items: { type: 'string' }, type: 'array' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean' },
    },
    type: 'object',
    required: ['batch_id', 'replayed'],
  },
  confirm_localization_batch_request: {
    properties: {
      edit_provider: { type: 'string' },
      edit_model: { type: 'string' },
      edit_adapter_version: { type: 'string' },
      external_qc_steps: { items: { type: 'string' }, type: 'array' },
      max_attempts: { type: 'integer', maximum: 3, minimum: 1, default: 3 },
    },
    type: 'object',
    required: ['edit_provider', 'edit_model', 'edit_adapter_version'],
  },
  confirm_marketing_ai_qc_200: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  confirm_marketing_ai_qc_request: {
    properties: {
      ai_qc_ids: {
        items: { type: 'string' },
        type: 'array',
        maxItems: 60,
        minItems: 1,
      },
      resolved_checks: {
        additionalProperties: { type: 'string', const: 'pass' },
        type: 'object',
        maxProperties: 6,
      },
      note: { type: 'string', maxLength: 2000, default: '' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['ai_qc_ids'],
  },
  confirm_marketing_metrics_200: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  confirm_marketing_metrics_request: {
    properties: { batch_id: { type: 'string', maxLength: 64, minLength: 1 } },
    additionalProperties: false,
    type: 'object',
    required: ['batch_id'],
  },
  confirm_meta_launch_preview_200: {
    properties: {
      id: { type: 'string' },
      workspace_id: { type: 'string' },
      meta_mode: { type: 'string' },
      meta_connection_id: { type: 'string' },
      meta_binding_id: { type: 'string' },
      experiment_id: { type: 'string' },
      experiment_version_id: { type: 'string' },
      candidate_count: { type: 'integer' },
      campaign_config: { additionalProperties: true, type: 'object' },
      daily_budget_minor: { type: 'integer' },
      total_daily_cost_minor: { type: 'integer' },
      currency: { type: 'string' },
      preview_hash: { type: 'string' },
      state: { type: 'string' },
      confirmed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      expires_at: { type: 'string', format: 'date-time' },
      created_at: { type: 'string', format: 'date-time' },
      candidates: {
        items: { $ref: '#/components/schemas/LaunchCandidateResponse' },
        type: 'array',
      },
      confirmation_token: { type: 'string' },
      confirmation_expires_at: { type: 'string', format: 'date-time' },
      confirmation_context_hash: { type: 'string' },
    },
    type: 'object',
    required: [
      'id',
      'workspace_id',
      'meta_mode',
      'meta_connection_id',
      'meta_binding_id',
      'experiment_id',
      'experiment_version_id',
      'candidate_count',
      'daily_budget_minor',
      'total_daily_cost_minor',
      'currency',
      'preview_hash',
      'state',
      'expires_at',
      'created_at',
      'confirmation_token',
      'confirmation_expires_at',
      'confirmation_context_hash',
    ],
  },
  confirm_meta_launch_preview_request: {
    properties: {
      meta_mode: { type: 'string' },
      preview_hash: { type: 'string', maxLength: 64, minLength: 64 },
    },
    type: 'object',
    required: ['meta_mode', 'preview_hash'],
  },
  confirm_product_intake_200: {
    properties: {
      intake_id: { type: 'string' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: { type: 'string' },
      current_step: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      execution_mode: { type: 'string' },
      input: { $ref: '#/components/schemas/IntakeInputSummary' },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/IntakeFailure' },
          { type: 'null' },
        ],
      },
      evidence: { additionalProperties: true, type: 'object' },
      card: {
        anyOf: [
          { $ref: '#/components/schemas/ProductCardDraft' },
          { type: 'null' },
        ],
      },
      confirmation: {
        anyOf: [
          { $ref: '#/components/schemas/ConfirmedProduct' },
          { type: 'null' },
        ],
      },
      product_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['intake_id', 'status', 'execution_mode', 'input'],
    description:
      '``status``: ``queued`` -> ``fetching`` -> ``extracting`` ->\n``preparing_images`` -> ``ready`` (card available) -> ``confirmed``;\nor ``fetch_failed`` / ``extraction_failed`` / ``failed`` with\n``failure``. ``evidence`` holds per-step outcome, duration and digests.',
  },
  confirm_product_intake_request: {
    properties: {
      card: {
        anyOf: [
          { $ref: '#/components/schemas/ProductCardEdits' },
          { type: 'null' },
        ],
      },
      selected_variant: {
        anyOf: [{ type: 'string', maxLength: 100 }, { type: 'null' }],
      },
      image_ids: {
        anyOf: [
          { items: { type: 'string' }, type: 'array', maxItems: 8 },
          { type: 'null' },
        ],
      },
      entry_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
      acknowledged_warnings: {
        items: { type: 'string' },
        type: 'array',
        maxItems: 20,
      },
    },
    additionalProperties: false,
    type: 'object',
    description:
      "Confirm (and publish) the card. ``selected_variant`` is required when\nthe draft has more than one ``model_variants`` entry; only that variant\nenters the version. ``image_ids`` defaults to the chosen variant's\n``default_image_ids`` (``card.default_image_ids`` without variants);\nan omitted ``card.must_match`` defaults to the shared items plus the\nchosen variant's ``appearance``, and an omitted ``card.specs`` to the\nshared specs with the variant's laid over them. ``entry_id`` updates an\nexisting product instead of creating one.",
  },
  confirm_spend_guardrail_200: {},
  confirm_spend_guardrail_request: {
    properties: {
      preview_token: { type: 'string' },
      expected_revision: { type: 'integer' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['preview_token', 'expected_revision'],
  },
  convert_scale_advice_occurrence_201: {
    properties: {
      id: { type: 'string' },
      thread_id: { type: 'string' },
      occurrence_id: { type: 'string' },
      generation_draft_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      scale_test_plan_draft_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      input_lineage_hash: { type: 'string' },
      initiator_id: { type: 'string' },
      created_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'thread_id',
      'occurrence_id',
      'generation_draft_id',
      'scale_test_plan_draft_id',
      'input_lineage_hash',
      'initiator_id',
      'created_at',
    ],
  },
  convert_scale_advice_occurrence_request: {
    properties: {
      provider: { type: 'string', default: 'scale_advice_default' },
      model: { type: 'string', default: 'scale_advice_default' },
      refresh_axis: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
  },
  create_competitor_201: {
    properties: {
      competitor_id: { type: 'string' },
      product_id: { type: 'string' },
      role: { type: 'string', enum: ['competitor', 'self'] },
      status: { type: 'string', enum: ['active', 'archived'] },
      name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      website_domain: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      input_kind: {
        type: 'string',
        enum: ['name', 'facebook_page', 'ad_library'],
      },
      input_page_alias: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confirmation_status: {
        type: 'string',
        enum: ['confirmed', 'unconfirmed'],
      },
      confirmation: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorConfirmation' },
          { type: 'null' },
        ],
      },
      observed_page_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      last_scanned_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      latest_resolution: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorResolutionSummary' },
          { type: 'null' },
        ],
      },
      can_resolve: { type: 'boolean', default: false },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      archived_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      archived_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'competitor_id',
      'product_id',
      'role',
      'status',
      'input_kind',
      'confirmation_status',
    ],
    description:
      "A competitor of a product (or ``role=self``, the product's own page).\n``input_kind`` is how it was added; ``can_resolve`` says whether a\nname search is possible (a name, or the alias of a pasted page link).\n\nTwo read-only projections from the product's scans, neither a call nor\na change to the confirmation: ``observed_page_name`` is the advertiser\nname of the confirmed page's newest observation (a link confirms a page\nwithout its name; null until a scan sees the page), and\n``last_scanned_at`` is when the newest finished scan (``completed``,\n``partial``, ``failed`` or ``uncertain``) that included this competitor\nfinished (null when none has).",
  },
  create_competitor_request: {
    properties: {
      product_id: { type: 'string', maxLength: 64, minLength: 1 },
      input: { type: 'string', maxLength: 2048, minLength: 1 },
      website: {
        anyOf: [{ type: 'string', maxLength: 2048 }, { type: 'null' }],
      },
      role: {
        type: 'string',
        enum: ['competitor', 'self'],
        default: 'competitor',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id', 'input'],
    description:
      "One input of spec §4.1 in ``input``: a Facebook page link\n(``facebook.com/<alias>``, ``/profile.php?id=<id>``), an Ad Library link\nwith ``view_all_page_id``, or a name. ``website`` (a URL or a bare\ndomain) is the official site a resolve compares landing pages with.\n``role=self`` adds the product's own page as a baseline.",
  },
  create_competitor_scan_202: {
    properties: {
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      operation_id: { type: 'string' },
      job_id: { type: 'string' },
      status: {
        type: 'string',
        enum: [
          'queued',
          'running',
          'completed',
          'partial',
          'failed',
          'uncertain',
        ],
      },
      stage: {
        anyOf: [
          {
            type: 'string',
            enum: ['fetch', 'media', 'filter', 'tag', 'cluster', 'freeze'],
          },
          { type: 'null' },
        ],
      },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      provider: { type: 'string' },
      platform: { type: 'string', const: 'meta', default: 'meta' },
      country: { type: 'string' },
      params: { $ref: '#/components/schemas/CompetitorScanParams' },
      targets: {
        items: { $ref: '#/components/schemas/CompetitorScanTarget' },
        type: 'array',
      },
      steps: {
        items: { $ref: '#/components/schemas/CompetitorScanStepState' },
        type: 'array',
      },
      quote: { $ref: '#/components/schemas/CompetitorScanQuote' },
      cost: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorScanCost' },
          { type: 'null' },
        ],
      },
      initial_credential_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      resumes: {
        items: { $ref: '#/components/schemas/CompetitorScanResume' },
        type: 'array',
      },
      reconciliation: {
        items: { $ref: '#/components/schemas/CompetitorScanReconciliation' },
        type: 'array',
      },
      media: { $ref: '#/components/schemas/CompetitorScanMedia' },
      observation_count: { type: 'integer', default: 0 },
      waits: { type: 'integer', default: 0 },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorScanFailure' },
          { type: 'null' },
        ],
      },
      can_resume: { type: 'boolean', default: false },
      reclusters: {
        items: { $ref: '#/components/schemas/CompetitorScanRecluster' },
        type: 'array',
      },
      snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      started_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'scan_id',
      'product_id',
      'operation_id',
      'job_id',
      'status',
      'execution_mode',
      'provider',
      'country',
      'params',
      'quote',
    ],
    description:
      "A scan: ``queued`` -> ``running`` -> ``completed`` | ``partial`` (some\npages succeeded, some failed or ran out of credits) | ``failed`` (no page\nsucceeded) | ``uncertain`` (a page's outcome is unknown and not yet\nreconciled). ``stage`` is the last completed step. ``can_resume`` when\npages are left, a page awaits reconciliation, or a judgment step did not\nfinish (a ``completed`` scan whose AI key was refused mid-way, say).\n``waits`` counts how often it queued behind another market-data job on\nthe same key (spec §5.1). ``snapshot_id`` is the snapshot of the\nrevisions in effect as of the last freeze.",
  },
  create_competitor_scan_request: {
    properties: {
      product_id: { type: 'string', maxLength: 64, minLength: 1 },
      competitor_ids: {
        items: { type: 'string', maxLength: 64, minLength: 1 },
        type: 'array',
        maxItems: 20,
        minItems: 1,
      },
      keywords: {
        items: { type: 'string', maxLength: 100, minLength: 1 },
        type: 'array',
        maxItems: 5,
      },
      country: { type: 'string', maxLength: 2, minLength: 2, default: 'US' },
      max_pages_per_competitor: {
        type: 'integer',
        maximum: 5,
        minimum: 1,
        default: 3,
      },
      keyword_pages: { type: 'integer', maximum: 5, minimum: 1, default: 2 },
      max_media_refreshes: {
        type: 'integer',
        maximum: 50,
        minimum: 0,
        default: 10,
      },
      max_tagged_ads: {
        type: 'integer',
        maximum: 200,
        minimum: 0,
        default: 60,
      },
      max_discover_advertisers: {
        type: 'integer',
        maximum: 100,
        minimum: 0,
        default: 30,
      },
      execution_mode: {
        anyOf: [{ type: 'string', enum: ['byok', 'mock'] }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id', 'competitor_ids'],
    description:
      "What to scan (spec §5.1). Every competitor must belong to the product,\nbe active and have a confirmed page; ``role=self`` competitors are\nscanned as the baseline. ``keywords`` feed competitor discovery (spec\n§8), ``keyword_pages`` pages each. ``max_media_refreshes`` is the quota\nof paid ``ad_refresh`` calls for media links that expired.\n``max_tagged_ads`` and ``max_discover_advertisers`` cap the LLM steps.\n``execution_mode`` defaults to the quote's default (``byok`` when both\nkeys are ready, else ``mock``). The AI output language is the\ninitiator's interface language, frozen into ``params.language``.",
  },
  create_credential_200: {},
  create_credential_request: {
    properties: {
      credential_key: { type: 'string' },
      provider: { type: 'string' },
      plaintext_b64: { type: 'string' },
      display_label: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['credential_key', 'provider', 'plaintext_b64'],
  },
  create_download_intent_201: {
    properties: {
      intent_id: { type: 'string' },
      expires_at: { type: 'string', format: 'date-time' },
      download_grant: { $ref: '#/components/schemas/DownloadGrantResponse' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['intent_id', 'expires_at', 'download_grant'],
    description:
      'See the note on `UploadIntentResponse` -- the handler returns a\n`JSONResponse` directly.',
  },
  create_download_intent_request: {
    properties: {
      owner_kind: { type: 'string' },
      owner_id: { type: 'string' },
      committed_blob_ref_key: { type: 'string' },
      committed_blob_ref_version: { type: 'string' },
      filename: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'owner_kind',
      'owner_id',
      'committed_blob_ref_key',
      'committed_blob_ref_version',
      'filename',
    ],
  },
  create_experiment_201: {
    properties: {
      id: { type: 'string' },
      product_id: { type: 'string' },
      name: { type: 'string' },
      state: { type: 'string' },
      revision: { type: 'integer' },
      record_schema_version: { type: 'integer' },
      draft_context: { additionalProperties: true, type: 'object' },
      frozen_snapshot_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { type: 'string', format: 'date-time' },
      frozen_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      started_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      closed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      frozen_context: {
        anyOf: [
          { $ref: '#/components/schemas/ExperimentFrozenContext' },
          { type: 'null' },
        ],
      },
      entries: {
        items: { $ref: '#/components/schemas/ExperimentEntry' },
        type: 'array',
      },
      candidates: {
        items: { $ref: '#/components/schemas/ExperimentCandidate' },
        type: 'array',
      },
      judgements: {
        items: { $ref: '#/components/schemas/ExperimentJudgement' },
        type: 'array',
      },
      proposals: {
        items: { $ref: '#/components/schemas/ExperimentProposal' },
        type: 'array',
      },
      decisions: {
        items: { $ref: '#/components/schemas/ExperimentDecision' },
        type: 'array',
      },
      concept_resolution_evidence: {
        anyOf: [
          { $ref: '#/components/schemas/ConceptResolutionEvidence' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'id',
      'product_id',
      'name',
      'state',
      'revision',
      'record_schema_version',
      'created_at',
    ],
  },
  create_experiment_request: {
    properties: {
      product_id: { type: 'string', minLength: 1 },
      name: { type: 'string', maxLength: 300, minLength: 1 },
      draft_context: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['product_id', 'name', 'draft_context'],
  },
  create_external_creatives_202: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/ExternalCreativeDetail' },
        type: 'array',
      },
    },
    type: 'object',
  },
  create_external_creatives_request: {
    properties: {
      product_id: { type: 'string', maxLength: 64, minLength: 1 },
      upload_intent_ids: {
        items: { type: 'string' },
        type: 'array',
        maxItems: 10,
        minItems: 1,
      },
      rights: { $ref: '#/components/schemas/RightsDeclaration' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id', 'upload_intent_ids', 'rights'],
    description:
      'Finalized uploads of purpose ``external_creative`` (PNG/JPEG/WebP,\n<= 20 MB each, at most 10) for one product.',
  },
  create_generation_asset_review_201: {
    properties: {
      id: { type: 'string' },
      asset_version_id: { type: 'string' },
      review_value: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reason: { type: 'string', default: '' },
      supersedes_review_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      actor_id: { type: 'string' },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source: { type: 'string', default: 'human' },
    },
    type: 'object',
    required: ['id', 'asset_version_id', 'actor_id'],
    description:
      'Row of ``AssetVersionDetail.reviews`` and the create-review result.',
  },
  create_generation_asset_review_request: {
    properties: {
      review_value: { type: 'string', minLength: 1 },
      reason: { type: 'string', default: '' },
      supersedes_review_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['review_value'],
  },
  create_generation_asset_version_download_intent_201: {
    properties: {
      intent_id: { type: 'string' },
      expires_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      download_grant: {
        anyOf: [
          { $ref: '#/components/schemas/DownloadGrant' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['intent_id'],
    description:
      'Shared shape for asset-version/export download-intent results.\n\n``download_grant`` is optional because the export-package route\n(``create_generation_export_download_intent``) does not yet issue one\n(Issue #49 Task 9 wires that); the asset-version route always populates\nit.',
  },
  create_generation_draft_201: {
    properties: {
      id: { type: 'string' },
      product_id: { type: 'string' },
      provider: { type: 'string' },
      model: { type: 'string' },
      execution_mode: { type: 'string' },
      state: { type: 'string' },
      revision: { type: 'integer' },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      selectors: { additionalProperties: true, type: 'object' },
      combination_strategy: { additionalProperties: true, type: 'object' },
      format_config: { additionalProperties: true, type: 'object' },
      exclusions: { additionalProperties: true, type: 'object' },
      regeneration_intent: { type: 'string', default: 'new_generation' },
      source_asset_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      source_recipe_identity: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'product_id',
      'provider',
      'model',
      'execution_mode',
      'state',
      'revision',
    ],
    description:
      '``GET /generation/drafts/{id}`` and the create/update/regenerate result.',
  },
  create_generation_draft_request: {
    properties: {
      product_id: { type: 'string', minLength: 1 },
      provider: { type: 'string', minLength: 1 },
      model: { type: 'string', minLength: 1 },
      execution_mode: { type: 'string', minLength: 1 },
      combination_strategy: { additionalProperties: true, type: 'object' },
      format_config: { additionalProperties: true, type: 'object' },
      exclusions: { additionalProperties: true, type: 'object' },
      selectors: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['product_id', 'provider', 'model', 'execution_mode'],
  },
  create_generation_export_download_intent_201: {
    properties: {
      intent_id: { type: 'string' },
      expires_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      download_grant: {
        anyOf: [
          { $ref: '#/components/schemas/DownloadGrant' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['intent_id'],
    description:
      'Shared shape for asset-version/export download-intent results.\n\n``download_grant`` is optional because the export-package route\n(``create_generation_export_download_intent``) does not yet issue one\n(Issue #49 Task 9 wires that); the asset-version route always populates\nit.',
  },
  create_generation_export_package_202: {
    properties: {
      package_id: { type: 'string' },
      operation_id: { type: 'string' },
      state: { type: 'string' },
    },
    type: 'object',
    required: ['package_id', 'operation_id', 'state'],
    description:
      '``POST /generation/exports`` 202 result (Issue #49 Task 9).\n\n``state`` is the authorizing Operation\'s state (``"queued"`` for a\nfresh call, or whatever state a replayed Operation has already reached)\n-- not the ExportPackage\'s own ``pending``/``building``/``ready``/\n``failed`` projection, which ``GET /generation/exports/{id}``\n(``ExportPackageDetail``) surfaces instead.',
  },
  create_generation_export_package_request: {
    properties: {
      selector: { $ref: '#/components/schemas/ExportSelectorInput' },
    },
    type: 'object',
    required: ['selector'],
  },
  create_generation_preview_201: {
    properties: {
      purpose: {
        type: 'string',
        const: 'initial_generation',
        default: 'initial_generation',
      },
      id: { type: 'string' },
      preview_hash: { type: 'string' },
      recipe_input_hash: { type: 'string' },
      execution_mode: { type: 'string' },
      valid_until: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      draft_id: { type: 'string' },
      draft_revision: { type: 'integer' },
      invalidation_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      combination_count: { type: 'integer', default: 0 },
      call_count: { type: 'integer', default: 0 },
      maximum_cost: {
        anyOf: [{ $ref: '#/components/schemas/MaximumCost' }, { type: 'null' }],
      },
      final_prompt: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      items: {
        items: { $ref: '#/components/schemas/PreviewItemSummary' },
        type: 'array',
      },
      capability_decisions: {
        items: { $ref: '#/components/schemas/CapabilityDecision' },
        type: 'array',
      },
      exclusion_summary: {
        anyOf: [
          { $ref: '#/components/schemas/ExclusionSummary' },
          { type: 'null' },
        ],
      },
      skipped_signature_count: { type: 'integer', default: 0 },
      skipped_call_count: { type: 'integer', default: 0 },
      estimated_cost_saving: {
        anyOf: [{ $ref: '#/components/schemas/MaximumCost' }, { type: 'null' }],
      },
      strategy_applied: {
        anyOf: [
          { $ref: '#/components/schemas/StrategyApplied' },
          { type: 'null' },
        ],
      },
      combinations_dropped_by_constraints: { type: 'integer', default: 0 },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
      prompt: { type: 'string', default: '' },
      quantity: { type: 'integer', default: 1 },
      selection_maximum_cost: { type: 'string', default: '0' },
      currency: { type: 'string', default: 'USD' },
      pricing_version: { type: 'string', default: '' },
      provider: { type: 'string', default: '' },
      model: { type: 'string', default: '' },
      image_tool: {
        anyOf: [
          { $ref: '#/components/schemas/ImageToolSpec' },
          { type: 'null' },
        ],
      },
      format_config: { additionalProperties: true, type: 'object' },
      submitted_run_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'id',
      'preview_hash',
      'recipe_input_hash',
      'execution_mode',
      'draft_id',
      'draft_revision',
    ],
    description:
      '``POST /generation/drafts/{id}/previews`` and ``GET /generation/previews/{id}``.',
  },
  create_generation_regeneration_draft_201: {
    properties: {
      id: { type: 'string' },
      product_id: { type: 'string' },
      provider: { type: 'string' },
      model: { type: 'string' },
      execution_mode: { type: 'string' },
      state: { type: 'string' },
      revision: { type: 'integer' },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      selectors: { additionalProperties: true, type: 'object' },
      combination_strategy: { additionalProperties: true, type: 'object' },
      format_config: { additionalProperties: true, type: 'object' },
      exclusions: { additionalProperties: true, type: 'object' },
      regeneration_intent: { type: 'string', default: 'new_generation' },
      source_asset_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      source_recipe_identity: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'product_id',
      'provider',
      'model',
      'execution_mode',
      'state',
      'revision',
    ],
    description:
      '``GET /generation/drafts/{id}`` and the create/update/regenerate result.',
  },
  create_generation_regeneration_draft_request: {
    properties: {
      intent: { type: 'string', minLength: 1 },
      prompt_overrides: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      qc_attempt_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['intent'],
  },
  create_generation_review_summary_regeneration_201: {
    properties: {
      drafts: {
        items: { $ref: '#/components/schemas/RegenerationDraftBatchItem' },
        type: 'array',
      },
    },
    type: 'object',
    description:
      "``POST /generation/regeneration-draft-batches`` result (Issue #49\nTask 7, spec §0.3/§0.4). Browser Task 10's ``ReviewSummary`` consumes\nthis.",
  },
  create_generation_review_summary_regeneration_request: {
    properties: {
      action: { type: 'string', minLength: 1 },
      candidate_version_ids: {
        items: { type: 'string' },
        type: 'array',
        minItems: 1,
      },
    },
    type: 'object',
    required: ['action', 'candidate_version_ids'],
  },
  create_generation_run_recovery_preview_201: {
    properties: {
      purpose: {
        type: 'string',
        const: 'run_recovery',
        default: 'run_recovery',
      },
      id: { type: 'string' },
      preview_hash: { type: 'string' },
      command: { type: 'string' },
      targets: { items: { type: 'string' }, type: 'array' },
      estimated_additional_cost: { $ref: '#/components/schemas/MaximumCost' },
      valid_until: { type: 'string' },
      run_id: { type: 'string', default: '' },
      expected_run_revision: { type: 'integer', default: 0 },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'preview_hash',
      'command',
      'estimated_additional_cost',
      'valid_until',
    ],
    description:
      'POST /generation/runs/{id}/recovery-previews and\nGET /generation/previews/{id} when purpose is run_recovery.',
  },
  create_generation_run_recovery_preview_request: {
    properties: {
      command: { type: 'string', minLength: 1 },
      selection: { $ref: '#/components/schemas/RecoveryPreviewSelection' },
    },
    type: 'object',
    required: ['command', 'selection'],
  },
  create_library_entry_201: {
    properties: {
      id: { type: 'string' },
      entry_type: { type: 'string' },
      display_name: { type: 'string' },
      lifecycle: { type: 'string' },
      current_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      draft: {
        anyOf: [
          { $ref: '#/components/schemas/DraftSummary' },
          { type: 'null' },
        ],
      },
      versions: {
        items: { $ref: '#/components/schemas/LibraryVersionSummary' },
        type: 'array',
      },
      revision: { type: 'integer', default: 1 },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['id', 'entry_type', 'display_name', 'lifecycle'],
    description:
      "``GET /library/entries/{id}`` and the create/save/archive/fork/clone result.\n\n``revision`` tracks the entry's open Draft (defaults to 1 once archived\nand the Draft has been dropped) and is the value clients must echo back\nvia ``If-Match`` on the next mutating command.",
  },
  create_library_entry_request: {
    properties: {
      entry_type: { type: 'string' },
      display_name: { type: 'string', maxLength: 300, minLength: 1 },
      content: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['entry_type', 'display_name'],
  },
  create_library_internal_entry_201: {
    properties: {
      id: { type: 'string' },
      entry_type: { type: 'string' },
      display_name: { type: 'string' },
      lifecycle: { type: 'string' },
      current_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      draft: {
        anyOf: [
          { $ref: '#/components/schemas/DraftSummary' },
          { type: 'null' },
        ],
      },
      versions: {
        items: { $ref: '#/components/schemas/LibraryVersionSummary' },
        type: 'array',
      },
      revision: { type: 'integer', default: 1 },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['id', 'entry_type', 'display_name', 'lifecycle'],
    description:
      "``GET /library/entries/{id}`` and the create/save/archive/fork/clone result.\n\n``revision`` tracks the entry's open Draft (defaults to 1 once archived\nand the Draft has been dropped) and is the value clients must echo back\nvia ``If-Match`` on the next mutating command.",
  },
  create_library_internal_entry_request: {
    properties: {
      entry_type: { type: 'string' },
      display_name: { type: 'string', maxLength: 300, minLength: 1 },
      content: { additionalProperties: true, type: 'object' },
      parent_type: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      parent_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['entry_type', 'display_name'],
  },
  create_library_reference_download_intent_201: {
    properties: {
      version_id: { type: 'string' },
      object_id: { type: 'string' },
      download_url: { type: 'string' },
      content_disposition: { type: 'string' },
      expires_at: { type: 'string' },
      derivative_mime: { type: 'string' },
      derivative_byte_size: { type: 'integer' },
    },
    type: 'object',
    required: [
      'version_id',
      'object_id',
      'download_url',
      'content_disposition',
      'expires_at',
      'derivative_mime',
      'derivative_byte_size',
    ],
    description:
      '201 response for ``POST /versions/{id}/download-intent``.\n\n``download_url`` is a short-lived signed capability issued by the\n``BlobStore`` adapter for the controlled derivative -- the browser opens it\ndirectly against the storage origin (``NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS``),\nexactly like generated-asset downloads; it never transits FastAPI or Next.js.',
  },
  create_library_reference_object_202: {
    properties: {
      operation_id: { type: 'string' },
      state: { type: 'string', default: 'queued' },
      command_type: { type: 'string' },
    },
    type: 'object',
    required: ['operation_id', 'command_type'],
    description:
      '202 Accepted envelope for async domain commands (connection-checks, manual-fact-imports, reference-gate).',
  },
  create_library_reference_object_request: {
    properties: {
      upload_intent_id: { type: 'string' },
      filename: { type: 'string', default: '' },
      declared_mime: { type: 'string', default: '' },
      execution_mode: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
        description:
          'Ignored. The content-safety execution mode is decided by the server.',
        deprecated: true,
      },
      safety_scenario: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
        description:
          'Ignored. The controlled-Mock scenario is decided by the server.',
        deprecated: true,
      },
      rights: {
        anyOf: [
          { $ref: '#/components/schemas/ReferenceRightsDeclarationInput' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['upload_intent_id'],
  },
  create_localization_batch_201: {
    properties: {
      batch_id: { type: 'string' },
      phase: { type: 'string' },
      reused: { type: 'boolean' },
    },
    type: 'object',
    required: ['batch_id', 'phase', 'reused'],
  },
  create_localization_batch_request: {
    properties: {
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      truth_data_mode: { type: 'string', enum: ['real', 'mock'] },
      provider_execution_mode: {
        type: 'string',
        enum: ['byok', 'sandbox', 'mock'],
      },
      sources: {
        items: { $ref: '#/components/schemas/BatchSourceInputModel' },
        type: 'array',
        minItems: 1,
      },
      market_targets: {
        items: { $ref: '#/components/schemas/BatchMarketTargetInputModel' },
        type: 'array',
        minItems: 1,
      },
      requested_evidence_tier: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'product_version_id',
      'offer_version_id',
      'truth_data_mode',
      'provider_execution_mode',
      'sources',
      'market_targets',
    ],
  },
  create_marketing_evidence_handoff_201: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/CreatedEvidenceHandoff' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  create_marketing_evidence_handoff_request: {
    properties: {
      evaluation_id: { type: 'string', maxLength: 64, minLength: 1 },
      asset_version_id: { type: 'string', maxLength: 64, minLength: 1 },
      offer_version_id: { type: 'string', maxLength: 64, minLength: 1 },
      confirms_tested_context: { type: 'boolean' },
      note: { type: 'string', maxLength: 2000, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'evaluation_id',
      'asset_version_id',
      'offer_version_id',
      'confirms_tested_context',
      'note',
    ],
  },
  create_marketing_handoff_202: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  create_marketing_handoff_request: {
    properties: {
      asset_version_ids: {
        items: { type: 'string' },
        type: 'array',
        maxItems: 60,
        minItems: 1,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['asset_version_ids'],
  },
  create_marketing_image_edit_202: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  create_marketing_image_edit_request: {
    properties: {
      asset_version_id: { type: 'string', maxLength: 64, minLength: 1 },
      crop: {
        anyOf: [{ $ref: '#/components/schemas/ImageCrop' }, { type: 'null' }],
      },
      rotation: { type: 'integer', enum: [0, 90, 180, 270], default: 0 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['asset_version_id'],
  },
  create_marketing_project_201: {
    properties: {
      id: { type: 'string' },
      revision: { type: 'integer' },
      state: {
        type: 'string',
        enum: [
          'input',
          'understanding_draft',
          'understanding_confirmed',
          'plan_draft',
          'plan_confirmed',
        ],
      },
      source_text: { type: 'string' },
      product_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      understanding: {
        anyOf: [
          { $ref: '#/components/schemas/Understanding' },
          { type: 'null' },
        ],
      },
      plan: {
        anyOf: [
          { $ref: '#/components/schemas/FrozenTestPlan' },
          { type: 'null' },
        ],
      },
      language_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      plan_review_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      brand_style_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      brand_kit: {
        anyOf: [
          { $ref: '#/components/schemas/BrandKitVersion' },
          { type: 'null' },
        ],
      },
      competitor_review_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      competitor_snapshot_stale: {
        anyOf: [{ type: 'boolean' }, { type: 'null' }],
      },
      competitor_market_unmatched: {
        anyOf: [{ type: 'boolean' }, { type: 'null' }],
      },
      evidence_status: {
        type: 'string',
        const: 'unvalidated',
        default: 'unvalidated',
      },
      latest_call: {
        anyOf: [
          { $ref: '#/components/schemas/PlanningCallSummary' },
          { type: 'null' },
        ],
      },
      generation_draft_ids: { items: { type: 'string' }, type: 'array' },
      product_references: {
        $ref: '#/components/schemas/ProductReferenceSelection',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'revision',
      'state',
      'source_text',
      'product_id',
      'understanding',
      'plan',
      'latest_call',
      'generation_draft_ids',
    ],
  },
  create_marketing_project_request: {
    properties: {
      source_text: { type: 'string', maxLength: 16000, default: '' },
      product_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
  },
  create_meta_binding_201: {},
  create_meta_binding_request: {
    properties: {
      ad_account_id: { type: 'string' },
      meta_mode: { type: 'string' },
      access_token_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      app_secret_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['ad_account_id', 'meta_mode'],
  },
  create_meta_connection_201: {},
  create_meta_connection_request: {
    properties: {
      meta_mode: { type: 'string' },
      ad_account_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['meta_mode'],
  },
  create_meta_contract_201: {},
  create_meta_contract_request: {
    properties: {
      connection_id: { type: 'string' },
      meta_mode: { type: 'string' },
      contract_type: { type: 'string' },
      ad_account_id: { type: 'string' },
      fields: { items: {}, type: 'array' },
      attribution_window: { additionalProperties: true, type: 'object' },
      breakdowns: { items: {}, type: 'array' },
      time_window: { additionalProperties: true, type: 'object' },
      local_timezone: { type: 'string' },
      delivery_config: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'connection_id',
      'meta_mode',
      'contract_type',
      'ad_account_id',
      'fields',
      'attribution_window',
      'breakdowns',
      'time_window',
      'local_timezone',
    ],
  },
  create_meta_launch_preview_201: {
    properties: {
      id: { type: 'string' },
      workspace_id: { type: 'string' },
      meta_mode: { type: 'string' },
      meta_connection_id: { type: 'string' },
      meta_binding_id: { type: 'string' },
      experiment_id: { type: 'string' },
      experiment_version_id: { type: 'string' },
      candidate_count: { type: 'integer' },
      campaign_config: { additionalProperties: true, type: 'object' },
      daily_budget_minor: { type: 'integer' },
      total_daily_cost_minor: { type: 'integer' },
      currency: { type: 'string' },
      preview_hash: { type: 'string' },
      state: { type: 'string' },
      confirmed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      expires_at: { type: 'string', format: 'date-time' },
      created_at: { type: 'string', format: 'date-time' },
      candidates: {
        items: { $ref: '#/components/schemas/LaunchCandidateResponse' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'workspace_id',
      'meta_mode',
      'meta_connection_id',
      'meta_binding_id',
      'experiment_id',
      'experiment_version_id',
      'candidate_count',
      'daily_budget_minor',
      'total_daily_cost_minor',
      'currency',
      'preview_hash',
      'state',
      'expires_at',
      'created_at',
    ],
  },
  create_meta_launch_preview_request: {
    properties: {
      experiment_id: { type: 'string' },
      experiment_version_id: { type: 'string' },
      connection_id: { type: 'string' },
      meta_mode: { type: 'string' },
      ad_account_id: { type: 'string' },
      candidates: {
        items: { $ref: '#/components/schemas/CandidateInput' },
        type: 'array',
      },
      daily_budget_minor: { type: 'integer' },
      currency: { type: 'string' },
      campaign_config: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: [
      'experiment_id',
      'experiment_version_id',
      'connection_id',
      'meta_mode',
      'ad_account_id',
      'candidates',
      'daily_budget_minor',
      'currency',
    ],
  },
  create_product_intake_202: {
    properties: {
      intake_id: { type: 'string' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: { type: 'string' },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['intake_id', 'status'],
    description:
      '202: the intake and the Operation to poll (``GET /operations/{id}``)\n-- or ``GET /products/intake/{intake_id}`` for the card itself.\n``replayed`` is true when the same input (within 24 h) or the same\nIdempotency-Key returned an existing intake.',
  },
  create_product_intake_request: {
    properties: {
      url: { anyOf: [{ type: 'string', maxLength: 2048 }, { type: 'null' }] },
      upload_intent_ids: {
        anyOf: [
          { items: { type: 'string' }, type: 'array', maxItems: 12 },
          { type: 'null' },
        ],
      },
      text: { anyOf: [{ type: 'string', maxLength: 20000 }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    description:
      'At least one of ``url``, ``upload_intent_ids`` (finalized uploads of\npurpose ``product_intake_image``) or ``text``.',
  },
  create_product_intake_image_download_intent_201: {
    properties: {
      intake_id: { type: 'string' },
      image_id: { type: 'string' },
      reference_object_id: { type: 'string' },
      download_url: { type: 'string' },
      expires_at: { type: 'string' },
      mime_type: { type: 'string' },
      byte_size: { type: 'integer' },
      width: { type: 'integer' },
      height: { type: 'integer' },
    },
    type: 'object',
    required: [
      'intake_id',
      'image_id',
      'reference_object_id',
      'download_url',
      'expires_at',
      'mime_type',
      'byte_size',
      'width',
      'height',
    ],
  },
  create_results_import_201: {
    properties: {
      import_id: { type: 'string' },
      product_id: { type: 'string' },
      source: { type: 'string' },
      status: { type: 'string', enum: ['previewed', 'committed'] },
      file_name: { type: 'string', default: '' },
      currency: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      summary: { $ref: '#/components/schemas/ResultsImportSummary' },
      rows: {
        items: { $ref: '#/components/schemas/ResultsImportRow' },
        type: 'array',
      },
      rows_truncated: { type: 'boolean', default: false },
      snapshot_ids: { items: { type: 'string' }, type: 'array' },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      committed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      duplicate: { type: 'boolean', default: false },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['import_id', 'product_id', 'source', 'status', 'summary'],
    description:
      'A previewed (``previewed``) or committed (``committed``) import.\n``rows`` lists every rejected row and the first accepted ones\n(``rows_truncated``); ``duplicate`` is true when the same rows were\nalready imported for this product (the existing import is returned).',
  },
  create_results_import_request: {
    properties: {
      product_id: { type: 'string', maxLength: 64, minLength: 1 },
      source: { type: 'string', enum: ['csv', 'manual', 'meta_sync'] },
      csv_text: {
        anyOf: [{ type: 'string', maxLength: 300000 }, { type: 'null' }],
      },
      upload_intent_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
      rows: {
        anyOf: [
          {
            items: { $ref: '#/components/schemas/ManualResultRow' },
            type: 'array',
            maxItems: 2000,
          },
          { type: 'null' },
        ],
      },
      currency: { anyOf: [{ type: 'string', maxLength: 3 }, { type: 'null' }] },
      file_name: {
        anyOf: [{ type: 'string', maxLength: 300 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id', 'source'],
    description:
      "``csv``: exactly one of ``csv_text`` (a pasted table or a small file\nread by the browser, <= 300,000 characters) or ``upload_intent_id`` (a\nfinalized ``results_import`` upload, <= 10 MB, <= 5,000 rows).\n``manual``: ``rows`` (<= 2,000).\n``meta_sync``: nothing else -- the synced Meta metrics of ads bound to\nthis product's images. ``currency`` applies to rows without one.",
  },
  create_scale_activation_preview_201: {
    properties: {
      id: { type: 'string' },
      enrollment_id: { type: 'string' },
      meta_mode: { type: 'string' },
      scenario_scope_key: { type: 'string' },
      transition_kind: { type: 'string' },
      delivery_evidence_snapshot_id: { type: 'string' },
      lineage_guard_id: { type: 'string' },
      candidate_fingerprint: { type: 'string' },
      guard_revision: { type: 'integer' },
      lineage_hash: { type: 'string' },
      enrollment_revision: { type: 'integer' },
      expires_at: { type: 'string', format: 'date-time' },
      consumed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      created_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'enrollment_id',
      'meta_mode',
      'scenario_scope_key',
      'transition_kind',
      'delivery_evidence_snapshot_id',
      'lineage_guard_id',
      'candidate_fingerprint',
      'guard_revision',
      'lineage_hash',
      'enrollment_revision',
      'expires_at',
      'consumed_at',
      'created_at',
    ],
  },
  create_scale_activation_preview_request: {
    properties: { expected_revision: { type: 'integer' } },
    additionalProperties: false,
    type: 'object',
    required: ['expected_revision'],
  },
  create_scale_advice_disposition_201: {
    properties: {
      id: { type: 'string' },
      thread_id: { type: 'string' },
      occurrence_id: { type: 'string' },
      kind: { type: 'string' },
      note: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      actor_id: { type: 'string' },
      created_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'thread_id',
      'occurrence_id',
      'kind',
      'note',
      'actor_id',
      'created_at',
    ],
  },
  create_scale_advice_disposition_request: {
    properties: {
      kind: { type: 'string', minLength: 1 },
      note: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      occurrence_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['kind'],
    description:
      "``kind`` is validated by the domain layer\n(``app.scale.advice.create_disposition`` rejects everything but\n``acknowledged``/``dismissed`` with ``unsupported_disposition_kind``,\nmapped to 422 by ``app.scale.v1_router``'s problem handler) rather than\nre-validated here, so that single rejection reason stays authoritative.",
  },
  create_scale_advice_snooze_201: {
    properties: {
      id: { type: 'string' },
      thread_id: { type: 'string' },
      actor_id: { type: 'string' },
      started_at: { type: 'string', format: 'date-time' },
      snooze_until: { type: 'string', format: 'date-time' },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'thread_id',
      'actor_id',
      'started_at',
      'snooze_until',
      'reason',
    ],
  },
  create_scale_advice_snooze_request: {
    properties: {
      until: { type: 'string', format: 'date-time' },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['until'],
  },
  create_scale_enrollment_201: {
    properties: {
      id: { type: 'string' },
      meta_mode: { type: 'string' },
      meta_connection_id: { type: 'string' },
      scenario_scope_key: { type: 'string' },
      ad_account_id: { type: 'string' },
      winner_decision_id: { type: 'string' },
      winner_confirmation_id: { type: 'string' },
      asset_version_id: { type: 'string' },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      market_code: { type: 'string' },
      landing_page_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      market_asset_binding_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      campaign_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ad_set_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ad_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      remote_binding_key: { type: 'string' },
      lifecycle: { type: 'string' },
      transition_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      current_episode_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      creator_id: { type: 'string' },
      original_initiator_id: { type: 'string' },
      revision: { type: 'integer' },
      evaluation_cancellation_generation: { type: 'integer' },
      created_at: { type: 'string', format: 'date-time' },
      updated_at: { type: 'string', format: 'date-time' },
      closed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibilityModel' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'meta_mode',
      'meta_connection_id',
      'scenario_scope_key',
      'ad_account_id',
      'winner_decision_id',
      'winner_confirmation_id',
      'asset_version_id',
      'product_version_id',
      'offer_version_id',
      'market_code',
      'landing_page_version_id',
      'market_asset_binding_id',
      'campaign_remote_id',
      'ad_set_remote_id',
      'ad_remote_id',
      'remote_binding_key',
      'lifecycle',
      'transition_reason',
      'current_episode_id',
      'creator_id',
      'original_initiator_id',
      'revision',
      'evaluation_cancellation_generation',
      'created_at',
      'updated_at',
      'closed_at',
      'allowed_actions',
    ],
  },
  create_scale_enrollment_request: {
    properties: { enrollment_candidate_id: { type: 'string', minLength: 1 } },
    additionalProperties: false,
    type: 'object',
    required: ['enrollment_candidate_id'],
    description:
      'Issue #86 Task 9: the only field a caller may supply is the id of an\nalready-proven ``ScaleEnrollmentCandidateRecord`` -- every raw Winner/\nDeliveryTarget identifier a pre-#86 client sent directly (and\n``creator_id``, which the server now derives exclusively from the\nverified ``TenantContext``) is rejected as an unknown field by\n``_Base``\'s ``extra="forbid"``, surfacing as a 422.',
  },
  create_scale_enrollment_candidate_201: {
    properties: {
      id: { type: 'string' },
      meta_mode: { type: 'string' },
      scenario_scope_key: { type: 'string' },
      winner_decision_id: { type: 'string' },
      delivery_target_id: { type: 'string' },
      candidate_fingerprint: { type: 'string' },
      expires_at: { type: 'string', format: 'date-time' },
      consumed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      created_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'meta_mode',
      'scenario_scope_key',
      'winner_decision_id',
      'delivery_target_id',
      'candidate_fingerprint',
      'expires_at',
      'consumed_at',
      'created_at',
    ],
  },
  create_scale_enrollment_candidate_request: {
    properties: {
      winner_decision_id: { type: 'string', minLength: 1 },
      delivery_target_id: { type: 'string', minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['winner_decision_id', 'delivery_target_id'],
    description:
      'Wire shape for creating one short-lived Enrollment Candidate (spec\n§4.4). ``winner_decision_id``/``delivery_target_id`` are still raw\nidentifiers here -- that is expected: this is the one endpoint whose\nentire job is proving a caller-named pair is currently a valid lineage,\nbefore handing back the opaque, narrow-scoped candidate id every other\nScale write actually consumes.',
  },
  create_scale_export_202: {
    properties: {
      id: { type: 'string' },
      operation_id: { type: 'string' },
      enrollment_id: { type: 'string' },
      episode_id: { type: 'string' },
      export_kind: { type: 'string' },
      requested_by: { type: 'string' },
      requested_at: { type: 'string', format: 'date-time' },
      completed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      result_object_reference: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'operation_id',
      'enrollment_id',
      'episode_id',
      'export_kind',
      'requested_by',
      'requested_at',
      'completed_at',
      'result_object_reference',
    ],
  },
  create_scale_export_request: {
    properties: {
      enrollment_id: { type: 'string', minLength: 1 },
      episode_id: { type: 'string', minLength: 1 },
      export_kind: { type: 'string', minLength: 1 },
      snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      evaluation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['enrollment_id', 'episode_id', 'export_kind'],
  },
  create_scale_export_download_intent_201: {
    properties: {
      intent_id: { type: 'string' },
      expires_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['intent_id', 'expires_at'],
  },
  create_scale_target_201: {
    properties: {
      id: { type: 'string' },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      market_code: { type: 'string' },
      currency: { type: 'string' },
      target_cpa: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      breakeven_roas: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      minimum_economic_spend: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      lifecycle_state: { type: 'string' },
      creator_id: { type: 'string' },
      revision: { type: 'integer' },
      published_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      withdrawn_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      created_at: { type: 'string', format: 'date-time' },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibilityModel' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'product_version_id',
      'offer_version_id',
      'market_code',
      'currency',
      'target_cpa',
      'breakeven_roas',
      'minimum_economic_spend',
      'lifecycle_state',
      'creator_id',
      'revision',
      'published_at',
      'withdrawn_at',
      'created_at',
      'allowed_actions',
    ],
  },
  create_scale_target_request: {
    properties: {
      publication_preview_id: { type: 'string', maxLength: 64, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['publication_preview_id'],
  },
  create_scale_target_publication_preview_201: {
    properties: {
      id: { type: 'string' },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      market_code: { type: 'string' },
      currency: { type: 'string' },
      target_cpa: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      breakeven_roas: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      minimum_economic_spend: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      affected_enrollment_count: { type: 'integer' },
      policy_floor_check_passed: { type: 'boolean' },
      impact_hash: { type: 'string' },
      expires_at: { type: 'string', format: 'date-time' },
      created_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'product_version_id',
      'offer_version_id',
      'market_code',
      'currency',
      'target_cpa',
      'breakeven_roas',
      'minimum_economic_spend',
      'affected_enrollment_count',
      'policy_floor_check_passed',
      'impact_hash',
      'expires_at',
      'created_at',
    ],
  },
  create_scale_target_publication_preview_request: {
    properties: {
      product_version_id: { type: 'string', minLength: 1 },
      offer_version_id: { type: 'string', minLength: 1 },
      market_code: { type: 'string', maxLength: 10, minLength: 1 },
      currency: { type: 'string', maxLength: 3, minLength: 3 },
      target_cpa: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      breakeven_roas: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      minimum_economic_spend: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'product_version_id',
      'offer_version_id',
      'market_code',
      'currency',
    ],
  },
  create_scale_target_withdrawal_preview_201: {
    properties: {
      id: { type: 'string' },
      target_version_id: { type: 'string' },
      reason: { type: 'string' },
      affected_enrollment_count: { type: 'integer' },
      dependent_advice_thread_count: { type: 'integer' },
      impact_hash: { type: 'string' },
      expires_at: { type: 'string', format: 'date-time' },
      created_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'target_version_id',
      'reason',
      'affected_enrollment_count',
      'dependent_advice_thread_count',
      'impact_hash',
      'expires_at',
      'created_at',
    ],
  },
  create_scale_target_withdrawal_preview_request: {
    properties: { reason: { type: 'string', maxLength: 200, minLength: 1 } },
    additionalProperties: false,
    type: 'object',
    required: ['reason'],
  },
  create_test_plan_202: {
    properties: {
      plan_id: { type: 'string' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: { type: 'string' },
      revision: { type: 'integer' },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['plan_id', 'status', 'revision'],
    description:
      '202: poll ``GET /operations/{operation_id}`` or ``GET /test-plans/{plan_id}``.',
  },
  create_test_plan_request: {
    properties: {
      product_id: { type: 'string', maxLength: 64, minLength: 1 },
      goal: {
        type: 'string',
        enum: ['explore', 'optimize'],
        default: 'explore',
      },
      audience_text: {
        anyOf: [{ type: 'string', maxLength: 500 }, { type: 'null' }],
      },
      audience_card_index: {
        anyOf: [{ type: 'integer', maximum: 20, minimum: 0 }, { type: 'null' }],
      },
      scale: {
        type: 'string',
        enum: ['sample', 'small', 'full'],
        default: 'sample',
      },
      seed_asset_version_ids: {
        items: { type: 'string' },
        type: 'array',
        maxItems: 10,
      },
      from_learnings_snapshot_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id'],
    description:
      "The three questions (contract §3.2), each with a default: ``goal``\n``explore``; ``audience_card_index`` the product's first audience card\n(or ``audience_text`` to word it yourself); ``scale`` ``sample``.\n``optimize`` needs 1-10 ``seed_asset_version_ids`` (Approved assets or\nWinners of this Workspace).",
  },
  create_test_plan_run_202: {
    properties: {
      stage: { type: 'string' },
      target_scale: { type: 'string' },
      status: { type: 'string' },
      plan_revision: { type: 'integer' },
      run_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      run_status: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      items_total: { type: 'integer', default: 0 },
      items_done: { type: 'integer', default: 0 },
      items_failed: { type: 'integer', default: 0 },
      counts: { additionalProperties: true, type: 'object' },
      defaults: { additionalProperties: true, type: 'object' },
      template_entry_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      template_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      generation_draft_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      preview_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      failure: {
        anyOf: [{ $ref: '#/components/schemas/RunFailure' }, { type: 'null' }],
      },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      plan_id: { type: 'string' },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['stage', 'target_scale', 'status', 'plan_revision', 'plan_id'],
  },
  create_test_plan_run_request: {
    properties: { stage: { type: 'string', enum: ['sample', 'full'] } },
    additionalProperties: false,
    type: 'object',
    required: ['stage'],
    description:
      "``sample``: one combination per direction. ``full``: the plan's\nsmall/full selection (``full`` when the plan's scale is ``sample``),\nskipping combinations the sample Run already produced.",
  },
  create_upload_intent_201: {
    properties: {
      intent_id: { type: 'string' },
      state: {
        type: 'string',
        enum: ['pending', 'verifying', 'finalized', 'rejected', 'expired'],
      },
      purpose: { type: 'string' },
      expires_at: { type: 'string', format: 'date-time' },
      upload_grant: { $ref: '#/components/schemas/UploadGrantResponse' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['intent_id', 'state', 'purpose', 'expires_at', 'upload_grant'],
    description:
      'Declared only so `generate_openapi.py` can extract a real response\nschema (see the identical note on `IdentityProbeResponse`); the handler\nreturns a `JSONResponse` directly.',
  },
  create_upload_intent_request: {
    properties: {
      purpose: { type: 'string' },
      owner_kind: { type: 'string' },
      owner_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      filename: { type: 'string' },
      content_type: { type: 'string' },
      content_length_min: { type: 'integer' },
      content_length_max: { type: 'integer' },
      checksum_sha256_b64: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'purpose',
      'owner_kind',
      'filename',
      'content_type',
      'content_length_min',
      'content_length_max',
      'checksum_sha256_b64',
    ],
  },
  decide_competitor_discovery_200: {
    properties: {
      discovery: { $ref: '#/components/schemas/CompetitorDiscovery' },
      decision: { $ref: '#/components/schemas/CompetitorDiscoveryDecision' },
      competitor: {
        anyOf: [{ $ref: '#/components/schemas/Competitor' }, { type: 'null' }],
      },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['discovery', 'decision'],
    description:
      'The recommendation after the decision, the decision in effect, and the\ncompetitor an accept created (null for a reject).',
  },
  decide_competitor_discovery_request: {
    properties: {
      decision: { type: 'string', enum: ['accept', 'reject'] },
      reason: { anyOf: [{ type: 'string', maxLength: 500 }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['decision'],
    description:
      "``accept`` adds the advertiser as a competitor confirmed by the page\nID from its ads (no resolve, no charge); ``reject`` keeps it out of the\nproduct's later scans. ``reason`` is optional.",
  },
  delete_product_reference_200: {
    properties: {
      product_id: { type: 'string' },
      entry_id: { type: 'string' },
      version_id: { type: 'string' },
      revision: { type: 'integer' },
      model_variants: { items: { type: 'string' }, type: 'array' },
      has_unpublished_changes: { type: 'boolean', default: false },
      items: {
        items: { $ref: '#/components/schemas/ProductReferenceItem' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'product_id',
      'entry_id',
      'version_id',
      'revision',
      'model_variants',
      'items',
    ],
    description:
      "A projection of the product's immutable Library version/bindings.\n\n``revision`` is the If-Match value for edits. Deleted items remain in\nprior ProductVersions; ``version_id`` can be frozen by later consumers.",
  },
  download_marketing_handoff_201: {
    properties: {
      intent_id: { type: 'string' },
      expires_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      download_grant: {
        anyOf: [
          { $ref: '#/components/schemas/DownloadGrant' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['intent_id'],
    description:
      'Shared shape for asset-version/export download-intent results.\n\n``download_grant`` is optional because the export-package route\n(``create_generation_export_download_intent``) does not yet issue one\n(Issue #49 Task 9 wires that); the asset-version route always populates\nit.',
  },
  edit_marketing_project_200: {
    properties: {
      id: { type: 'string' },
      revision: { type: 'integer' },
      state: {
        type: 'string',
        enum: [
          'input',
          'understanding_draft',
          'understanding_confirmed',
          'plan_draft',
          'plan_confirmed',
        ],
      },
      source_text: { type: 'string' },
      product_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      understanding: {
        anyOf: [
          { $ref: '#/components/schemas/Understanding' },
          { type: 'null' },
        ],
      },
      plan: {
        anyOf: [
          { $ref: '#/components/schemas/FrozenTestPlan' },
          { type: 'null' },
        ],
      },
      language_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      plan_review_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      brand_style_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      brand_kit: {
        anyOf: [
          { $ref: '#/components/schemas/BrandKitVersion' },
          { type: 'null' },
        ],
      },
      competitor_review_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      competitor_snapshot_stale: {
        anyOf: [{ type: 'boolean' }, { type: 'null' }],
      },
      competitor_market_unmatched: {
        anyOf: [{ type: 'boolean' }, { type: 'null' }],
      },
      evidence_status: {
        type: 'string',
        const: 'unvalidated',
        default: 'unvalidated',
      },
      latest_call: {
        anyOf: [
          { $ref: '#/components/schemas/PlanningCallSummary' },
          { type: 'null' },
        ],
      },
      generation_draft_ids: { items: { type: 'string' }, type: 'array' },
      product_references: {
        $ref: '#/components/schemas/ProductReferenceSelection',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'revision',
      'state',
      'source_text',
      'product_id',
      'understanding',
      'plan',
      'latest_call',
      'generation_draft_ids',
    ],
  },
  edit_marketing_project_request: {
    properties: {
      command: {
        type: 'string',
        enum: [
          'save_understanding',
          'confirm_understanding',
          'save_plan',
          'confirm_plan',
          'revise_source',
        ],
      },
      understanding: {
        anyOf: [
          { $ref: '#/components/schemas/Understanding' },
          { type: 'null' },
        ],
      },
      plan: {
        anyOf: [{ $ref: '#/components/schemas/TestPlan' }, { type: 'null' }],
      },
      source_text: {
        anyOf: [{ type: 'string', maxLength: 16000 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['command'],
  },
  evaluate_marketing_round_200: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  execute_meta_launch_preview_202: {
    properties: {
      id: { type: 'string' },
      operation_id: { type: 'string' },
      operation_state: { type: 'string' },
      provisioning_state: { type: 'string' },
    },
    type: 'object',
    required: ['id', 'operation_id', 'operation_state', 'provisioning_state'],
  },
  execute_meta_launch_preview_request: {
    properties: {
      meta_mode: { type: 'string' },
      confirmation_token: { type: 'string', minLength: 32 },
    },
    type: 'object',
    required: ['meta_mode', 'confirmation_token'],
  },
  finalize_upload_202: {
    properties: {
      operation_id: { type: 'string' },
      state: {
        type: 'string',
        enum: [
          'queued',
          'running',
          'partially_succeeded',
          'succeeded',
          'failed',
          'cancelled',
          'uncertain',
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['operation_id', 'state'],
    description:
      'See the note on `UploadIntentResponse` -- the handler returns a\n`JSONResponse` directly.',
  },
  flip_competitor_ad_relevance_200: {
    properties: {
      assessment_id: { type: 'string' },
      ad_id: { type: 'string' },
      scan_id: { type: 'string' },
      kind: { type: 'string', const: 'relevance' },
      revision: { type: 'integer' },
      source: { type: 'string', enum: ['ai', 'rule', 'user'] },
      relevance: {
        type: 'string',
        enum: ['relevant', 'adjacent', 'unrelated'],
      },
      confidence: { anyOf: [{ type: 'number' }, { type: 'null' }] },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      language: {
        anyOf: [{ type: 'string', enum: ['en', 'zh-CN'] }, { type: 'null' }],
      },
      rule: {
        anyOf: [
          {
            type: 'string',
            enum: ['personal_account', 'wholesale_site', 'dealer'],
          },
          { type: 'null' },
        ],
      },
      previous: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdAssessmentPrevious' },
          { type: 'null' },
        ],
      },
      evidence: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdAssessmentEvidence' },
          { type: 'null' },
        ],
      },
      attempt_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'assessment_id',
      'ad_id',
      'scan_id',
      'kind',
      'revision',
      'source',
      'relevance',
    ],
    description:
      "One append-only revision of an observation's relevance. ``created_by``\nis the member who flipped it (null for the scan's own judgments);\n``previous`` is what a flip replaced.",
  },
  flip_competitor_ad_relevance_request: {
    properties: {
      relevance: {
        type: 'string',
        enum: ['relevant', 'adjacent', 'unrelated'],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['relevance'],
    description:
      "A member's verdict on one observation; flipping back is one more revision.",
  },
  fork_library_version_201: {
    properties: {
      id: { type: 'string' },
      entry_type: { type: 'string' },
      display_name: { type: 'string' },
      lifecycle: { type: 'string' },
      current_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      draft: {
        anyOf: [
          { $ref: '#/components/schemas/DraftSummary' },
          { type: 'null' },
        ],
      },
      versions: {
        items: { $ref: '#/components/schemas/LibraryVersionSummary' },
        type: 'array',
      },
      revision: { type: 'integer', default: 1 },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['id', 'entry_type', 'display_name', 'lifecycle'],
    description:
      "``GET /library/entries/{id}`` and the create/save/archive/fork/clone result.\n\n``revision`` tracks the entry's open Draft (defaults to 1 once archived\nand the Draft has been dropped) and is the value clients must echo back\nvia ``If-Match`` on the next mutating command.",
  },
  fork_library_version_request: {
    properties: {
      display_name: { type: 'string', maxLength: 300, minLength: 1 },
    },
    type: 'object',
    required: ['display_name'],
  },
  freeze_experiment_200: {
    properties: {
      id: { type: 'string' },
      product_id: { type: 'string' },
      name: { type: 'string' },
      state: { type: 'string' },
      revision: { type: 'integer' },
      record_schema_version: { type: 'integer' },
      draft_context: { additionalProperties: true, type: 'object' },
      frozen_snapshot_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { type: 'string', format: 'date-time' },
      frozen_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      started_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      closed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      frozen_context: {
        anyOf: [
          { $ref: '#/components/schemas/ExperimentFrozenContext' },
          { type: 'null' },
        ],
      },
      entries: {
        items: { $ref: '#/components/schemas/ExperimentEntry' },
        type: 'array',
      },
      candidates: {
        items: { $ref: '#/components/schemas/ExperimentCandidate' },
        type: 'array',
      },
      judgements: {
        items: { $ref: '#/components/schemas/ExperimentJudgement' },
        type: 'array',
      },
      proposals: {
        items: { $ref: '#/components/schemas/ExperimentProposal' },
        type: 'array',
      },
      decisions: {
        items: { $ref: '#/components/schemas/ExperimentDecision' },
        type: 'array',
      },
      concept_resolution_evidence: {
        anyOf: [
          { $ref: '#/components/schemas/ConceptResolutionEvidence' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'id',
      'product_id',
      'name',
      'state',
      'revision',
      'record_schema_version',
      'created_at',
    ],
  },
  freeze_experiment_request: {
    properties: {
      expected_revision: { type: 'integer', minimum: 1 },
      analyzed_candidate_set_hash: {
        type: 'string',
        maxLength: 64,
        minLength: 64,
      },
      concept_resolution_plan: {
        items: { $ref: '#/components/schemas/ConceptResolutionActionRequest' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['expected_revision', 'analyzed_candidate_set_hash'],
  },
  freeze_marketing_round_201: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  freeze_marketing_round_request: {
    properties: {
      start_date: { type: 'string', format: 'date' },
      end_date: { type: 'string', format: 'date' },
      account_timezone: { type: 'string', maxLength: 64, default: 'UTC' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      protocol: { $ref: '#/components/schemas/EvaluationProtocol-Input' },
      attribution_window: {
        type: 'string',
        enum: [
          '1d_click',
          '7d_click',
          '1d_view',
          '1d_click_1d_view',
          '7d_click_1d_view',
        ],
        default: '7d_click_1d_view',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['start_date', 'end_date', 'execution_mode', 'protocol'],
  },
  get_competitor_200: {
    properties: {
      competitor_id: { type: 'string' },
      product_id: { type: 'string' },
      role: { type: 'string', enum: ['competitor', 'self'] },
      status: { type: 'string', enum: ['active', 'archived'] },
      name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      website_domain: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      input_kind: {
        type: 'string',
        enum: ['name', 'facebook_page', 'ad_library'],
      },
      input_page_alias: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      confirmation_status: {
        type: 'string',
        enum: ['confirmed', 'unconfirmed'],
      },
      confirmation: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorConfirmation' },
          { type: 'null' },
        ],
      },
      observed_page_name: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      last_scanned_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      latest_resolution: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorResolutionSummary' },
          { type: 'null' },
        ],
      },
      can_resolve: { type: 'boolean', default: false },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      archived_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      archived_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'competitor_id',
      'product_id',
      'role',
      'status',
      'input_kind',
      'confirmation_status',
    ],
    description:
      "A competitor of a product (or ``role=self``, the product's own page).\n``input_kind`` is how it was added; ``can_resolve`` says whether a\nname search is possible (a name, or the alias of a pasted page link).\n\nTwo read-only projections from the product's scans, neither a call nor\na change to the confirmation: ``observed_page_name`` is the advertiser\nname of the confirmed page's newest observation (a link confirms a page\nwithout its name; null until a scan sees the page), and\n``last_scanned_at`` is when the newest finished scan (``completed``,\n``partial``, ``failed`` or ``uncertain``) that included this competitor\nfinished (null when none has).",
  },
  get_competitor_resolution_200: {
    properties: {
      competitor_id: { type: 'string' },
      resolve_job_id: { type: 'string' },
      operation_id: { type: 'string' },
      status: {
        type: 'string',
        enum: ['queued', 'running', 'completed', 'failed', 'uncertain'],
      },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      provider: { type: 'string' },
      query: { type: 'string' },
      website_domain: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      country: { type: 'string' },
      quote: { $ref: '#/components/schemas/CompetitorResolveQuote' },
      cost: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorResolveCost' },
          { type: 'null' },
        ],
      },
      candidates: {
        items: { $ref: '#/components/schemas/CompetitorCandidate' },
        type: 'array',
      },
      auto_confirmed: { type: 'boolean', default: false },
      confirmation_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      waits: { type: 'integer', default: 0 },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorResolveFailure' },
          { type: 'null' },
        ],
      },
      reusable: { type: 'boolean', default: false },
      expires_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      started_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'competitor_id',
      'resolve_job_id',
      'operation_id',
      'status',
      'execution_mode',
      'provider',
      'query',
      'country',
      'quote',
    ],
    description:
      "A resolve: ``queued`` -> ``running`` -> ``completed``; ``failed``\n(the search failed, or a 402/401/403 stopped the probes -- candidates\nalready found stay listed) or ``uncertain`` (a call's outcome is\nunknown; nothing is resent). ``waits`` counts how often it queued\nbehind another market-data job on the same key (spec §5.1). A finished\nresolve is ``reusable`` for confirmation until ``expires_at`` (72 hours).\n``auto_confirmed`` is true when it confirmed the page itself (exact name\nand domain hit, or the alias of a pasted page link).",
  },
  get_competitor_scan_200: {
    properties: {
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      operation_id: { type: 'string' },
      job_id: { type: 'string' },
      status: {
        type: 'string',
        enum: [
          'queued',
          'running',
          'completed',
          'partial',
          'failed',
          'uncertain',
        ],
      },
      stage: {
        anyOf: [
          {
            type: 'string',
            enum: ['fetch', 'media', 'filter', 'tag', 'cluster', 'freeze'],
          },
          { type: 'null' },
        ],
      },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      provider: { type: 'string' },
      platform: { type: 'string', const: 'meta', default: 'meta' },
      country: { type: 'string' },
      params: { $ref: '#/components/schemas/CompetitorScanParams' },
      targets: {
        items: { $ref: '#/components/schemas/CompetitorScanTarget' },
        type: 'array',
      },
      steps: {
        items: { $ref: '#/components/schemas/CompetitorScanStepState' },
        type: 'array',
      },
      quote: { $ref: '#/components/schemas/CompetitorScanQuote' },
      cost: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorScanCost' },
          { type: 'null' },
        ],
      },
      initial_credential_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      resumes: {
        items: { $ref: '#/components/schemas/CompetitorScanResume' },
        type: 'array',
      },
      reconciliation: {
        items: { $ref: '#/components/schemas/CompetitorScanReconciliation' },
        type: 'array',
      },
      media: { $ref: '#/components/schemas/CompetitorScanMedia' },
      observation_count: { type: 'integer', default: 0 },
      waits: { type: 'integer', default: 0 },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorScanFailure' },
          { type: 'null' },
        ],
      },
      can_resume: { type: 'boolean', default: false },
      reclusters: {
        items: { $ref: '#/components/schemas/CompetitorScanRecluster' },
        type: 'array',
      },
      snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      started_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'scan_id',
      'product_id',
      'operation_id',
      'job_id',
      'status',
      'execution_mode',
      'provider',
      'country',
      'params',
      'quote',
    ],
    description:
      "A scan: ``queued`` -> ``running`` -> ``completed`` | ``partial`` (some\npages succeeded, some failed or ran out of credits) | ``failed`` (no page\nsucceeded) | ``uncertain`` (a page's outcome is unknown and not yet\nreconciled). ``stage`` is the last completed step. ``can_resume`` when\npages are left, a page awaits reconciliation, or a judgment step did not\nfinish (a ``completed`` scan whose AI key was refused mid-way, say).\n``waits`` counts how often it queued behind another market-data job on\nthe same key (spec §5.1). ``snapshot_id`` is the snapshot of the\nrevisions in effect as of the last freeze.",
  },
  get_competitor_scan_snapshot_200: {
    properties: {
      snapshot_id: { type: 'string' },
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      country: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      hash: { type: 'string' },
      cluster_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      current: { type: 'boolean', default: false },
      counts: { $ref: '#/components/schemas/CompetitorSnapshotCounts' },
      job_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      content: { additionalProperties: true, type: 'object' },
      created: { type: 'boolean', default: false },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'snapshot_id',
      'scan_id',
      'product_id',
      'country',
      'execution_mode',
      'hash',
    ],
    description:
      'A snapshot with its frozen ``content`` (``version``\n``competitor-snapshot/v1``: ``relevant_ad_ids``, ``judgments``,\n``cluster_revision``, ``claim_clusters``, ``unclustered_ad_ids``,\n``dimension_counts``, ``long_running_ad_ids``, ``adjacent_ad_ids``,\n``self_ad_ids``, ``ads``, ``params``). A refreeze answers ``created``\nfalse when the hash was already frozen.',
  },
  get_competitor_scan_summary_200: {
    properties: {
      snapshot_id: { type: 'string' },
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      country: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      hash: { type: 'string' },
      cluster_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      current: { type: 'boolean', default: false },
      counts: { $ref: '#/components/schemas/CompetitorSnapshotCounts' },
      job_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      mock: { type: 'boolean', default: false },
      language: {
        anyOf: [{ type: 'string', enum: ['en', 'zh-CN'] }, { type: 'null' }],
      },
      pending_changes: { type: 'boolean', default: false },
      can_recluster: { type: 'boolean', default: false },
      relevance_sources: {
        $ref: '#/components/schemas/CompetitorRelevanceSources',
      },
      clusters: {
        items: { $ref: '#/components/schemas/CompetitorClaimCluster' },
        type: 'array',
      },
      unclustered: {
        items: { $ref: '#/components/schemas/CompetitorSnapshotAd' },
        type: 'array',
      },
      adjacent: {
        items: { $ref: '#/components/schemas/CompetitorSnapshotAd' },
        type: 'array',
      },
      long_running: {
        items: { $ref: '#/components/schemas/CompetitorSnapshotAd' },
        type: 'array',
      },
      self_observations: {
        items: { $ref: '#/components/schemas/CompetitorSnapshotAd' },
        type: 'array',
      },
      long_running_days: { type: 'integer', default: 60 },
      dimension_counts: {
        items: { $ref: '#/components/schemas/CompetitorDimensionCount' },
        type: 'array',
      },
      conclusion: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorSnapshotConclusion' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'snapshot_id',
      'scan_id',
      'product_id',
      'country',
      'execution_mode',
      'hash',
    ],
    description:
      "The summary of one snapshot (spec §9): claim clusters, the relevant\nobservations no cluster holds (``unclustered``: flipped to relevant\nsince the last clustering), adjacent, long-running (``long_running_days``\nor more) and own-page observations, dimension counts and the AI's\nconclusion. ``pending_changes`` says a refreeze would make a new\nsnapshot; ``can_recluster`` that a recluster can be quoted now.",
  },
  get_cost_summary_200: {},
  get_experiment_200: {
    properties: {
      id: { type: 'string' },
      product_id: { type: 'string' },
      name: { type: 'string' },
      state: { type: 'string' },
      revision: { type: 'integer' },
      record_schema_version: { type: 'integer' },
      draft_context: { additionalProperties: true, type: 'object' },
      frozen_snapshot_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { type: 'string', format: 'date-time' },
      frozen_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      started_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      closed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      frozen_context: {
        anyOf: [
          { $ref: '#/components/schemas/ExperimentFrozenContext' },
          { type: 'null' },
        ],
      },
      entries: {
        items: { $ref: '#/components/schemas/ExperimentEntry' },
        type: 'array',
      },
      candidates: {
        items: { $ref: '#/components/schemas/ExperimentCandidate' },
        type: 'array',
      },
      judgements: {
        items: { $ref: '#/components/schemas/ExperimentJudgement' },
        type: 'array',
      },
      proposals: {
        items: { $ref: '#/components/schemas/ExperimentProposal' },
        type: 'array',
      },
      decisions: {
        items: { $ref: '#/components/schemas/ExperimentDecision' },
        type: 'array',
      },
      concept_resolution_evidence: {
        anyOf: [
          { $ref: '#/components/schemas/ConceptResolutionEvidence' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'id',
      'product_id',
      'name',
      'state',
      'revision',
      'record_schema_version',
      'created_at',
    ],
  },
  get_experiment_timeline_200: {
    properties: {
      head_sequence: { type: 'integer' },
      items: {
        items: { $ref: '#/components/schemas/ExperimentTimelineEvent' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['head_sequence'],
  },
  get_external_creative_200: {
    properties: {
      external_creative_id: { type: 'string' },
      product_id: { type: 'string' },
      status: { type: 'string' },
      current_step: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      execution_mode: { type: 'string' },
      filename: { type: 'string', default: '' },
      mime_type: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      byte_size: { type: 'integer', default: 0 },
      width: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      height: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      aspect_ratio: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      rights: { $ref: '#/components/schemas/RightsView' },
      asset_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      asset_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      asset_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      thumbnail: {
        anyOf: [
          { $ref: '#/components/schemas/ThumbnailLink' },
          { type: 'null' },
        ],
      },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/CreativeFailure' },
          { type: 'null' },
        ],
      },
      vocabulary: { $ref: '#/components/schemas/Vocabulary' },
      suggestion: {
        anyOf: [
          { $ref: '#/components/schemas/TagSuggestion' },
          { type: 'null' },
        ],
      },
      confirmation: {
        anyOf: [
          { $ref: '#/components/schemas/TagConfirmation' },
          { type: 'null' },
        ],
      },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'external_creative_id',
      'product_id',
      'status',
      'execution_mode',
      'rights',
      'vocabulary',
    ],
    description:
      '``status``: ``queued`` -> ``gating`` -> ``preparing_asset`` ->\n``tagging`` -> ``ready`` (suggestion available) -> ``confirmed``; or\n``rejected`` (upload gate), ``failed``, ``tagging_failed`` (the asset\nexists; tag it by hand). ``asset_code`` (``MK-XXXXXX``) is what a\nresults sheet matches.',
  },
  get_generation_asset_version_200: {
    properties: {
      id: { type: 'string' },
      asset_id: { type: 'string' },
      product_id: { type: 'string' },
      run_id: { type: 'string' },
      provider: { type: 'string' },
      model: { type: 'string' },
      status: { type: 'string' },
      kind: { type: 'string' },
      aspect_ratio: { type: 'string' },
      stable_dimension_signature: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      tags: { additionalProperties: true, type: 'object' },
      mime_type: { type: 'string' },
      byte_size: { type: 'integer' },
      content_sha256: { type: 'string' },
      origin_kind: { type: 'string' },
      execution_mode: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      mock_evidence: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      seed: { $ref: '#/components/schemas/AssetVersionSeed' },
      prompt: { type: 'string' },
      reference_sources: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      producing_run_item_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      provider_attempt_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reviews: {
        items: { $ref: '#/components/schemas/AssetReview' },
        type: 'array',
      },
      review_head_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      effective_review_value: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      qc_attempts: {
        items: { $ref: '#/components/schemas/AssetQCAttemptSummary' },
        type: 'array',
      },
      latest_qc_attempt_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      latest_qc_state: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      contextual_conclusions: { items: {}, type: 'array' },
      parent: {
        anyOf: [
          { $ref: '#/components/schemas/AssetVersionLineageEntry' },
          { type: 'null' },
        ],
      },
      children: {
        items: { $ref: '#/components/schemas/AssetVersionLineageEntry' },
        type: 'array',
      },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'id',
      'asset_id',
      'product_id',
      'run_id',
      'provider',
      'model',
      'status',
      'kind',
      'aspect_ratio',
      'mime_type',
      'byte_size',
      'content_sha256',
      'origin_kind',
      'seed',
      'prompt',
    ],
    description:
      "``GET /generation/asset-versions/{id}``\n(``asset_queries.get_asset_version_detail``, Issue #49 Task 4, spec\n§0.2). Extends ``AssetVersionSummary``'s fields with full provenance:\nseed, prompt, append-only ``reviews[]``, QC attempt history, and\nparent/children lineage. Browser Task 8's ``LightboxModal`` consumes\nthis.\n\nRaw object keys and committed storage-version identifiers are\ndeliberately never exposed to the browser -- downloads go through a\nsigned DownloadIntent, not a raw blob reference.\n``stable_dimension_signature`` and ``mock_evidence`` stay ``None`` --\nno backing column exists yet. ``contextual_conclusions`` (Task 6, spec\n§0.7) is every non-revoked, fully-anchored\n``ContextualExperimentConclusion`` for this exact AssetVersion, each\ncarrying its own full context. ``review_head_id``/\n``effective_review_value`` apply Task 5's supersession-aware head-review\nrule (spec §0.3) -- a ``cleared`` head still sets ``review_head_id`` but\nprojects ``effective_review_value`` to ``None``.",
  },
  get_generation_capabilities_200: {
    properties: {
      providers: { items: { type: 'string' }, type: 'array' },
      models: { additionalProperties: true, type: 'object' },
      modes: { items: { type: 'string' }, type: 'array' },
      format_limits: { additionalProperties: true, type: 'object' },
      credential_readiness: { additionalProperties: true, type: 'object' },
      llm: { additionalProperties: true, type: 'object' },
      provider_options: {
        items: { $ref: '#/components/schemas/GenerationProviderOption' },
        type: 'array',
      },
      products: {
        items: { $ref: '#/components/schemas/GenerationProductOption' },
        type: 'array',
      },
    },
    type: 'object',
    description: 'Workspace provider catalog and non-secret readiness.',
  },
  get_generation_defaults_200: {
    properties: {
      provider: { type: 'string' },
      model: { type: 'string' },
      execution_mode: { type: 'string' },
      aspect_ratio: { type: 'string' },
      variants: { additionalProperties: { type: 'integer' }, type: 'object' },
      source: { type: 'string' },
      revision: { type: 'integer' },
      credential_bound: { type: 'boolean' },
      warnings: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
    required: [
      'provider',
      'model',
      'execution_mode',
      'aspect_ratio',
      'variants',
      'source',
      'revision',
      'credential_bound',
    ],
    description:
      'Effective「生成默认值」. ``source`` is ``workspace`` once an Owner\nsaved them (``revision`` >= 1), else ``computed`` (``revision`` 0).\n``warnings``: ``mock_mode`` (show the bind-a-key banner),\n``byok_credential_missing``.',
  },
  get_generation_draft_200: {
    properties: {
      id: { type: 'string' },
      product_id: { type: 'string' },
      provider: { type: 'string' },
      model: { type: 'string' },
      execution_mode: { type: 'string' },
      state: { type: 'string' },
      revision: { type: 'integer' },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      selectors: { additionalProperties: true, type: 'object' },
      combination_strategy: { additionalProperties: true, type: 'object' },
      format_config: { additionalProperties: true, type: 'object' },
      exclusions: { additionalProperties: true, type: 'object' },
      regeneration_intent: { type: 'string', default: 'new_generation' },
      source_asset_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      source_recipe_identity: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'product_id',
      'provider',
      'model',
      'execution_mode',
      'state',
      'revision',
    ],
    description:
      '``GET /generation/drafts/{id}`` and the create/update/regenerate result.',
  },
  get_generation_export_manifest_200: {
    properties: {
      package_id: { type: 'string' },
      execution_mode: { type: 'string' },
      entries: {
        items: { $ref: '#/components/schemas/ExportManifestEntry' },
        type: 'array',
      },
      total_versions: { type: 'integer' },
    },
    type: 'object',
    required: ['package_id', 'execution_mode', 'total_versions'],
    description:
      '``GET /generation/exports/{id}/manifest`` (export.build_manifest).',
  },
  get_generation_export_package_200: {
    properties: {
      package_id: { type: 'string' },
      operation_id: { type: 'string' },
      execution_mode: { type: 'string' },
      state: { type: 'string' },
      revision: { type: 'integer' },
      byte_size: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      zip_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: [
      'package_id',
      'operation_id',
      'execution_mode',
      'state',
      'revision',
    ],
    description:
      "``GET /generation/exports/{id}`` result (export.get_export_package).\n\nRecovery/polling shape (Issue #49 Task 9): the browser's ``useExportFlow``\npolls this alongside the Operation to recover state across a refresh.\n``state`` here is the ExportPackage's own projection\n(``pending``/``building``/``ready``/``failed``/``expired``), unlike the\n202 response's Operation-state ``state``.",
  },
  get_generation_preview_200: {
    oneOf: [
      { $ref: '#/components/schemas/GenerationPreviewDetail' },
      { $ref: '#/components/schemas/GenerationRecoveryPreview' },
    ],
  },
  get_generation_run_200: {
    properties: {
      id: { type: 'string' },
      product_id: { type: 'string' },
      provider: { type: 'string' },
      model: { type: 'string' },
      status: { type: 'string' },
      total: { type: 'integer' },
      done: { type: 'integer' },
      failed: { type: 'integer' },
      execution_mode: { type: 'string' },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      revision: { type: 'integer' },
      recipe_snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      commands: {
        items: { $ref: '#/components/schemas/GenerationRunCommandSummary' },
        type: 'array',
      },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'product_id',
      'provider',
      'model',
      'status',
      'total',
      'done',
      'failed',
      'execution_mode',
      'revision',
    ],
    description: '``GET /generation/runs/{id}`` (run_queries.get_run_detail).',
  },
  get_generation_run_item_200: {
    properties: {
      id: { type: 'string' },
      ordinal: { type: 'integer' },
      state: { type: 'string' },
      combination_fingerprint: { type: 'string' },
      variant_index: { type: 'integer' },
      tags: { additionalProperties: true, type: 'object' },
      bound_asset_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      thumbnail: {
        anyOf: [
          { $ref: '#/components/schemas/AssetThumbnailLink' },
          { type: 'null' },
        ],
      },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
      combination_identity: { additionalProperties: true, type: 'object' },
      execution_mode: { type: 'string' },
      error_summary: { type: 'string', default: '' },
      error_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source_asset_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      source_run_item_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      intent_kind: { type: 'string', default: 'new_generation' },
      latest_attempt: {
        anyOf: [
          { $ref: '#/components/schemas/AttemptSummary' },
          { type: 'null' },
        ],
      },
      qc_result: {
        anyOf: [
          { $ref: '#/components/schemas/QcResultSummary' },
          { type: 'null' },
        ],
      },
      cost_evidence: {
        anyOf: [
          { $ref: '#/components/schemas/CostEvidence' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'id',
      'ordinal',
      'state',
      'combination_fingerprint',
      'variant_index',
      'execution_mode',
    ],
    description:
      '``GET /generation/runs/{id}/items/{item_id}`` (run_queries.get_run_item_detail).',
  },
  get_identity_probe_200: {
    properties: {
      workspace_id: { type: 'string' },
      role: { type: 'string', enum: ['owner', 'member'] },
      initiator_id: { type: 'string' },
      authorization_epoch: { type: 'integer' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['workspace_id', 'role', 'initiator_id', 'authorization_epoch'],
    description:
      'The verified TenantContext, echoed back for the tracer round trip.\n\nDeclared only so `generate_openapi.py` can extract a real response\nschema from `response_model` — the handler returns a `JSONResponse`\ndirectly, which FastAPI never re-validates against `response_model`, so\nthis model documents the contract without changing runtime behavior.',
  },
  get_library_command_outcome_200: {
    oneOf: [
      { $ref: '#/components/schemas/LibraryCommandPending' },
      { $ref: '#/components/schemas/LibraryCommandSucceeded' },
      { $ref: '#/components/schemas/LibraryCommandProblem' },
      { $ref: '#/components/schemas/LibraryCommandReconciliationRequired' },
      { $ref: '#/components/schemas/LibraryCommandNotApplied' },
    ],
  },
  get_library_data_source_connection_check_200: {
    properties: {
      check_id: { type: 'string' },
      data_source_entry_id: { type: 'string' },
      health: {
        type: 'string',
        enum: ['unknown', 'healthy', 'degraded', 'failed', 'revoked'],
      },
      reason_code: {
        type: 'string',
        description:
          'One of the codes in app.library.connection_checks.REASON_HEALTH.',
      },
      adapter_kind: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      adapter_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      execution_mode: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      data_source_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      credential_state: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
        description:
          'State of the bound CredentialVersion, or null when none is bound.',
      },
      checked_at: { type: 'string', format: 'date-time' },
    },
    type: 'object',
    required: [
      'check_id',
      'data_source_entry_id',
      'health',
      'reason_code',
      'adapter_kind',
      'adapter_version',
      'execution_mode',
      'data_source_version_id',
      'credential_state',
      'checked_at',
    ],
    description:
      "``GET /entries/{id}/connection-checks/{check_id}``: one check's evidence (#383).\n\nThe conclusion is ``health`` plus ``reason_code``; the Operation that ran\nthe check is ``succeeded`` whatever it found. ``execution_mode`` is the\nchecked version's, never the one a client asked for.",
  },
  get_library_data_source_credential_binding_200: {
    properties: {
      id: { type: 'string' },
      data_source_entry_id: { type: 'string' },
      credential_version_id: { type: 'string' },
      revision: { type: 'integer' },
      state: { type: 'string' },
    },
    type: 'object',
    required: [
      'id',
      'data_source_entry_id',
      'credential_version_id',
      'revision',
      'state',
    ],
    description:
      'Shape of ``datasources.bind_credential`` / ``get_active_binding``.',
  },
  get_library_entry_200: {
    properties: {
      id: { type: 'string' },
      entry_type: { type: 'string' },
      display_name: { type: 'string' },
      lifecycle: { type: 'string' },
      current_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      draft: {
        anyOf: [
          { $ref: '#/components/schemas/DraftSummary' },
          { type: 'null' },
        ],
      },
      versions: {
        items: { $ref: '#/components/schemas/LibraryVersionSummary' },
        type: 'array',
      },
      revision: { type: 'integer', default: 1 },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['id', 'entry_type', 'display_name', 'lifecycle'],
    description:
      "``GET /library/entries/{id}`` and the create/save/archive/fork/clone result.\n\n``revision`` tracks the entry's open Draft (defaults to 1 once archived\nand the Draft has been dropped) and is the value clients must echo back\nvia ``If-Match`` on the next mutating command.",
  },
  get_library_internal_entry_200: {
    properties: {
      id: { type: 'string' },
      entry_type: { type: 'string' },
      display_name: { type: 'string' },
      lifecycle: { type: 'string' },
      current_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      host_category: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      context_count: { type: 'integer', default: 0 },
    },
    type: 'object',
    required: ['id', 'entry_type', 'display_name', 'lifecycle'],
    description:
      '``GET /library/internal/{type}/{id}`` — unscoped canonical metadata.',
  },
  get_library_internal_entry_context_200: {
    properties: {
      id: { type: 'string' },
      entry_type: { type: 'string' },
      display_name: { type: 'string' },
      lifecycle: { type: 'string' },
      current_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      root_entry_id: { type: 'string' },
      root_entry_type: { type: 'string' },
      parent_entry_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      relation_kind: { type: 'string' },
      creation_evidence: { type: 'string', default: '' },
    },
    type: 'object',
    required: [
      'id',
      'entry_type',
      'display_name',
      'lifecycle',
      'root_entry_id',
      'root_entry_type',
      'relation_kind',
    ],
    description:
      '``GET /library/entries/{root}/internal/{type}/{id}`` — verified context.',
  },
  get_library_reference_object_200: {
    properties: {
      object_id: { type: 'string' },
      state: { type: 'string' },
      execution_mode: { type: 'string' },
      filename: { type: 'string', default: '' },
      declared_mime: { type: 'string', default: '' },
      mime_type: { type: 'string', default: '' },
      byte_size: { type: 'integer', default: 0 },
      width: { type: 'integer', default: 0 },
      height: { type: 'integer', default: 0 },
      content_sha256: { type: 'string', default: '' },
      checks: {
        items: { $ref: '#/components/schemas/ReferenceGateCheck' },
        type: 'array',
      },
      failure_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      failure_detail: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      content_safety_unavailable: { type: 'boolean', default: false },
      rights_declared: { type: 'boolean', default: false },
      promoted_entry_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      derivative: {
        anyOf: [
          { $ref: '#/components/schemas/ReferenceDerivativeFacts' },
          { type: 'null' },
        ],
      },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['object_id', 'state', 'execution_mode'],
    description:
      '``GET /library/reference-objects/{id}``: gate evidence for one object.\n\n``failure_code`` is the stable code the browser keys its copy on when the\ngate Operation failed (``content_safety_unavailable`` means the deployment\nselected ``real`` content safety without an adapter -- the object is still\n``quarantined``, not rejected). Object keys and signed URLs never appear\nhere.',
  },
  get_library_version_200: {
    properties: {
      id: { type: 'string' },
      entry_id: { type: 'string' },
      entry_type: { type: 'string' },
      version_no: { type: 'integer' },
      state: { type: 'string' },
      eligibility: { type: 'string' },
      content_schema_version: { type: 'integer', default: 1 },
      lifecycle_revision: { type: 'integer', default: 1 },
      content: { additionalProperties: true, type: 'object' },
      content_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      origin_command: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      base_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      published_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      published_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      withdraw_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'entry_id',
      'entry_type',
      'version_no',
      'state',
      'eligibility',
    ],
    description:
      '``GET /library/versions/{id}`` and the publish/withdraw command result.\n\nVersions are immutable once published (no draft-style revision counter),\nso there is no ``revision``/If-Match contract here -- ``version_no`` is\ninformational only. ``allowed_actions`` is server-computed.',
  },
  get_library_version_impact_200: {
    properties: {
      version_id: { type: 'string' },
      entry_id: { type: 'string' },
      entry_type: { type: 'string' },
      state: { type: 'string' },
      eligibility: { type: 'string' },
      dependents: {
        items: { $ref: '#/components/schemas/ImpactDependent' },
        type: 'array',
      },
      reason: { type: 'string', default: '' },
    },
    type: 'object',
    required: ['version_id', 'entry_id', 'entry_type', 'state', 'eligibility'],
    description:
      '``GET /library/versions/{id}/impact`` (service._impact_payload).',
  },
  get_localization_batch_200: {
    properties: {
      id: { type: 'string' },
      phase: { type: 'string' },
      preparation_stage: {
        anyOf: [
          {
            type: 'string',
            enum: ['analysis', 'truth', 'claim_review', 'copy', 'complete'],
          },
          { type: 'null' },
        ],
      },
      outcome: { type: 'string' },
      truth_freshness: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      truth_status: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      truth_valid_until: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      provider_execution_mode: { type: 'string' },
      evidence_tier: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source_count: { type: 'integer' },
      market_count: { type: 'integer' },
      item_count: { type: 'integer' },
      item_state_counts: {
        additionalProperties: { type: 'integer' },
        type: 'object',
      },
      revision: { type: 'integer' },
      created_at: { type: 'string' },
      updated_at: { type: 'string' },
      allowed_actions: {
        items: { $ref: '#/components/schemas/LocalizationAction' },
        type: 'array',
      },
      source_market_snapshot: { additionalProperties: true, type: 'object' },
      truth_package_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      truth_data_mode: { type: 'string' },
      max_attempts: { type: 'integer' },
      frozen_recipe: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      frozen_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      sources: {
        items: { $ref: '#/components/schemas/LocalizationBatchSource' },
        type: 'array',
      },
      markets: {
        items: { $ref: '#/components/schemas/LocalizationTruthMarket' },
        type: 'array',
      },
      copies: {
        items: { $ref: '#/components/schemas/LocalizedCopySummary' },
        type: 'array',
      },
      items: {
        items: { $ref: '#/components/schemas/LocalizationBatchItemSummary' },
        type: 'array',
      },
      commands: {
        items: { $ref: '#/components/schemas/LocalizationCommandSummary' },
        type: 'array',
      },
      inventories: {
        items: { $ref: '#/components/schemas/LocalizationSourceInventory' },
        type: 'array',
      },
      claim_decision_sets: {
        items: {
          $ref: '#/components/schemas/LocalizationClaimDecisionSetSummary',
        },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'phase',
      'outcome',
      'product_version_id',
      'offer_version_id',
      'provider_execution_mode',
      'source_count',
      'market_count',
      'item_count',
      'revision',
      'created_at',
      'updated_at',
      'truth_data_mode',
      'max_attempts',
    ],
  },
  get_localization_batch_outcome_200: {
    properties: {
      batch_id: { type: 'string' },
      phase: { type: 'string' },
      outcome: { type: 'string' },
      revision: { type: 'integer' },
      item_count: { type: 'integer' },
      item_state_counts: {
        additionalProperties: { type: 'integer' },
        type: 'object',
      },
    },
    type: 'object',
    required: ['batch_id', 'phase', 'outcome', 'revision', 'item_count'],
  },
  get_localization_item_200: {
    properties: {
      id: { type: 'string' },
      batch_id: { type: 'string' },
      state: { type: 'string' },
      target_market: { type: 'string' },
      source_asset_version_id: { type: 'string' },
      decision_id: { type: 'string' },
      truth_package_id: { type: 'string' },
      truth_package_market_id: { type: 'string' },
      market_policy_version_id: { type: 'string' },
      inventory_id: { type: 'string' },
      claim_decision_set_id: { type: 'string' },
      copy_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      attempt_count: { type: 'integer' },
      max_attempts: { type: 'integer' },
      reason_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      reason_detail: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      recoverable: { anyOf: [{ type: 'boolean' }, { type: 'null' }] },
      next_action: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      revision: { type: 'integer' },
      localized_copy: {
        anyOf: [
          { $ref: '#/components/schemas/LocalizedCopyDetail' },
          { type: 'null' },
        ],
      },
      attempts: {
        items: { $ref: '#/components/schemas/LocalizationAttemptSummary' },
        type: 'array',
      },
      cost: { $ref: '#/components/schemas/LocalizationItemCost' },
      binding: {
        anyOf: [
          { $ref: '#/components/schemas/LocalizationAssetBinding' },
          { type: 'null' },
        ],
      },
      lineage: { $ref: '#/components/schemas/LocalizationAssetLineage' },
      allowed_actions: {
        items: { $ref: '#/components/schemas/LocalizationAction' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'batch_id',
      'state',
      'target_market',
      'source_asset_version_id',
      'decision_id',
      'truth_package_id',
      'truth_package_market_id',
      'market_policy_version_id',
      'inventory_id',
      'claim_decision_set_id',
      'attempt_count',
      'max_attempts',
      'revision',
      'lineage',
    ],
  },
  get_localization_workspace_200: {
    properties: {
      role: { type: 'string' },
      eligible_sources: {
        items: { $ref: '#/components/schemas/EligibleLocalizationSource' },
        type: 'array',
      },
      markets: {
        items: { $ref: '#/components/schemas/LocalizationMarket' },
        type: 'array',
      },
      truth_resolution_policies: {
        items: {
          $ref: '#/components/schemas/LocalizationConfigurationVersion',
        },
        type: 'array',
      },
      data_sources: {
        items: {
          $ref: '#/components/schemas/LocalizationConfigurationVersion',
        },
        type: 'array',
      },
    },
    type: 'object',
    required: ['role'],
  },
  get_marketing_ai_qc_quote_200: {
    properties: {
      available: { type: 'boolean' },
      unavailable_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      provider: { type: 'string' },
      model: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      quantity: { type: 'integer' },
      maximum_cost: { type: 'string' },
      currency: { type: 'string', const: 'USD', default: 'USD' },
      quote_hash: { type: 'string' },
      asset_version_ids: { items: { type: 'string' }, type: 'array' },
      kind: {
        anyOf: [
          { type: 'string', enum: ['single', 'paired'] },
          { type: 'null' },
        ],
      },
      waiting: {
        anyOf: [
          {
            items: { $ref: '#/components/schemas/PairedQCWaiting' },
            type: 'array',
          },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'available',
      'unavailable_reason',
      'provider',
      'model',
      'execution_mode',
      'quantity',
      'maximum_cost',
      'quote_hash',
      'asset_version_ids',
    ],
  },
  get_marketing_capabilities_200: {
    properties: {
      paid: { $ref: '#/components/schemas/PlannerCapability' },
      mock_available: { type: 'boolean', const: true, default: true },
      media_currencies: {
        items: {
          type: 'string',
          enum: [
            'AED',
            'AUD',
            'BRL',
            'CAD',
            'CHF',
            'CNY',
            'EUR',
            'GBP',
            'HKD',
            'IDR',
            'INR',
            'JPY',
            'KRW',
            'MXN',
            'MYR',
            'NZD',
            'PHP',
            'PLN',
            'SAR',
            'SEK',
            'SGD',
            'THB',
            'TWD',
            'USD',
            'VND',
            'ZAR',
          ],
        },
        type: 'array',
      },
      attribution_windows: {
        items: {
          type: 'string',
          enum: [
            '1d_click',
            '7d_click',
            '1d_view',
            '1d_click_1d_view',
            '7d_click_1d_view',
          ],
        },
        type: 'array',
      },
      attribution_min_lag_days: {
        additionalProperties: { type: 'integer' },
        type: 'object',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['paid'],
  },
  get_marketing_evidence_handoff_200: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/EvidenceHandoffOptions' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  get_marketing_project_200: {
    properties: {
      id: { type: 'string' },
      revision: { type: 'integer' },
      state: {
        type: 'string',
        enum: [
          'input',
          'understanding_draft',
          'understanding_confirmed',
          'plan_draft',
          'plan_confirmed',
        ],
      },
      source_text: { type: 'string' },
      product_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      understanding: {
        anyOf: [
          { $ref: '#/components/schemas/Understanding' },
          { type: 'null' },
        ],
      },
      plan: {
        anyOf: [
          { $ref: '#/components/schemas/FrozenTestPlan' },
          { type: 'null' },
        ],
      },
      language_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      plan_review_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      brand_style_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      brand_kit: {
        anyOf: [
          { $ref: '#/components/schemas/BrandKitVersion' },
          { type: 'null' },
        ],
      },
      competitor_review_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      competitor_snapshot_stale: {
        anyOf: [{ type: 'boolean' }, { type: 'null' }],
      },
      competitor_market_unmatched: {
        anyOf: [{ type: 'boolean' }, { type: 'null' }],
      },
      evidence_status: {
        type: 'string',
        const: 'unvalidated',
        default: 'unvalidated',
      },
      latest_call: {
        anyOf: [
          { $ref: '#/components/schemas/PlanningCallSummary' },
          { type: 'null' },
        ],
      },
      generation_draft_ids: { items: { type: 'string' }, type: 'array' },
      product_references: {
        $ref: '#/components/schemas/ProductReferenceSelection',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'revision',
      'state',
      'source_text',
      'product_id',
      'understanding',
      'plan',
      'latest_call',
      'generation_draft_ids',
    ],
  },
  get_marketing_round_200: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  get_marketing_workflow_200: {
    properties: {
      project_id: { type: 'string' },
      rounds: {
        items: { $ref: '#/components/schemas/RoundSummary' },
        type: 'array',
      },
      current: {
        anyOf: [
          { $ref: '#/components/schemas/MarketingRound' },
          { type: 'null' },
        ],
      },
      next_action: { type: 'string' },
      poll_after_ms: { type: 'integer', minimum: 0, default: 0 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['project_id', 'rounds', 'current', 'next_action'],
  },
  get_meta_connection_200: {},
  get_meta_launch_preview_200: {
    properties: {
      id: { type: 'string' },
      workspace_id: { type: 'string' },
      meta_mode: { type: 'string' },
      meta_connection_id: { type: 'string' },
      meta_binding_id: { type: 'string' },
      experiment_id: { type: 'string' },
      experiment_version_id: { type: 'string' },
      candidate_count: { type: 'integer' },
      campaign_config: { additionalProperties: true, type: 'object' },
      daily_budget_minor: { type: 'integer' },
      total_daily_cost_minor: { type: 'integer' },
      currency: { type: 'string' },
      preview_hash: { type: 'string' },
      state: { type: 'string' },
      confirmed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      expires_at: { type: 'string', format: 'date-time' },
      created_at: { type: 'string', format: 'date-time' },
      candidates: {
        items: { $ref: '#/components/schemas/LaunchCandidateResponse' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'workspace_id',
      'meta_mode',
      'meta_connection_id',
      'meta_binding_id',
      'experiment_id',
      'experiment_version_id',
      'candidate_count',
      'daily_budget_minor',
      'total_daily_cost_minor',
      'currency',
      'preview_hash',
      'state',
      'expires_at',
      'created_at',
    ],
  },
  get_meta_launch_provisioning_200: {
    properties: {
      id: { type: 'string' },
      operation_id: { type: 'string' },
      operation_state: { type: 'string' },
      provisioning_state: { type: 'string' },
      activation_state: { type: 'string' },
      remote_campaign_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      fencing_token: { type: 'integer' },
      steps: {
        items: { $ref: '#/components/schemas/LaunchProvisioningStepResponse' },
        type: 'array',
      },
      parity_check: {
        anyOf: [
          { $ref: '#/components/schemas/LaunchParityCheckResponse' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'id',
      'operation_id',
      'operation_state',
      'provisioning_state',
      'activation_state',
      'fencing_token',
    ],
  },
  get_operation_200: {
    properties: {
      operation_id: { type: 'string' },
      command_type: { type: 'string' },
      state: {
        type: 'string',
        enum: [
          'admitted',
          'queued',
          'running',
          'partially_succeeded',
          'succeeded',
          'failed',
          'cancelled',
          'uncertain',
        ],
      },
      result: {
        anyOf: [
          { $ref: '#/components/schemas/OperationResultRefResponse' },
          { type: 'null' },
        ],
      },
      created_at: { type: 'string', format: 'date-time' },
      updated_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'operation_id',
      'command_type',
      'state',
      'result',
      'created_at',
      'updated_at',
    ],
    description:
      'Declared only so `generate_openapi.py` can extract a real response\nschema (see the identical note on `IdentityProbeResponse`); the handler\nreturns a `JSONResponse` directly, so this documents the contract\nwithout changing runtime behavior.',
  },
  get_product_brand_kit_200: {
    properties: {
      product_id: { type: 'string' },
      revision: { type: 'integer' },
      current: {
        anyOf: [
          { $ref: '#/components/schemas/BrandKitVersion' },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id', 'revision'],
  },
  get_product_competitor_settings_200: {
    properties: {
      product_id: { type: 'string' },
      competitor_snapshot_in_drafts: { type: 'boolean' },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['product_id', 'competitor_snapshot_in_drafts'],
    description:
      "Whether drafting cites the latest competitor snapshot of the plan's\nmarket (spec §7.2; on unless a member turned it off).",
  },
  get_product_intake_200: {
    properties: {
      intake_id: { type: 'string' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: { type: 'string' },
      current_step: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      execution_mode: { type: 'string' },
      input: { $ref: '#/components/schemas/IntakeInputSummary' },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/IntakeFailure' },
          { type: 'null' },
        ],
      },
      evidence: { additionalProperties: true, type: 'object' },
      card: {
        anyOf: [
          { $ref: '#/components/schemas/ProductCardDraft' },
          { type: 'null' },
        ],
      },
      confirmation: {
        anyOf: [
          { $ref: '#/components/schemas/ConfirmedProduct' },
          { type: 'null' },
        ],
      },
      product_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['intake_id', 'status', 'execution_mode', 'input'],
    description:
      '``status``: ``queued`` -> ``fetching`` -> ``extracting`` ->\n``preparing_images`` -> ``ready`` (card available) -> ``confirmed``;\nor ``fetch_failed`` / ``extraction_failed`` / ``failed`` with\n``failure``. ``evidence`` holds per-step outcome, duration and digests.',
  },
  get_product_learnings_200: {
    properties: {
      product_id: { type: 'string' },
      product_name: { type: 'string', default: '' },
      status: { type: 'string', enum: ['no_data', 'ready'] },
      evidence_mode: {
        anyOf: [
          { type: 'string', enum: ['production', 'sandbox', 'mock'] },
          { type: 'null' },
        ],
      },
      evidence_notice: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      available_modes: { items: { type: 'string' }, type: 'array' },
      snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      computed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      algorithm_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      thresholds: { additionalProperties: { type: 'string' }, type: 'object' },
      currency: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      highlights: { items: { type: 'string' }, type: 'array' },
      rounds: {
        items: { $ref: '#/components/schemas/RoundLearning' },
        type: 'array',
      },
      unattributable: {
        items: { $ref: '#/components/schemas/UnattributableAsset' },
        type: 'array',
      },
      can_seed_next_round: { type: 'boolean', default: false },
      review_signals: {
        anyOf: [
          { $ref: '#/components/schemas/ReviewSignals' },
          { type: 'null' },
        ],
      },
      recent_imports: {
        items: { $ref: '#/components/schemas/RecentImport' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['product_id', 'status'],
    description:
      '「这个产品我们已经学到了什么」. ``status`` ``no_data`` (show\n"尚无投放数据" and ``review_signals``) or ``ready``. ``evidence_mode``\nis the partition shown (production first); ``evidence_notice`` must be\ndisplayed when present (demo data). ``rounds`` newest first.',
  },
  get_product_references_200: {
    properties: {
      product_id: { type: 'string' },
      entry_id: { type: 'string' },
      version_id: { type: 'string' },
      revision: { type: 'integer' },
      model_variants: { items: { type: 'string' }, type: 'array' },
      has_unpublished_changes: { type: 'boolean', default: false },
      items: {
        items: { $ref: '#/components/schemas/ProductReferenceItem' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'product_id',
      'entry_id',
      'version_id',
      'revision',
      'model_variants',
      'items',
    ],
    description:
      "A projection of the product's immutable Library version/bindings.\n\n``revision`` is the If-Match value for edits. Deleted items remain in\nprior ProductVersions; ``version_id`` can be frozen by later consumers.",
  },
  get_results_import_200: {
    properties: {
      import_id: { type: 'string' },
      product_id: { type: 'string' },
      source: { type: 'string' },
      status: { type: 'string', enum: ['previewed', 'committed'] },
      file_name: { type: 'string', default: '' },
      currency: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      summary: { $ref: '#/components/schemas/ResultsImportSummary' },
      rows: {
        items: { $ref: '#/components/schemas/ResultsImportRow' },
        type: 'array',
      },
      rows_truncated: { type: 'boolean', default: false },
      snapshot_ids: { items: { type: 'string' }, type: 'array' },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      committed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      duplicate: { type: 'boolean', default: false },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['import_id', 'product_id', 'source', 'status', 'summary'],
    description:
      'A previewed (``previewed``) or committed (``committed``) import.\n``rows`` lists every rejected row and the first accepted ones\n(``rows_truncated``); ``duplicate`` is true when the same rows were\nalready imported for this product (the existing import is returned).',
  },
  get_results_template_200: {
    properties: {
      product_id: { type: 'string' },
      file_name: { type: 'string' },
      columns: {
        items: { $ref: '#/components/schemas/TemplateColumn' },
        type: 'array',
      },
      currency: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      rows: {
        items: { $ref: '#/components/schemas/TemplateRow' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['product_id', 'file_name', 'columns'],
    description:
      'The CSV template: ``columns`` in order and one row per image of the\nproduct (code + what it shows); the browser writes the file.',
  },
  get_scale_command_outcome_200: {
    properties: {
      identity_hash: { type: 'string' },
      command_type: { type: 'string' },
      dedup_key: { type: 'string' },
      result_resource_id: { type: 'string' },
      result_resource_type: { type: 'string' },
      request_fingerprint: { type: 'string' },
      initiator_id: { type: 'string' },
      created_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'identity_hash',
      'command_type',
      'dedup_key',
      'result_resource_id',
      'result_resource_type',
      'request_fingerprint',
      'initiator_id',
      'created_at',
    ],
  },
  get_scale_enrollment_200: {
    properties: {
      id: { type: 'string' },
      meta_mode: { type: 'string' },
      meta_connection_id: { type: 'string' },
      scenario_scope_key: { type: 'string' },
      ad_account_id: { type: 'string' },
      winner_decision_id: { type: 'string' },
      winner_confirmation_id: { type: 'string' },
      asset_version_id: { type: 'string' },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      market_code: { type: 'string' },
      landing_page_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      market_asset_binding_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      campaign_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ad_set_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ad_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      remote_binding_key: { type: 'string' },
      lifecycle: { type: 'string' },
      transition_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      current_episode_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      creator_id: { type: 'string' },
      original_initiator_id: { type: 'string' },
      revision: { type: 'integer' },
      evaluation_cancellation_generation: { type: 'integer' },
      created_at: { type: 'string', format: 'date-time' },
      updated_at: { type: 'string', format: 'date-time' },
      closed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibilityModel' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'meta_mode',
      'meta_connection_id',
      'scenario_scope_key',
      'ad_account_id',
      'winner_decision_id',
      'winner_confirmation_id',
      'asset_version_id',
      'product_version_id',
      'offer_version_id',
      'market_code',
      'landing_page_version_id',
      'market_asset_binding_id',
      'campaign_remote_id',
      'ad_set_remote_id',
      'ad_remote_id',
      'remote_binding_key',
      'lifecycle',
      'transition_reason',
      'current_episode_id',
      'creator_id',
      'original_initiator_id',
      'revision',
      'evaluation_cancellation_generation',
      'created_at',
      'updated_at',
      'closed_at',
      'allowed_actions',
    ],
  },
  get_scale_enrollment_breakdowns_200: {
    properties: {
      snapshot_id: { type: 'string' },
      episode_id: { type: 'string' },
      breakdown_type: { type: 'string' },
      window_start: { type: 'string', format: 'date' },
      window_end: { type: 'string', format: 'date' },
      safe_watermark: { type: 'string', format: 'date' },
      meta_mode: { type: 'string' },
      policy_version_id: { type: 'string' },
      target_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      segments: {
        items: { $ref: '#/components/schemas/BreakdownSegmentResponse' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'snapshot_id',
      'episode_id',
      'breakdown_type',
      'window_start',
      'window_end',
      'safe_watermark',
      'meta_mode',
      'policy_version_id',
      'target_version_id',
      'segments',
    ],
  },
  get_scale_enrollment_evidence_200: {
    properties: {
      enrollment_id: { type: 'string' },
      episode_id: { type: 'string' },
      evaluation_id: { type: 'string' },
      snapshot_id: { type: 'string' },
      policy_version_id: { type: 'string' },
      target_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      algorithm_version: { type: 'string' },
      metric_semantic_contract_set_hash: { type: 'string' },
      maturity: { type: 'string' },
      data_quality_state: { type: 'string' },
      health: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      health_reason: { type: 'string' },
      healthy_evaluations: { type: 'integer' },
      streak_counters: {
        items: { additionalProperties: true, type: 'object' },
        type: 'array',
      },
      safe_watermark: { type: 'string', format: 'date' },
      last_complete_local_day: { type: 'string', format: 'date' },
      last_sync_at: { type: 'string', format: 'date-time' },
      freshness_reference_at: { type: 'string', format: 'date-time' },
      complete_local_days_in_episode: { type: 'integer' },
      active_delivery_days_in_episode: { type: 'integer' },
      current_window: { $ref: '#/components/schemas/EvidenceWindowResponse' },
      baseline_window: { $ref: '#/components/schemas/EvidenceWindowResponse' },
      rolling_window: { $ref: '#/components/schemas/EvidenceWindowResponse' },
      quality_issues: {
        items: { $ref: '#/components/schemas/EvidenceQualityIssueResponse' },
        type: 'array',
      },
      signals: {
        items: { $ref: '#/components/schemas/EvidenceSignalResponse' },
        type: 'array',
      },
      cursor_as_of_local_day: { type: 'string', format: 'date' },
      cursor_source_revision: { type: 'integer' },
      cursor_semantic_event_sequence: { type: 'integer' },
      cursor_lineage_generation: { type: 'integer' },
      revision: { type: 'integer' },
      updated_at: { type: 'string', format: 'date-time' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'enrollment_id',
      'episode_id',
      'evaluation_id',
      'snapshot_id',
      'policy_version_id',
      'target_version_id',
      'algorithm_version',
      'metric_semantic_contract_set_hash',
      'maturity',
      'data_quality_state',
      'health',
      'health_reason',
      'healthy_evaluations',
      'streak_counters',
      'safe_watermark',
      'last_complete_local_day',
      'last_sync_at',
      'freshness_reference_at',
      'complete_local_days_in_episode',
      'active_delivery_days_in_episode',
      'current_window',
      'baseline_window',
      'rolling_window',
      'quality_issues',
      'signals',
      'cursor_as_of_local_day',
      'cursor_source_revision',
      'cursor_semantic_event_sequence',
      'cursor_lineage_generation',
      'revision',
      'updated_at',
    ],
    description:
      "Current evidence/readiness for an Enrollment's active Episode --\nthe accepted-Evaluation projection (spec §3.4 step 3/4), not a raw\nevidence snapshot. Reflects only a projection-eligible result that has\nalready won the structured cursor CAS.",
  },
  get_scale_episode_200: {
    properties: {
      id: { type: 'string' },
      enrollment_id: { type: 'string' },
      meta_mode: { type: 'string' },
      phase: { type: 'string' },
      started_at: { type: 'string', format: 'date-time' },
      started_local_day: { type: 'string', format: 'date' },
      ended_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      closure_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      delivery_evidence_hash: { type: 'string' },
      delivery_comparability_hash: { type: 'string' },
      metric_semantic_contract_set_id: { type: 'string' },
      timezone_name: { type: 'string' },
      currency: { type: 'string' },
      attribution_semantics_hash: { type: 'string' },
      revision: { type: 'integer' },
      projection_revision: { type: 'integer' },
      accepted_evaluation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibilityModel' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'enrollment_id',
      'meta_mode',
      'phase',
      'started_at',
      'started_local_day',
      'ended_at',
      'closure_reason',
      'delivery_evidence_hash',
      'delivery_comparability_hash',
      'metric_semantic_contract_set_id',
      'timezone_name',
      'currency',
      'attribution_semantics_hash',
      'revision',
      'projection_revision',
      'accepted_evaluation_id',
      'allowed_actions',
    ],
  },
  get_scale_target_200: {
    properties: {
      id: { type: 'string' },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      market_code: { type: 'string' },
      currency: { type: 'string' },
      target_cpa: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      breakeven_roas: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      minimum_economic_spend: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      lifecycle_state: { type: 'string' },
      creator_id: { type: 'string' },
      revision: { type: 'integer' },
      published_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      withdrawn_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      created_at: { type: 'string', format: 'date-time' },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibilityModel' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'product_version_id',
      'offer_version_id',
      'market_code',
      'currency',
      'target_cpa',
      'breakeven_roas',
      'minimum_economic_spend',
      'lifecycle_state',
      'creator_id',
      'revision',
      'published_at',
      'withdrawn_at',
      'created_at',
      'allowed_actions',
    ],
  },
  get_spend_guardrail_200: {},
  get_test_plan_200: {
    properties: {
      plan_id: { type: 'string' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: { type: 'string' },
      revision: { type: 'integer' },
      product_id: { type: 'string' },
      product_entry_id: { type: 'string' },
      product_version_id: { type: 'string' },
      product_name: { type: 'string' },
      goal: { type: 'string' },
      audience: { $ref: '#/components/schemas/AudienceView' },
      scale: { type: 'string' },
      seed_asset_version_ids: { items: { type: 'string' }, type: 'array' },
      learnings_basis: {
        anyOf: [
          { $ref: '#/components/schemas/LearningsBasis' },
          { type: 'null' },
        ],
      },
      failure: {
        anyOf: [{ $ref: '#/components/schemas/PlanFailure' }, { type: 'null' }],
      },
      factors: {
        items: { $ref: '#/components/schemas/FactorView' },
        type: 'array',
      },
      constraints: {
        items: { additionalProperties: true, type: 'object' },
        type: 'array',
      },
      directions: {
        items: { $ref: '#/components/schemas/DirectionView' },
        type: 'array',
      },
      stages: {
        additionalProperties: { $ref: '#/components/schemas/StageCounts' },
        type: 'object',
      },
      selected_stage: {
        anyOf: [{ $ref: '#/components/schemas/StageCounts' }, { type: 'null' }],
      },
      expansion: {
        anyOf: [{ $ref: '#/components/schemas/Expansion' }, { type: 'null' }],
      },
      defaults: { $ref: '#/components/schemas/GenerationDefaultsView' },
      runs: {
        items: { $ref: '#/components/schemas/TestPlanRunView' },
        type: 'array',
      },
      drafting: { additionalProperties: true, type: 'object' },
      planner_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      drafted_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'plan_id',
      'status',
      'revision',
      'product_id',
      'product_entry_id',
      'product_version_id',
      'product_name',
      'goal',
      'audience',
      'scale',
      'defaults',
    ],
    description:
      '``status``: ``drafting`` (poll ``operation_id``) -> ``ready`` or\n``failed``. ``stages`` has ``sample`` / ``small`` / ``full`` counts and\nmax cost; ``selected_stage`` is the plan\'s own scale; ``expansion`` is\nwhat "展开完整测试" would cost now; ``runs`` are the linked Runs.',
  },
  get_test_plan_results_200: {
    properties: {
      plan_id: { type: 'string' },
      product_id: { type: 'string' },
      status: { type: 'string', enum: ['no_data', 'ready'] },
      evidence_mode: {
        anyOf: [
          { type: 'string', enum: ['production', 'sandbox', 'mock'] },
          { type: 'null' },
        ],
      },
      evidence_notice: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      available_modes: { items: { type: 'string' }, type: 'array' },
      snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      computed_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      thresholds: { additionalProperties: { type: 'string' }, type: 'object' },
      round: {
        anyOf: [
          { $ref: '#/components/schemas/RoundLearning' },
          { type: 'null' },
        ],
      },
      can_seed_next_round: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['plan_id', 'product_id', 'status'],
    description:
      "One plan's round of the latest snapshot (directions with metrics).",
  },
  get_usage_200: {},
  import_library_manual_facts_202: {
    properties: {
      operation_id: { type: 'string' },
      state: { type: 'string', default: 'queued' },
      command_type: { type: 'string' },
    },
    type: 'object',
    required: ['operation_id', 'command_type'],
    description:
      '202 Accepted envelope for async domain commands (connection-checks, manual-fact-imports, reference-gate).',
  },
  import_library_manual_facts_request: {
    properties: {
      upload_intent_id: { type: 'string' },
      purpose: {
        type: 'string',
        maxLength: 100,
        minLength: 1,
        default: 'library_manual_fact_import',
      },
    },
    type: 'object',
    required: ['upload_intent_id'],
  },
  judge_experiment_200: {
    properties: {
      judgement: { $ref: '#/components/schemas/ExperimentJudgement' },
      proposal_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      proposal_state: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['judgement'],
  },
  judge_experiment_request: {
    properties: {
      expected_revision: { type: 'integer', minimum: 1 },
      as_of: { anyOf: [{ type: 'string', format: 'date' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['expected_revision'],
  },
  list_audit_events_200: { type: 'object', additionalProperties: true },
  list_competitor_ad_assessments_200: {
    properties: {
      scan_id: { type: 'string' },
      ad_id: { type: 'string' },
      kind: { type: 'string', const: 'relevance' },
      items: {
        items: { $ref: '#/components/schemas/CompetitorAdAssessment' },
        type: 'array',
      },
      current: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdAssessment' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['scan_id', 'ad_id', 'kind'],
    description:
      "An observation's relevance revisions, oldest first; ``current`` is the one in effect.",
  },
  list_competitor_ad_tags_200: {
    properties: {
      scan_id: { type: 'string' },
      ad_id: { type: 'string' },
      kind: { type: 'string', const: 'tags' },
      vocabulary: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorTagVocabulary' },
          { type: 'null' },
        ],
      },
      items: {
        items: { $ref: '#/components/schemas/CompetitorAdTagAssessment' },
        type: 'array',
      },
      current: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorAdTagAssessment' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: ['scan_id', 'ad_id', 'kind'],
    description:
      "An observation's tags revisions, oldest first; ``current`` is the one\nin effect. ``vocabulary`` is null until the tag step froze one.",
  },
  list_competitor_scan_ads_200: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/CompetitorAd' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      total: { type: 'integer', default: 0 },
    },
    type: 'object',
    description:
      'One page of observations, longest-running first; ``total`` counts every match.',
  },
  list_competitor_scan_discoveries_200: {
    properties: {
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      language: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: {
        type: 'string',
        enum: [
          'pending',
          'running',
          'completed',
          'partial',
          'failed',
          'skipped',
        ],
      },
      reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      advertisers: { type: 'integer', default: 0 },
      judged: { type: 'integer', default: 0 },
      recommended: { type: 'integer', default: 0 },
      unrelated: { type: 'integer', default: 0 },
      over_cap: { type: 'integer', default: 0 },
      items: {
        items: { $ref: '#/components/schemas/CompetitorDiscovery' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['scan_id', 'product_id', 'execution_mode', 'status'],
    description:
      "A scan's recommendations, most observations first. ``status`` and\n``reason`` are the discover step's, which runs last on a scan with\nkeywords (``skipped`` + ``no_keywords`` without any, ``no_candidates``\nwhen they found no advertiser outside the list; ``pending`` until it\nruns; ``partial`` or ``failed`` with ``calls_failed``,\n``ordinals_exhausted`` or the ``llm_chat`` code that stopped it, which a\nresume finishes). ``advertisers`` is\nthe frozen table (at most ``max_discover_advertisers``; ``over_cap``\nmore were left out), ``judged`` how many have an answer, ``unrelated``\nhow many were judged unrelated and are not listed.",
  },
  list_competitor_scan_snapshots_200: {
    properties: {
      scan_id: { type: 'string' },
      current_snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      items: {
        items: { $ref: '#/components/schemas/CompetitorScanSnapshotMeta' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['scan_id'],
    description: "A scan's snapshots, newest first.",
  },
  list_competitor_scans_200: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/CompetitorScanSummary' },
        type: 'array',
      },
    },
    type: 'object',
  },
  list_competitors_200: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/Competitor' },
        type: 'array',
      },
    },
    type: 'object',
  },
  list_credentials_200: {},
  list_experiments_200: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/ExperimentSummary' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
  },
  list_external_creatives_200: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/ExternalCreativeDetail' },
        type: 'array',
      },
    },
    type: 'object',
  },
  list_generation_assets_200: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/AssetVersionSummary' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      facets: { $ref: '#/components/schemas/AssetGalleryFacets' },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['facets'],
    description:
      '``GET /generation/assets`` (Issue #49 Task 3, spec §0.2). Replaces the\ngeneric ``Page[Any]`` envelope with a typed page carrying server-computed\nfacets and collection-level ``allowed_actions`` (export eligibility for\nthe current scope).',
  },
  list_generation_drafts_200: {
    properties: {
      items: { items: {}, type: 'array' },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['items'],
    description:
      'Generic ``Page[T]`` envelope (spec §2): ``{ items, next_cursor }``.',
  },
  list_generation_run_items_200: {
    properties: {
      items: { items: {}, type: 'array' },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['items'],
    description:
      'Generic ``Page[T]`` envelope (spec §2): ``{ items, next_cursor }``.',
  },
  list_generation_runs_200: {
    properties: {
      items: { items: {}, type: 'array' },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['items'],
    description:
      'Generic ``Page[T]`` envelope (spec §2): ``{ items, next_cursor }``.',
  },
  list_library_entries_200: {
    properties: {
      items: { items: {}, type: 'array' },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['items'],
    description:
      'Generic ``Page[T]`` envelope (spec §2): ``{ items, next_cursor }``.',
  },
  list_library_entry_events_200: {
    properties: {
      items: { items: {}, type: 'array' },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['items'],
    description:
      'Generic ``Page[T]`` envelope (spec §2): ``{ items, next_cursor }``.',
  },
  list_library_entry_types_200: {
    properties: { entry_types: { items: { type: 'string' }, type: 'array' } },
    type: 'object',
    required: ['entry_types'],
    description:
      '``GET /library/entry-types`` -- service.contracts.iter_entry_types() is a\nflat sorted list of type keys (e.g. "product", "person"), not objects.',
  },
  list_library_entry_versions_200: {
    properties: {
      items: { items: {}, type: 'array' },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['items'],
    description:
      'Generic ``Page[T]`` envelope (spec §2): ``{ items, next_cursor }``.',
  },
  list_library_internal_entries_200: {
    properties: {
      items: { items: {}, type: 'array' },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['items'],
    description:
      'Generic ``Page[T]`` envelope (spec §2): ``{ items, next_cursor }``.',
  },
  list_library_version_bindings_200: {
    properties: {
      items: { items: {}, type: 'array' },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['items'],
    description:
      'Generic ``Page[T]`` envelope (spec §2): ``{ items, next_cursor }``.',
  },
  list_localization_batches_200: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/LocalizationBatchSummary' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
  },
  list_marketing_brand_kits_200: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/BrandKitSummary' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['items'],
  },
  list_marketing_projects_200: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/MarketingProjectSummary' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['items'],
  },
  list_meta_bindings_200: {},
  list_meta_connections_200: {},
  list_meta_contracts_200: {},
  list_products_200: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/ProductSummary' },
        type: 'array',
      },
    },
    type: 'object',
  },
  list_scale_advice_threads_200: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/AdviceThreadResponse' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['items'],
  },
  list_scale_available_modes_200: {},
  list_scale_enrollment_candidates_200: {},
  list_scale_enrollments_200: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/EnrollmentResponse' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['items'],
  },
  list_scale_target_contexts_200: {},
  list_scale_targets_200: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/TargetResponse' },
        type: 'array',
      },
      next_cursor: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['items'],
  },
  list_test_plans_200: {
    properties: {
      items: {
        items: { $ref: '#/components/schemas/TestPlanSummary' },
        type: 'array',
      },
    },
    type: 'object',
  },
  pause_scale_enrollment_200: {
    properties: {
      id: { type: 'string' },
      meta_mode: { type: 'string' },
      meta_connection_id: { type: 'string' },
      scenario_scope_key: { type: 'string' },
      ad_account_id: { type: 'string' },
      winner_decision_id: { type: 'string' },
      winner_confirmation_id: { type: 'string' },
      asset_version_id: { type: 'string' },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      market_code: { type: 'string' },
      landing_page_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      market_asset_binding_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      campaign_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ad_set_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ad_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      remote_binding_key: { type: 'string' },
      lifecycle: { type: 'string' },
      transition_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      current_episode_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      creator_id: { type: 'string' },
      original_initiator_id: { type: 'string' },
      revision: { type: 'integer' },
      evaluation_cancellation_generation: { type: 'integer' },
      created_at: { type: 'string', format: 'date-time' },
      updated_at: { type: 'string', format: 'date-time' },
      closed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibilityModel' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'meta_mode',
      'meta_connection_id',
      'scenario_scope_key',
      'ad_account_id',
      'winner_decision_id',
      'winner_confirmation_id',
      'asset_version_id',
      'product_version_id',
      'offer_version_id',
      'market_code',
      'landing_page_version_id',
      'market_asset_binding_id',
      'campaign_remote_id',
      'ad_set_remote_id',
      'ad_remote_id',
      'remote_binding_key',
      'lifecycle',
      'transition_reason',
      'current_episode_id',
      'creator_id',
      'original_initiator_id',
      'revision',
      'evaluation_cancellation_generation',
      'created_at',
      'updated_at',
      'closed_at',
      'allowed_actions',
    ],
  },
  pause_scale_enrollment_request: {
    properties: { reason: { type: 'string', default: 'user_requested' } },
    additionalProperties: false,
    type: 'object',
  },
  prepare_marketing_feedback_201: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  prepare_marketing_feedback_request: {
    properties: {
      asset_version_id: { type: 'string', maxLength: 64, minLength: 1 },
      note: { type: 'string', maxLength: 1200, default: '' },
      provider: {
        type: 'string',
        enum: ['gemini', 'openai', 'seedream', 'mock'],
      },
      model: { type: 'string', maxLength: 200, minLength: 1 },
      image_tool: {
        anyOf: [
          { $ref: '#/components/schemas/ImageToolSpec' },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['asset_version_id', 'provider', 'model'],
  },
  prepare_marketing_generation_201: {
    properties: {
      id: { type: 'string' },
      revision: { type: 'integer' },
      state: {
        type: 'string',
        enum: [
          'input',
          'understanding_draft',
          'understanding_confirmed',
          'plan_draft',
          'plan_confirmed',
        ],
      },
      source_text: { type: 'string' },
      product_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      understanding: {
        anyOf: [
          { $ref: '#/components/schemas/Understanding' },
          { type: 'null' },
        ],
      },
      plan: {
        anyOf: [
          { $ref: '#/components/schemas/FrozenTestPlan' },
          { type: 'null' },
        ],
      },
      language_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      plan_review_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      brand_style_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      brand_kit: {
        anyOf: [
          { $ref: '#/components/schemas/BrandKitVersion' },
          { type: 'null' },
        ],
      },
      competitor_review_support: {
        anyOf: [{ type: 'string', const: 'v1' }, { type: 'null' }],
      },
      competitor_snapshot_stale: {
        anyOf: [{ type: 'boolean' }, { type: 'null' }],
      },
      competitor_market_unmatched: {
        anyOf: [{ type: 'boolean' }, { type: 'null' }],
      },
      evidence_status: {
        type: 'string',
        const: 'unvalidated',
        default: 'unvalidated',
      },
      latest_call: {
        anyOf: [
          { $ref: '#/components/schemas/PlanningCallSummary' },
          { type: 'null' },
        ],
      },
      generation_draft_ids: { items: { type: 'string' }, type: 'array' },
      product_references: {
        $ref: '#/components/schemas/ProductReferenceSelection',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'revision',
      'state',
      'source_text',
      'product_id',
      'understanding',
      'plan',
      'latest_call',
      'generation_draft_ids',
    ],
  },
  prepare_marketing_generation_request: {
    properties: {
      product_references: {
        $ref: '#/components/schemas/ProductReferenceSelection',
      },
      provider: {
        type: 'string',
        enum: ['gemini', 'openai', 'seedream', 'mock'],
      },
      model: { type: 'string', maxLength: 200, minLength: 1 },
      aspect_ratio: {
        type: 'string',
        enum: ['1:1', '4:5', '9:16', '16:9'],
        default: '4:5',
      },
      image_size: { type: 'string', enum: ['1K', '2K'], default: '1K' },
      variants: {
        anyOf: [{ type: 'integer', maximum: 4, minimum: 1 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['provider', 'model'],
  },
  preview_competitor_scan_200: {
    properties: {
      kind: { type: 'string', enum: ['scan', 'resume'] },
      scan_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      product_id: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      available_modes: {
        items: { type: 'string', enum: ['byok', 'mock'] },
        type: 'array',
      },
      byok_unavailable_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      byok_unavailable_params: { additionalProperties: true, type: 'object' },
      credentials: {
        items: { $ref: '#/components/schemas/CompetitorScanCredential' },
        type: 'array',
      },
      country: { type: 'string' },
      language: { type: 'string', enum: ['en', 'zh-CN'] },
      competitor_count: { type: 'integer' },
      keyword_count: { type: 'integer' },
      market_data: {
        $ref: '#/components/schemas/CompetitorScanMarketDataQuote',
      },
      llm: { $ref: '#/components/schemas/CompetitorScanLlmQuote' },
      max_usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      currency: { type: 'string', const: 'USD', default: 'USD' },
    },
    type: 'object',
    required: [
      'kind',
      'product_id',
      'execution_mode',
      'available_modes',
      'country',
      'language',
      'competitor_count',
      'keyword_count',
      'market_data',
      'llm',
      'max_usd',
    ],
    description:
      'The two-line maximum of a scan (``kind=scan``) or of what is left of\none (``kind=resume``: remaining pages, the unused refresh quota and the\nLLM steps without an answer). ``available_modes`` lists ``byok`` only\nwhen both keys are ready and prices are configured; otherwise\n``byok_unavailable_code`` (and its ``byok_unavailable_params``) says\nwhy. ``max_usd`` is the sum of both lines.',
  },
  preview_competitor_scan_request: {
    properties: {
      product_id: { type: 'string', maxLength: 64, minLength: 1 },
      competitor_ids: {
        items: { type: 'string', maxLength: 64, minLength: 1 },
        type: 'array',
        maxItems: 20,
        minItems: 1,
      },
      keywords: {
        items: { type: 'string', maxLength: 100, minLength: 1 },
        type: 'array',
        maxItems: 5,
      },
      country: { type: 'string', maxLength: 2, minLength: 2, default: 'US' },
      max_pages_per_competitor: {
        type: 'integer',
        maximum: 5,
        minimum: 1,
        default: 3,
      },
      keyword_pages: { type: 'integer', maximum: 5, minimum: 1, default: 2 },
      max_media_refreshes: {
        type: 'integer',
        maximum: 50,
        minimum: 0,
        default: 10,
      },
      max_tagged_ads: {
        type: 'integer',
        maximum: 200,
        minimum: 0,
        default: 60,
      },
      max_discover_advertisers: {
        type: 'integer',
        maximum: 100,
        minimum: 0,
        default: 30,
      },
      execution_mode: {
        anyOf: [{ type: 'string', enum: ['byok', 'mock'] }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id', 'competitor_ids'],
    description:
      "What to scan (spec §5.1). Every competitor must belong to the product,\nbe active and have a confirmed page; ``role=self`` competitors are\nscanned as the baseline. ``keywords`` feed competitor discovery (spec\n§8), ``keyword_pages`` pages each. ``max_media_refreshes`` is the quota\nof paid ``ad_refresh`` calls for media links that expired.\n``max_tagged_ads`` and ``max_discover_advertisers`` cap the LLM steps.\n``execution_mode`` defaults to the quote's default (``byok`` when both\nkeys are ready, else ``mock``). The AI output language is the\ninitiator's interface language, frozen into ``params.language``.",
  },
  preview_library_archive_impact_201: {
    properties: {
      impact_id: { type: 'string' },
      impact_hash: { type: 'string' },
      command_kind: { type: 'string' },
      target_entry_id: { type: 'string' },
      target_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      target_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      content_hash: { type: 'string', default: '' },
      expires_at: { type: 'string' },
      projection: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: [
      'impact_id',
      'impact_hash',
      'command_kind',
      'target_entry_id',
      'expires_at',
    ],
    description:
      'Response from ``POST .../publish-impact``, ``archive-impact``, ``withdraw-impact``.\n\nThe browser presents this to the user, then echoes ``impact_id`` +\n``impact_hash`` back to the matching command for atomic verification.',
  },
  preview_library_publish_impact_201: {
    properties: {
      impact_id: { type: 'string' },
      impact_hash: { type: 'string' },
      command_kind: { type: 'string' },
      target_entry_id: { type: 'string' },
      target_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      target_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      content_hash: { type: 'string', default: '' },
      expires_at: { type: 'string' },
      projection: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: [
      'impact_id',
      'impact_hash',
      'command_kind',
      'target_entry_id',
      'expires_at',
    ],
    description:
      'Response from ``POST .../publish-impact``, ``archive-impact``, ``withdraw-impact``.\n\nThe browser presents this to the user, then echoes ``impact_id`` +\n``impact_hash`` back to the matching command for atomic verification.',
  },
  preview_library_template_reapply_201: {
    properties: {
      base_template_version_id: { type: 'string' },
      new_template_version_id: { type: 'string' },
      schema_version_id: { type: 'string' },
      items: {
        items: { additionalProperties: true, type: 'object' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'base_template_version_id',
      'new_template_version_id',
      'schema_version_id',
    ],
    description:
      '``POST /library/versions/{id}/reapply-preview`` (templates.reapply_template).',
  },
  preview_library_template_reapply_request: {
    properties: { existing_schema_version_id: { type: 'string' } },
    type: 'object',
    required: ['existing_schema_version_id'],
  },
  preview_library_withdraw_impact_201: {
    properties: {
      impact_id: { type: 'string' },
      impact_hash: { type: 'string' },
      command_kind: { type: 'string' },
      target_entry_id: { type: 'string' },
      target_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      target_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      content_hash: { type: 'string', default: '' },
      expires_at: { type: 'string' },
      projection: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: [
      'impact_id',
      'impact_hash',
      'command_kind',
      'target_entry_id',
      'expires_at',
    ],
    description:
      'Response from ``POST .../publish-impact``, ``archive-impact``, ``withdraw-impact``.\n\nThe browser presents this to the user, then echoes ``impact_id`` +\n``impact_hash`` back to the matching command for atomic verification.',
  },
  preview_marketing_metrics_201: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  preview_marketing_metrics_request: {
    properties: {
      csv_text: { type: 'string', maxLength: 262144, minLength: 1 },
      filename: { type: 'string', maxLength: 160, minLength: 1 },
      ad_account_id: {
        type: 'string',
        maxLength: 64,
        minLength: 1,
        pattern: '^(act_)?[0-9]+$',
      },
      account_timezone: { type: 'string', maxLength: 64 },
      currency: {
        type: 'string',
        enum: [
          'AED',
          'AUD',
          'BRL',
          'CAD',
          'CHF',
          'CNY',
          'EUR',
          'GBP',
          'HKD',
          'IDR',
          'INR',
          'JPY',
          'KRW',
          'MXN',
          'MYR',
          'NZD',
          'PHP',
          'PLN',
          'SAR',
          'SEK',
          'SGD',
          'THB',
          'TWD',
          'USD',
          'VND',
          'ZAR',
        ],
      },
      attribution_window: {
        type: 'string',
        enum: [
          '1d_click',
          '7d_click',
          '1d_view',
          '1d_click_1d_view',
          '7d_click_1d_view',
        ],
      },
      source_mode: {
        type: 'string',
        enum: ['customer_file', 'controlled_mock'],
      },
      field_mapping: {
        additionalProperties: { type: 'string' },
        type: 'object',
        maxProperties: 10,
      },
      replaces_batch_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'csv_text',
      'filename',
      'ad_account_id',
      'account_timezone',
      'currency',
      'attribution_window',
      'source_mode',
    ],
  },
  preview_spend_guardrail_200: {},
  preview_spend_guardrail_request: {
    properties: {
      per_operation_limit: { type: 'string' },
      rolling_24h_limit: { type: 'string' },
      period_limit: { type: 'string' },
      outstanding_exposure_limit: { type: 'string' },
      expected_revision: { type: 'integer' },
      mfa_evidence_ref: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'per_operation_limit',
      'rolling_24h_limit',
      'period_limit',
      'outstanding_exposure_limit',
      'expected_revision',
    ],
  },
  promote_library_reference_object_201: {
    properties: {
      object_id: { type: 'string' },
      entry_id: { type: 'string' },
      version_id: { type: 'string' },
      version_no: { type: 'integer' },
      display_name: { type: 'string' },
      created: { type: 'boolean' },
      binding: {
        anyOf: [
          { $ref: '#/components/schemas/ReferencePromotionBinding' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'object_id',
      'entry_id',
      'version_id',
      'version_no',
      'display_name',
      'created',
    ],
    description:
      '201 response for ``POST /library/reference-objects/{id}/promote``.\n\n``created`` is false when the object had already been promoted: the same\nentry and its current effective version are returned instead of a second\npublication. ``binding`` describes the product/person Draft the version\nwas appended to when ``bind`` was requested.',
  },
  promote_library_reference_object_request: {
    properties: {
      display_name: {
        anyOf: [{ type: 'string', maxLength: 300 }, { type: 'null' }],
      },
      rights: {
        anyOf: [
          { $ref: '#/components/schemas/ReferenceRightsDeclarationInput' },
          { type: 'null' },
        ],
      },
      bind: {
        anyOf: [
          { $ref: '#/components/schemas/PromoteReferenceBindInput' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
  },
  publish_library_entry_201: {
    properties: {
      id: { type: 'string' },
      entry_id: { type: 'string' },
      entry_type: { type: 'string' },
      version_no: { type: 'integer' },
      state: { type: 'string' },
      eligibility: { type: 'string' },
      content_schema_version: { type: 'integer', default: 1 },
      lifecycle_revision: { type: 'integer', default: 1 },
      content: { additionalProperties: true, type: 'object' },
      content_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      origin_command: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      source_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      base_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      published_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      published_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      withdraw_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'entry_id',
      'entry_type',
      'version_no',
      'state',
      'eligibility',
    ],
    description:
      '``GET /library/versions/{id}`` and the publish/withdraw command result.\n\nVersions are immutable once published (no draft-style revision counter),\nso there is no ``revision``/If-Match contract here -- ``version_no`` is\ninformational only. ``allowed_actions`` is server-computed.',
  },
  publish_library_entry_request: {
    properties: {
      acknowledged_warnings: { items: { type: 'string' }, type: 'array' },
      impact_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      impact_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
  },
  quote_competitor_resolve_200: {
    properties: {
      competitor_id: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      available_modes: {
        items: { type: 'string', enum: ['byok', 'mock'] },
        type: 'array',
      },
      byok_unavailable_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      byok_unavailable_params: { additionalProperties: true, type: 'object' },
      provider: { type: 'string' },
      query: { type: 'string' },
      website_domain: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      country: { type: 'string' },
      max_credits: { type: 'integer' },
      max_usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      currency: { type: 'string', const: 'USD', default: 'USD' },
      pricing_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      lines: {
        items: { $ref: '#/components/schemas/CompetitorQuoteLine' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'competitor_id',
      'execution_mode',
      'available_modes',
      'provider',
      'query',
      'country',
      'max_credits',
      'max_usd',
    ],
    description:
      'The maximum a resolve can cost: ``page_search`` x 1 + ``probe`` x 3\n(fewer probes are made when fewer candidates qualify; the rest is\nreleased). ``max_usd`` is what the Workspace pays: ``"0"`` under\nControlled Mock, whose ``simulated_usd`` is an estimate only (null when\nthe deployment has no price table). ``available_modes`` lists ``byok``\nonly when a ``market_data`` key is bound and prices are configured;\notherwise ``byok_unavailable_code`` says why, with the values its copy\ninterpolates in ``byok_unavailable_params`` (the missing ``operations``\nof ``market_data_price_operation_missing``).',
  },
  quote_competitor_scan_recluster_200: {
    properties: {
      kind: { type: 'string', const: 'recluster' },
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      relevant_count: { type: 'integer' },
      llm: { $ref: '#/components/schemas/CompetitorScanLlmQuote' },
      max_usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      currency: { type: 'string', const: 'USD', default: 'USD' },
    },
    type: 'object',
    required: [
      'kind',
      'scan_id',
      'product_id',
      'execution_mode',
      'relevant_count',
      'llm',
      'max_usd',
    ],
    description:
      "What a recluster can cost at most: ``cluster:1`` at\n``quote_llm_chat``'s maximum, in the scan's own execution mode.",
  },
  quote_competitor_scan_resume_200: {
    properties: {
      kind: { type: 'string', enum: ['scan', 'resume'] },
      scan_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      product_id: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      available_modes: {
        items: { type: 'string', enum: ['byok', 'mock'] },
        type: 'array',
      },
      byok_unavailable_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      byok_unavailable_params: { additionalProperties: true, type: 'object' },
      credentials: {
        items: { $ref: '#/components/schemas/CompetitorScanCredential' },
        type: 'array',
      },
      country: { type: 'string' },
      language: { type: 'string', enum: ['en', 'zh-CN'] },
      competitor_count: { type: 'integer' },
      keyword_count: { type: 'integer' },
      market_data: {
        $ref: '#/components/schemas/CompetitorScanMarketDataQuote',
      },
      llm: { $ref: '#/components/schemas/CompetitorScanLlmQuote' },
      max_usd: { type: 'string' },
      simulated_usd: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      currency: { type: 'string', const: 'USD', default: 'USD' },
    },
    type: 'object',
    required: [
      'kind',
      'product_id',
      'execution_mode',
      'available_modes',
      'country',
      'language',
      'competitor_count',
      'keyword_count',
      'market_data',
      'llm',
      'max_usd',
    ],
    description:
      'The two-line maximum of a scan (``kind=scan``) or of what is left of\none (``kind=resume``: remaining pages, the unused refresh quota and the\nLLM steps without an answer). ``available_modes`` lists ``byok`` only\nwhen both keys are ready and prices are configured; otherwise\n``byok_unavailable_code`` (and its ``byok_unavailable_params``) says\nwhy. ``max_usd`` is the sum of both lines.',
  },
  recluster_competitor_scan_202: {
    properties: {
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      operation_id: { type: 'string' },
      job_id: { type: 'string' },
      status: {
        type: 'string',
        enum: [
          'queued',
          'running',
          'completed',
          'partial',
          'failed',
          'uncertain',
        ],
      },
      stage: {
        anyOf: [
          {
            type: 'string',
            enum: ['fetch', 'media', 'filter', 'tag', 'cluster', 'freeze'],
          },
          { type: 'null' },
        ],
      },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      provider: { type: 'string' },
      platform: { type: 'string', const: 'meta', default: 'meta' },
      country: { type: 'string' },
      params: { $ref: '#/components/schemas/CompetitorScanParams' },
      targets: {
        items: { $ref: '#/components/schemas/CompetitorScanTarget' },
        type: 'array',
      },
      steps: {
        items: { $ref: '#/components/schemas/CompetitorScanStepState' },
        type: 'array',
      },
      quote: { $ref: '#/components/schemas/CompetitorScanQuote' },
      cost: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorScanCost' },
          { type: 'null' },
        ],
      },
      initial_credential_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      resumes: {
        items: { $ref: '#/components/schemas/CompetitorScanResume' },
        type: 'array',
      },
      reconciliation: {
        items: { $ref: '#/components/schemas/CompetitorScanReconciliation' },
        type: 'array',
      },
      media: { $ref: '#/components/schemas/CompetitorScanMedia' },
      observation_count: { type: 'integer', default: 0 },
      waits: { type: 'integer', default: 0 },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorScanFailure' },
          { type: 'null' },
        ],
      },
      can_resume: { type: 'boolean', default: false },
      reclusters: {
        items: { $ref: '#/components/schemas/CompetitorScanRecluster' },
        type: 'array',
      },
      snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      started_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'scan_id',
      'product_id',
      'operation_id',
      'job_id',
      'status',
      'execution_mode',
      'provider',
      'country',
      'params',
      'quote',
    ],
    description:
      "A scan: ``queued`` -> ``running`` -> ``completed`` | ``partial`` (some\npages succeeded, some failed or ran out of credits) | ``failed`` (no page\nsucceeded) | ``uncertain`` (a page's outcome is unknown and not yet\nreconciled). ``stage`` is the last completed step. ``can_resume`` when\npages are left, a page awaits reconciliation, or a judgment step did not\nfinish (a ``completed`` scan whose AI key was refused mid-way, say).\n``waits`` counts how often it queued behind another market-data job on\nthe same key (spec §5.1). ``snapshot_id`` is the snapshot of the\nrevisions in effect as of the last freeze.",
  },
  reconcile_competitor_scan_200: {
    properties: {
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      operation_id: { type: 'string' },
      job_id: { type: 'string' },
      status: {
        type: 'string',
        enum: [
          'queued',
          'running',
          'completed',
          'partial',
          'failed',
          'uncertain',
        ],
      },
      stage: {
        anyOf: [
          {
            type: 'string',
            enum: ['fetch', 'media', 'filter', 'tag', 'cluster', 'freeze'],
          },
          { type: 'null' },
        ],
      },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      provider: { type: 'string' },
      platform: { type: 'string', const: 'meta', default: 'meta' },
      country: { type: 'string' },
      params: { $ref: '#/components/schemas/CompetitorScanParams' },
      targets: {
        items: { $ref: '#/components/schemas/CompetitorScanTarget' },
        type: 'array',
      },
      steps: {
        items: { $ref: '#/components/schemas/CompetitorScanStepState' },
        type: 'array',
      },
      quote: { $ref: '#/components/schemas/CompetitorScanQuote' },
      cost: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorScanCost' },
          { type: 'null' },
        ],
      },
      initial_credential_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      resumes: {
        items: { $ref: '#/components/schemas/CompetitorScanResume' },
        type: 'array',
      },
      reconciliation: {
        items: { $ref: '#/components/schemas/CompetitorScanReconciliation' },
        type: 'array',
      },
      media: { $ref: '#/components/schemas/CompetitorScanMedia' },
      observation_count: { type: 'integer', default: 0 },
      waits: { type: 'integer', default: 0 },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorScanFailure' },
          { type: 'null' },
        ],
      },
      can_resume: { type: 'boolean', default: false },
      reclusters: {
        items: { $ref: '#/components/schemas/CompetitorScanRecluster' },
        type: 'array',
      },
      snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      started_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'scan_id',
      'product_id',
      'operation_id',
      'job_id',
      'status',
      'execution_mode',
      'provider',
      'country',
      'params',
      'quote',
    ],
    description:
      "A scan: ``queued`` -> ``running`` -> ``completed`` | ``partial`` (some\npages succeeded, some failed or ran out of credits) | ``failed`` (no page\nsucceeded) | ``uncertain`` (a page's outcome is unknown and not yet\nreconciled). ``stage`` is the last completed step. ``can_resume`` when\npages are left, a page awaits reconciliation, or a judgment step did not\nfinish (a ``completed`` scan whose AI key was refused mid-way, say).\n``waits`` counts how often it queued behind another market-data job on\nthe same key (spec §5.1). ``snapshot_id`` is the snapshot of the\nrevisions in effect as of the last freeze.",
  },
  reconcile_competitor_scan_request: {
    properties: {
      attempt_id: { type: 'string', maxLength: 64, minLength: 1 },
      outcome: { type: 'string', enum: ['charged', 'not_charged'] },
    },
    additionalProperties: false,
    type: 'object',
    required: ['attempt_id', 'outcome'],
    description:
      "The Owner's verdict on one pending page: did the provider charge it?",
  },
  reconcile_generation_run_200: {
    properties: {
      operation_id: { type: 'string' },
      run_id: { type: 'string' },
    },
    type: 'object',
    required: ['operation_id', 'run_id'],
    description:
      'Result of a Run mutation command (cancel/retry/resume/reconcile).',
  },
  reconcile_generation_run_request: {
    properties: {
      uncertain_targets: { items: { type: 'string' }, type: 'array' },
      operator_note: { type: 'string', default: '' },
    },
    type: 'object',
  },
  reconcile_library_command_outcome_200: {
    oneOf: [
      { $ref: '#/components/schemas/LibraryCommandPending' },
      { $ref: '#/components/schemas/LibraryCommandSucceeded' },
      { $ref: '#/components/schemas/LibraryCommandProblem' },
      { $ref: '#/components/schemas/LibraryCommandReconciliationRequired' },
      { $ref: '#/components/schemas/LibraryCommandNotApplied' },
    ],
  },
  recover_meta_launch_operation_202: {
    properties: {
      id: { type: 'string' },
      original_operation_id: { type: 'string' },
      recovery_operation_id: { type: 'string' },
      state: { type: 'string' },
      resolution: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['id', 'original_operation_id', 'recovery_operation_id', 'state'],
  },
  recover_meta_launch_operation_request: {
    properties: { meta_mode: { type: 'string' } },
    type: 'object',
    required: ['meta_mode'],
    description:
      'Body for POST routes that otherwise carry no payload but still need\nto bind the request to a Meta mode (see `_effective_context` below).',
  },
  refreeze_competitor_scan_200: {
    properties: {
      snapshot_id: { type: 'string' },
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      country: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      hash: { type: 'string' },
      cluster_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      current: { type: 'boolean', default: false },
      counts: { $ref: '#/components/schemas/CompetitorSnapshotCounts' },
      job_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      content: { additionalProperties: true, type: 'object' },
      created: { type: 'boolean', default: false },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'snapshot_id',
      'scan_id',
      'product_id',
      'country',
      'execution_mode',
      'hash',
    ],
    description:
      'A snapshot with its frozen ``content`` (``version``\n``competitor-snapshot/v1``: ``relevant_ad_ids``, ``judgments``,\n``cluster_revision``, ``claim_clusters``, ``unclustered_ad_ids``,\n``dimension_counts``, ``long_running_ad_ids``, ``adjacent_ad_ids``,\n``self_ad_ids``, ``ads``, ``params``). A refreeze answers ``created``\nfalse when the hash was already frozen.',
  },
  resolve_competitor_202: {
    properties: {
      competitor_id: { type: 'string' },
      resolve_job_id: { type: 'string' },
      operation_id: { type: 'string' },
      status: {
        type: 'string',
        enum: ['queued', 'running', 'completed', 'failed', 'uncertain'],
      },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      provider: { type: 'string' },
      query: { type: 'string' },
      website_domain: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      country: { type: 'string' },
      quote: { $ref: '#/components/schemas/CompetitorResolveQuote' },
      cost: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorResolveCost' },
          { type: 'null' },
        ],
      },
      candidates: {
        items: { $ref: '#/components/schemas/CompetitorCandidate' },
        type: 'array',
      },
      auto_confirmed: { type: 'boolean', default: false },
      confirmation_revision: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      waits: { type: 'integer', default: 0 },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorResolveFailure' },
          { type: 'null' },
        ],
      },
      reusable: { type: 'boolean', default: false },
      expires_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      started_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'competitor_id',
      'resolve_job_id',
      'operation_id',
      'status',
      'execution_mode',
      'provider',
      'query',
      'country',
      'quote',
    ],
    description:
      "A resolve: ``queued`` -> ``running`` -> ``completed``; ``failed``\n(the search failed, or a 402/401/403 stopped the probes -- candidates\nalready found stay listed) or ``uncertain`` (a call's outcome is\nunknown; nothing is resent). ``waits`` counts how often it queued\nbehind another market-data job on the same key (spec §5.1). A finished\nresolve is ``reusable`` for confirmation until ``expires_at`` (72 hours).\n``auto_confirmed`` is true when it confirmed the page itself (exact name\nand domain hit, or the alias of a pasted page link).",
  },
  resolve_competitor_request: {
    properties: {
      execution_mode: {
        anyOf: [{ type: 'string', enum: ['byok', 'mock'] }, { type: 'null' }],
      },
      country: { type: 'string', maxLength: 2, minLength: 2, default: 'US' },
    },
    additionalProperties: false,
    type: 'object',
    description:
      "Start a resolve. ``execution_mode`` defaults to the quote's default\n(``byok`` when available, else ``mock``); ``country`` (ISO 3166-1\nalpha-2) is the market the probes read active ads in.",
  },
  resolve_marketing_call_202: {
    properties: {
      id: { type: 'string' },
      operation_id: { type: 'string' },
      call_id: { type: 'string' },
      state: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'operation_id', 'call_id', 'state'],
  },
  resolve_marketing_call_request: {
    properties: {
      kind: { type: 'string', enum: ['planning', 'ai_qc'] },
      maximum_cost: {
        anyOf: [
          { type: 'number', minimum: 0 },
          {
            type: 'string',
            pattern:
              '^(?!^[-+.]*$)[+-]?0*(?:\\d{0,8}|(?=[\\d.]{1,15}0*$)\\d{0,8}\\.\\d{0,6}0*$)',
          },
        ],
      },
      acknowledge_estimated_cost: { type: 'boolean', const: true },
      note: { type: 'string', maxLength: 2000, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['kind', 'maximum_cost', 'acknowledge_estimated_cost', 'note'],
  },
  resume_competitor_scan_202: {
    properties: {
      scan_id: { type: 'string' },
      product_id: { type: 'string' },
      operation_id: { type: 'string' },
      job_id: { type: 'string' },
      status: {
        type: 'string',
        enum: [
          'queued',
          'running',
          'completed',
          'partial',
          'failed',
          'uncertain',
        ],
      },
      stage: {
        anyOf: [
          {
            type: 'string',
            enum: ['fetch', 'media', 'filter', 'tag', 'cluster', 'freeze'],
          },
          { type: 'null' },
        ],
      },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      provider: { type: 'string' },
      platform: { type: 'string', const: 'meta', default: 'meta' },
      country: { type: 'string' },
      params: { $ref: '#/components/schemas/CompetitorScanParams' },
      targets: {
        items: { $ref: '#/components/schemas/CompetitorScanTarget' },
        type: 'array',
      },
      steps: {
        items: { $ref: '#/components/schemas/CompetitorScanStepState' },
        type: 'array',
      },
      quote: { $ref: '#/components/schemas/CompetitorScanQuote' },
      cost: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorScanCost' },
          { type: 'null' },
        ],
      },
      initial_credential_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      resumes: {
        items: { $ref: '#/components/schemas/CompetitorScanResume' },
        type: 'array',
      },
      reconciliation: {
        items: { $ref: '#/components/schemas/CompetitorScanReconciliation' },
        type: 'array',
      },
      media: { $ref: '#/components/schemas/CompetitorScanMedia' },
      observation_count: { type: 'integer', default: 0 },
      waits: { type: 'integer', default: 0 },
      failure: {
        anyOf: [
          { $ref: '#/components/schemas/CompetitorScanFailure' },
          { type: 'null' },
        ],
      },
      can_resume: { type: 'boolean', default: false },
      reclusters: {
        items: { $ref: '#/components/schemas/CompetitorScanRecluster' },
        type: 'array',
      },
      snapshot_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      started_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      finished_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'scan_id',
      'product_id',
      'operation_id',
      'job_id',
      'status',
      'execution_mode',
      'provider',
      'country',
      'params',
      'quote',
    ],
    description:
      "A scan: ``queued`` -> ``running`` -> ``completed`` | ``partial`` (some\npages succeeded, some failed or ran out of credits) | ``failed`` (no page\nsucceeded) | ``uncertain`` (a page's outcome is unknown and not yet\nreconciled). ``stage`` is the last completed step. ``can_resume`` when\npages are left, a page awaits reconciliation, or a judgment step did not\nfinish (a ``completed`` scan whose AI key was refused mid-way, say).\n``waits`` counts how often it queued behind another market-data job on\nthe same key (spec §5.1). ``snapshot_id`` is the snapshot of the\nrevisions in effect as of the last freeze.",
  },
  resume_generation_run_paused_items_200: {
    properties: {
      operation_id: { type: 'string' },
      run_id: { type: 'string' },
    },
    type: 'object',
    required: ['operation_id', 'run_id'],
    description:
      'Result of a Run mutation command (cancel/retry/resume/reconcile).',
  },
  resume_generation_run_paused_items_request: {
    properties: {
      preview_id: { type: 'string', minLength: 1 },
      preview_hash: { type: 'string', minLength: 1 },
    },
    type: 'object',
    required: ['preview_id', 'preview_hash'],
  },
  resume_localization_item_200: {
    properties: {
      item_id: { type: 'string' },
      state: { type: 'string' },
      replayed: { type: 'boolean' },
    },
    type: 'object',
    required: ['item_id', 'state', 'replayed'],
  },
  resume_scale_enrollment_200: {
    properties: {
      id: { type: 'string' },
      meta_mode: { type: 'string' },
      meta_connection_id: { type: 'string' },
      scenario_scope_key: { type: 'string' },
      ad_account_id: { type: 'string' },
      winner_decision_id: { type: 'string' },
      winner_confirmation_id: { type: 'string' },
      asset_version_id: { type: 'string' },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      market_code: { type: 'string' },
      landing_page_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      market_asset_binding_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      campaign_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ad_set_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      ad_remote_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      remote_binding_key: { type: 'string' },
      lifecycle: { type: 'string' },
      transition_reason: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      current_episode_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      creator_id: { type: 'string' },
      original_initiator_id: { type: 'string' },
      revision: { type: 'integer' },
      evaluation_cancellation_generation: { type: 'integer' },
      created_at: { type: 'string', format: 'date-time' },
      updated_at: { type: 'string', format: 'date-time' },
      closed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibilityModel' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'meta_mode',
      'meta_connection_id',
      'scenario_scope_key',
      'ad_account_id',
      'winner_decision_id',
      'winner_confirmation_id',
      'asset_version_id',
      'product_version_id',
      'offer_version_id',
      'market_code',
      'landing_page_version_id',
      'market_asset_binding_id',
      'campaign_remote_id',
      'ad_set_remote_id',
      'ad_remote_id',
      'remote_binding_key',
      'lifecycle',
      'transition_reason',
      'current_episode_id',
      'creator_id',
      'original_initiator_id',
      'revision',
      'evaluation_cancellation_generation',
      'created_at',
      'updated_at',
      'closed_at',
      'allowed_actions',
    ],
  },
  resume_scale_enrollment_request: {
    properties: {
      activation_preview_id: { type: 'string', maxLength: 64, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['activation_preview_id'],
    description:
      'Issue #86 Task 11: same preview-only shape as\n``EnrollmentActivateRequest`` -- resume cannot bypass the preview\nprotocol (spec §5.3), even when it will end up continuing an\nalready-open Episode untouched.',
  },
  retire_meta_connection_200: {},
  retire_meta_connection_request: {
    properties: { meta_mode: { type: 'string' } },
    type: 'object',
    required: ['meta_mode'],
    description:
      'Body for POST routes that otherwise carry no payload but still need\nto bind the request to a Meta mode (see `_effective_context` below).',
  },
  retry_generation_run_failed_items_200: {
    properties: {
      operation_id: { type: 'string' },
      run_id: { type: 'string' },
    },
    type: 'object',
    required: ['operation_id', 'run_id'],
    description:
      'Result of a Run mutation command (cancel/retry/resume/reconcile).',
  },
  retry_generation_run_failed_items_request: {
    properties: {
      preview_id: { type: 'string', minLength: 1 },
      preview_hash: { type: 'string', minLength: 1 },
    },
    type: 'object',
    required: ['preview_id', 'preview_hash'],
  },
  retry_localization_item_200: {
    properties: {
      item_id: { type: 'string' },
      attempt_number: { type: 'integer' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean' },
    },
    type: 'object',
    required: ['item_id', 'attempt_number', 'replayed'],
  },
  retry_marketing_handoff_202: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  reuse_marketing_creative_201: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  reuse_marketing_creative_request: {
    properties: {
      asset_version_id: { type: 'string', maxLength: 64, minLength: 1 },
      combination_id: { type: 'string', maxLength: 64, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['asset_version_id', 'combination_id'],
  },
  review_marketing_creative_201: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  review_marketing_creative_request: {
    properties: {
      ai_qc_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
      paired_ai_qc_id: {
        anyOf: [{ type: 'string', maxLength: 64 }, { type: 'null' }],
      },
      override_note: { type: 'string', maxLength: 2000, default: '' },
      asset_version_id: { type: 'string', maxLength: 64, minLength: 1 },
      quality: {
        type: 'string',
        enum: ['approved', 'needs_changes', 'rejected'],
      },
      checks: {
        additionalProperties: {
          type: 'string',
          enum: ['pass', 'fail', 'unknown'],
        },
        type: 'object',
        maxProperties: 30,
      },
      note: { type: 'string', maxLength: 2000, minLength: 1 },
      compared_asset_version_ids: {
        items: { type: 'string' },
        type: 'array',
        maxItems: 20,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['asset_version_id', 'quality', 'checks', 'note'],
  },
  revise_test_plan_200: {
    properties: {
      plan_id: { type: 'string' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      status: { type: 'string' },
      revision: { type: 'integer' },
      product_id: { type: 'string' },
      product_entry_id: { type: 'string' },
      product_version_id: { type: 'string' },
      product_name: { type: 'string' },
      goal: { type: 'string' },
      audience: { $ref: '#/components/schemas/AudienceView' },
      scale: { type: 'string' },
      seed_asset_version_ids: { items: { type: 'string' }, type: 'array' },
      learnings_basis: {
        anyOf: [
          { $ref: '#/components/schemas/LearningsBasis' },
          { type: 'null' },
        ],
      },
      failure: {
        anyOf: [{ $ref: '#/components/schemas/PlanFailure' }, { type: 'null' }],
      },
      factors: {
        items: { $ref: '#/components/schemas/FactorView' },
        type: 'array',
      },
      constraints: {
        items: { additionalProperties: true, type: 'object' },
        type: 'array',
      },
      directions: {
        items: { $ref: '#/components/schemas/DirectionView' },
        type: 'array',
      },
      stages: {
        additionalProperties: { $ref: '#/components/schemas/StageCounts' },
        type: 'object',
      },
      selected_stage: {
        anyOf: [{ $ref: '#/components/schemas/StageCounts' }, { type: 'null' }],
      },
      expansion: {
        anyOf: [{ $ref: '#/components/schemas/Expansion' }, { type: 'null' }],
      },
      defaults: { $ref: '#/components/schemas/GenerationDefaultsView' },
      runs: {
        items: { $ref: '#/components/schemas/TestPlanRunView' },
        type: 'array',
      },
      drafting: { additionalProperties: true, type: 'object' },
      planner_version: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_by: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      drafted_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: [
      'plan_id',
      'status',
      'revision',
      'product_id',
      'product_entry_id',
      'product_version_id',
      'product_name',
      'goal',
      'audience',
      'scale',
      'defaults',
    ],
    description:
      '``status``: ``drafting`` (poll ``operation_id``) -> ``ready`` or\n``failed``. ``stages`` has ``sample`` / ``small`` / ``full`` counts and\nmax cost; ``selected_stage`` is the plan\'s own scale; ``expansion`` is\nwhat "展开完整测试" would cost now; ``runs`` are the linked Runs.',
  },
  revise_test_plan_request: {
    properties: {
      remove: { items: { type: 'string' }, type: 'array', maxItems: 8 },
      restore: { items: { type: 'string' }, type: 'array', maxItems: 8 },
      edit: {
        items: { $ref: '#/components/schemas/DirectionEdit' },
        type: 'array',
        maxItems: 8,
      },
      add: {
        items: { $ref: '#/components/schemas/DirectionAdd' },
        type: 'array',
        maxItems: 8,
      },
      scale: {
        anyOf: [
          { type: 'string', enum: ['sample', 'small', 'full'] },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    description:
      'Remove / restore / edit / add directions and optionally change the\nscale; the plan is re-planned and its counts and cost updated.',
  },
  revoke_credential_200: {},
  revoke_credential_request: {
    properties: { reason: { type: 'string' } },
    additionalProperties: false,
    type: 'object',
    required: ['reason'],
  },
  rotate_credential_200: {},
  rotate_credential_request: {
    properties: { new_plaintext_b64: { type: 'string' } },
    additionalProperties: false,
    type: 'object',
    required: ['new_plaintext_b64'],
  },
  save_library_draft_200: {
    properties: {
      id: { type: 'string' },
      entry_type: { type: 'string' },
      display_name: { type: 'string' },
      lifecycle: { type: 'string' },
      current_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      draft: {
        anyOf: [
          { $ref: '#/components/schemas/DraftSummary' },
          { type: 'null' },
        ],
      },
      versions: {
        items: { $ref: '#/components/schemas/LibraryVersionSummary' },
        type: 'array',
      },
      revision: { type: 'integer', default: 1 },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['id', 'entry_type', 'display_name', 'lifecycle'],
    description:
      "``GET /library/entries/{id}`` and the create/save/archive/fork/clone result.\n\n``revision`` tracks the entry's open Draft (defaults to 1 once archived\nand the Draft has been dropped) and is the value clients must echo back\nvia ``If-Match`` on the next mutating command.",
  },
  save_library_draft_request: {
    properties: { content: { additionalProperties: true, type: 'object' } },
    type: 'object',
    required: ['content'],
  },
  save_product_brand_kit_200: {
    properties: {
      product_id: { type: 'string' },
      revision: { type: 'integer' },
      current: {
        anyOf: [
          { $ref: '#/components/schemas/BrandKitVersion' },
          { type: 'null' },
        ],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['product_id', 'revision'],
  },
  save_product_brand_kit_request: {
    properties: {
      logo: {
        anyOf: [
          { $ref: '#/components/schemas/BrandLogoInput' },
          { type: 'null' },
        ],
      },
      fonts: { $ref: '#/components/schemas/BrandFonts' },
      palette: {
        items: { type: 'string', pattern: '^#[0-9A-Fa-f]{6}$' },
        type: 'array',
        maxItems: 5,
        minItems: 1,
      },
      readability_rules: { $ref: '#/components/schemas/ReadabilityRules' },
      forbidden: {
        items: { type: 'string', maxLength: 120, minLength: 1 },
        type: 'array',
        maxItems: 8,
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['fonts', 'palette'],
  },
  start_experiment_200: {
    properties: {
      id: { type: 'string' },
      product_id: { type: 'string' },
      name: { type: 'string' },
      state: { type: 'string' },
      revision: { type: 'integer' },
      record_schema_version: { type: 'integer' },
      draft_context: { additionalProperties: true, type: 'object' },
      frozen_snapshot_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      created_at: { type: 'string', format: 'date-time' },
      frozen_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      started_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      closed_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      frozen_context: {
        anyOf: [
          { $ref: '#/components/schemas/ExperimentFrozenContext' },
          { type: 'null' },
        ],
      },
      entries: {
        items: { $ref: '#/components/schemas/ExperimentEntry' },
        type: 'array',
      },
      candidates: {
        items: { $ref: '#/components/schemas/ExperimentCandidate' },
        type: 'array',
      },
      judgements: {
        items: { $ref: '#/components/schemas/ExperimentJudgement' },
        type: 'array',
      },
      proposals: {
        items: { $ref: '#/components/schemas/ExperimentProposal' },
        type: 'array',
      },
      decisions: {
        items: { $ref: '#/components/schemas/ExperimentDecision' },
        type: 'array',
      },
      concept_resolution_evidence: {
        anyOf: [
          { $ref: '#/components/schemas/ConceptResolutionEvidence' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
    required: [
      'id',
      'product_id',
      'name',
      'state',
      'revision',
      'record_schema_version',
      'created_at',
    ],
  },
  start_experiment_request: {
    properties: { expected_revision: { type: 'integer', minimum: 1 } },
    type: 'object',
    required: ['expected_revision'],
  },
  start_localization_preparation_200: {
    properties: {
      batch_id: { type: 'string' },
      truth_package_id: { type: 'string' },
      truth_status: { type: 'string' },
      preparation_stage: {
        type: 'string',
        enum: ['analysis', 'truth', 'claim_review', 'copy', 'complete'],
      },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      missing_analysis: { items: { type: 'string' }, type: 'array' },
      replayed: { type: 'boolean' },
    },
    type: 'object',
    required: [
      'batch_id',
      'truth_package_id',
      'truth_status',
      'preparation_stage',
      'replayed',
    ],
  },
  start_localization_preparation_request: {
    properties: {
      truth_resolution_policy_version_id: { type: 'string' },
      manual_fact_set_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      analysis_recipe_hash: { type: 'string' },
      analysis_provider: { type: 'string' },
      analysis_model: { type: 'string' },
      analysis_adapter_version: { type: 'string' },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'truth_resolution_policy_version_id',
      'analysis_recipe_hash',
      'analysis_provider',
      'analysis_model',
      'analysis_adapter_version',
    ],
  },
  start_marketing_ai_qc_202: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  start_marketing_ai_qc_request: {
    properties: {
      quote_hash: { type: 'string', maxLength: 64, minLength: 64 },
      kind: { type: 'string', enum: ['single', 'paired'], default: 'single' },
      strategy_language: {
        anyOf: [{ type: 'string', enum: ['zh-CN', 'en-US'] }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['quote_hash'],
  },
  start_marketing_explanation_202: {
    properties: {
      id: { type: 'string' },
      data: { $ref: '#/components/schemas/MarketingRound' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'data'],
  },
  start_marketing_explanation_request: {
    properties: {
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      quote_hash: { type: 'string', maxLength: 64, default: '' },
      strategy_language: {
        anyOf: [{ type: 'string', enum: ['zh-CN', 'en-US'] }, { type: 'null' }],
      },
    },
    additionalProperties: false,
    type: 'object',
    required: ['execution_mode'],
  },
  start_marketing_planning_202: {
    properties: {
      id: { type: 'string' },
      operation_id: { type: 'string' },
      kind: { type: 'string', enum: ['understanding', 'plan'] },
      state: { type: 'string' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      error_code: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      maximum_cost: { type: 'string', default: '0' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['id', 'operation_id', 'kind', 'state', 'execution_mode'],
  },
  start_marketing_planning_request: {
    properties: {
      kind: { type: 'string', enum: ['understanding', 'plan'] },
      ad_language: {
        anyOf: [
          {
            type: 'string',
            maxLength: 35,
            minLength: 2,
            pattern: '^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$',
          },
          { type: 'null' },
        ],
      },
      strategy_language: {
        type: 'string',
        enum: ['zh-CN', 'en-US'],
        default: 'en-US',
      },
      direction: {
        anyOf: [
          {
            type: 'string',
            enum: [
              'screen_angles',
              'match_expressions',
              'confirm_combination',
              'optimize_elements',
            ],
          },
          { type: 'null' },
        ],
      },
      decision_question: { type: 'string', maxLength: 1200, default: '' },
      execution_mode: { type: 'string', enum: ['byok', 'mock'] },
      quote_hash: { type: 'string', maxLength: 64, default: '' },
    },
    additionalProperties: false,
    type: 'object',
    required: ['kind', 'execution_mode'],
  },
  submit_generation_run_202: {
    properties: {
      operation_id: { type: 'string' },
      generation_run_id: { type: 'string' },
    },
    type: 'object',
    required: ['operation_id', 'generation_run_id'],
    description:
      '``POST /generation/runs`` result: the Operation created by Atomic Submission.',
  },
  submit_generation_run_request: {
    properties: {
      preview_id: { type: 'string', minLength: 1 },
      preview_hash: { type: 'string', minLength: 1 },
    },
    type: 'object',
    required: ['preview_id', 'preview_hash'],
  },
  submit_localization_claim_decisions_200: {
    properties: {
      batch_id: { type: 'string' },
      decision_set_id: { type: 'string' },
      copy_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      copy_origin: { type: 'string' },
      operation_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean' },
    },
    type: 'object',
    required: ['batch_id', 'decision_set_id', 'copy_origin', 'replayed'],
  },
  submit_localization_claim_decisions_request: {
    properties: {
      asset_version_id: { type: 'string' },
      target_market: { type: 'string' },
      analysis_recipe_hash: { type: 'string' },
      decisions: {
        items: { $ref: '#/components/schemas/ClaimDecisionInputModel' },
        type: 'array',
        minItems: 1,
      },
      copy_policy_snapshot: { additionalProperties: true, type: 'object' },
      source_locale: { type: 'string' },
      target_locale: { type: 'string' },
      translation_recipe: {
        anyOf: [
          { $ref: '#/components/schemas/TranslationRecipeInput' },
          { type: 'null' },
        ],
      },
      requested_evidence_tier: {
        type: 'string',
        enum: ['mock', 'sandbox', 'production'],
      },
    },
    type: 'object',
    required: [
      'asset_version_id',
      'target_market',
      'analysis_recipe_hash',
      'decisions',
      'source_locale',
      'target_locale',
      'requested_evidence_tier',
    ],
  },
  supersede_meta_binding_201: {},
  supersede_meta_binding_request: {
    properties: {
      ad_account_id: { type: 'string' },
      meta_mode: { type: 'string' },
      access_token_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      app_secret_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['ad_account_id', 'meta_mode'],
  },
  update_experiment_context_200: {
    properties: {
      revision: { type: 'integer' },
      draft_context: { additionalProperties: true, type: 'object' },
    },
    type: 'object',
    required: ['revision', 'draft_context'],
  },
  update_experiment_context_request: {
    properties: {
      draft_context: { additionalProperties: true, type: 'object' },
      expected_revision: { type: 'integer', minimum: 1 },
    },
    type: 'object',
    required: ['draft_context', 'expected_revision'],
  },
  update_experiment_entry_200: {
    properties: {
      id: { type: 'string' },
      experiment_id: { type: 'string' },
      asset_version_id: { type: 'string' },
      position: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
      marketing_vars: { additionalProperties: true, type: 'object' },
      revision: { type: 'integer' },
    },
    type: 'object',
    required: ['id', 'experiment_id', 'asset_version_id', 'revision'],
  },
  update_experiment_entry_request: {
    properties: {
      asset_version_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      marketing_vars: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      expected_revision: { type: 'integer', minimum: 1 },
    },
    type: 'object',
    required: ['expected_revision'],
  },
  update_generation_defaults_200: {
    properties: {
      provider: { type: 'string' },
      model: { type: 'string' },
      execution_mode: { type: 'string' },
      aspect_ratio: { type: 'string' },
      variants: { additionalProperties: { type: 'integer' }, type: 'object' },
      source: { type: 'string' },
      revision: { type: 'integer' },
      credential_bound: { type: 'boolean' },
      warnings: { items: { type: 'string' }, type: 'array' },
    },
    type: 'object',
    required: [
      'provider',
      'model',
      'execution_mode',
      'aspect_ratio',
      'variants',
      'source',
      'revision',
      'credential_bound',
    ],
    description:
      'Effective「生成默认值」. ``source`` is ``workspace`` once an Owner\nsaved them (``revision`` >= 1), else ``computed`` (``revision`` 0).\n``warnings``: ``mock_mode`` (show the bind-a-key banner),\n``byok_credential_missing``.',
  },
  update_generation_defaults_request: {
    properties: {
      provider: { type: 'string', maxLength: 64, minLength: 1 },
      model: { type: 'string', maxLength: 128, minLength: 1 },
      execution_mode: { type: 'string', enum: ['mock', 'byok'] },
      aspect_ratio: { type: 'string', maxLength: 16, minLength: 1 },
      variants: {
        additionalProperties: { type: 'integer' },
        propertyNames: { enum: ['sample', 'small', 'full'] },
        type: 'object',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'provider',
      'model',
      'execution_mode',
      'aspect_ratio',
      'variants',
    ],
  },
  update_generation_draft_200: {
    properties: {
      id: { type: 'string' },
      product_id: { type: 'string' },
      provider: { type: 'string' },
      model: { type: 'string' },
      execution_mode: { type: 'string' },
      state: { type: 'string' },
      revision: { type: 'integer' },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      selectors: { additionalProperties: true, type: 'object' },
      combination_strategy: { additionalProperties: true, type: 'object' },
      format_config: { additionalProperties: true, type: 'object' },
      exclusions: { additionalProperties: true, type: 'object' },
      regeneration_intent: { type: 'string', default: 'new_generation' },
      source_asset_version_id: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      source_recipe_identity: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibility' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'id',
      'product_id',
      'provider',
      'model',
      'execution_mode',
      'state',
      'revision',
    ],
    description:
      '``GET /generation/drafts/{id}`` and the create/update/regenerate result.',
  },
  update_generation_draft_request: {
    properties: {
      product_id: {
        anyOf: [{ type: 'string', minLength: 1 }, { type: 'null' }],
      },
      provider: { anyOf: [{ type: 'string', minLength: 1 }, { type: 'null' }] },
      model: { anyOf: [{ type: 'string', minLength: 1 }, { type: 'null' }] },
      execution_mode: {
        anyOf: [{ type: 'string', minLength: 1 }, { type: 'null' }],
      },
      selectors: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      combination_strategy: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      format_config: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
      exclusions: {
        anyOf: [
          { additionalProperties: true, type: 'object' },
          { type: 'null' },
        ],
      },
    },
    type: 'object',
  },
  update_product_competitor_settings_200: {
    properties: {
      product_id: { type: 'string' },
      competitor_snapshot_in_drafts: { type: 'boolean' },
      updated_at: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      replayed: { type: 'boolean', default: false },
    },
    type: 'object',
    required: ['product_id', 'competitor_snapshot_in_drafts'],
    description:
      "Whether drafting cites the latest competitor snapshot of the plan's\nmarket (spec §7.2; on unless a member turned it off).",
  },
  update_product_competitor_settings_request: {
    properties: { competitor_snapshot_in_drafts: { type: 'boolean' } },
    additionalProperties: false,
    type: 'object',
    required: ['competitor_snapshot_in_drafts'],
  },
  update_product_reference_200: {
    properties: {
      product_id: { type: 'string' },
      entry_id: { type: 'string' },
      version_id: { type: 'string' },
      revision: { type: 'integer' },
      model_variants: { items: { type: 'string' }, type: 'array' },
      has_unpublished_changes: { type: 'boolean', default: false },
      items: {
        items: { $ref: '#/components/schemas/ProductReferenceItem' },
        type: 'array',
      },
    },
    type: 'object',
    required: [
      'product_id',
      'entry_id',
      'version_id',
      'revision',
      'model_variants',
      'items',
    ],
    description:
      "A projection of the product's immutable Library version/bindings.\n\n``revision`` is the If-Match value for edits. Deleted items remain in\nprior ProductVersions; ``version_id`` can be frozen by later consumers.",
  },
  update_product_reference_request: {
    properties: {
      role: {
        anyOf: [
          {
            type: 'string',
            enum: ['primary', 'detail', 'logo', 'certification_mark'],
          },
          { type: 'null' },
        ],
      },
      identity_status: {
        anyOf: [
          { type: 'string', enum: ['verified_physical', 'unverified'] },
          { type: 'null' },
        ],
      },
      note: { anyOf: [{ type: 'string', maxLength: 1200 }, { type: 'null' }] },
    },
    additionalProperties: false,
    type: 'object',
  },
  validate_library_draft_200: {
    properties: {
      entry_type: { type: 'string' },
      ok: { type: 'boolean' },
      content_hash: { type: 'string' },
      blocking: {
        items: { additionalProperties: true, type: 'object' },
        type: 'array',
      },
      warnings: {
        items: { additionalProperties: true, type: 'object' },
        type: 'array',
      },
    },
    type: 'object',
    required: ['entry_type', 'ok', 'content_hash'],
    description:
      '``POST /library/entries/{id}/validate`` result (contracts.ValidationReport.as_dict).',
  },
  withdraw_library_version_200: {
    properties: {
      version_id: { type: 'string' },
      entry_id: { type: 'string' },
      entry_type: { type: 'string' },
      state: { type: 'string' },
      eligibility: { type: 'string' },
      dependents: {
        items: { $ref: '#/components/schemas/ImpactDependent' },
        type: 'array',
      },
      reason: { type: 'string', default: '' },
    },
    type: 'object',
    required: ['version_id', 'entry_id', 'entry_type', 'state', 'eligibility'],
    description:
      '``GET /library/versions/{id}/impact`` (service._impact_payload).',
  },
  withdraw_library_version_request: {
    properties: {
      reason: { type: 'string', maxLength: 1000, minLength: 1 },
      impact_id: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      impact_hash: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
    type: 'object',
    required: ['reason'],
  },
  withdraw_scale_target_200: {
    properties: {
      id: { type: 'string' },
      product_version_id: { type: 'string' },
      offer_version_id: { type: 'string' },
      market_code: { type: 'string' },
      currency: { type: 'string' },
      target_cpa: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      breakeven_roas: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      minimum_economic_spend: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      lifecycle_state: { type: 'string' },
      creator_id: { type: 'string' },
      revision: { type: 'integer' },
      published_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      withdrawn_at: {
        anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }],
      },
      created_at: { type: 'string', format: 'date-time' },
      allowed_actions: {
        items: { $ref: '#/components/schemas/ActionEligibilityModel' },
        type: 'array',
      },
    },
    additionalProperties: false,
    type: 'object',
    required: [
      'id',
      'product_version_id',
      'offer_version_id',
      'market_code',
      'currency',
      'target_cpa',
      'breakeven_roas',
      'minimum_economic_spend',
      'lifecycle_state',
      'creator_id',
      'revision',
      'published_at',
      'withdrawn_at',
      'created_at',
      'allowed_actions',
    ],
  },
  withdraw_scale_target_request: {
    properties: {
      withdrawal_preview_id: { type: 'string', maxLength: 64, minLength: 1 },
    },
    additionalProperties: false,
    type: 'object',
    required: ['withdrawal_preview_id'],
  },
};

const VALIDATOR_FUNCTIONS: Record<string, ValidateFunction> =
  Object.fromEntries(
    Object.entries(SCHEMAS).map(([id, schema]) => [id, ajv.compile(schema)])
  );

/** Every declared 2xx/3xx response schema plus the shared Problem Details schema, keyed by validatorId (spec §6.2.5). */
export const RESPONSE_VALIDATORS: ReadonlyMap<string, ValidateFunction> =
  new Map([
    ['problem_details', VALIDATOR_FUNCTIONS['problem_details']],
    [
      'activate_scale_enrollment_200',
      VALIDATOR_FUNCTIONS['activate_scale_enrollment_200'],
    ],
    [
      'activate_scale_enrollment_request',
      VALIDATOR_FUNCTIONS['activate_scale_enrollment_request'],
    ],
    [
      'add_experiment_entry_201',
      VALIDATOR_FUNCTIONS['add_experiment_entry_201'],
    ],
    [
      'add_experiment_entry_request',
      VALIDATOR_FUNCTIONS['add_experiment_entry_request'],
    ],
    [
      'add_product_reference_200',
      VALIDATOR_FUNCTIONS['add_product_reference_200'],
    ],
    [
      'add_product_reference_request',
      VALIDATOR_FUNCTIONS['add_product_reference_request'],
    ],
    [
      'adopt_localization_isolated_output_200',
      VALIDATOR_FUNCTIONS['adopt_localization_isolated_output_200'],
    ],
    [
      'analyze_experiment_duplicates_200',
      VALIDATOR_FUNCTIONS['analyze_experiment_duplicates_200'],
    ],
    [
      'apply_library_template_201',
      VALIDATOR_FUNCTIONS['apply_library_template_201'],
    ],
    [
      'apply_library_template_request',
      VALIDATOR_FUNCTIONS['apply_library_template_request'],
    ],
    [
      'approve_localization_copy_200',
      VALIDATOR_FUNCTIONS['approve_localization_copy_200'],
    ],
    [
      'approve_localization_copy_request',
      VALIDATOR_FUNCTIONS['approve_localization_copy_request'],
    ],
    ['archive_competitor_200', VALIDATOR_FUNCTIONS['archive_competitor_200']],
    [
      'archive_library_entry_200',
      VALIDATOR_FUNCTIONS['archive_library_entry_200'],
    ],
    [
      'archive_library_entry_request',
      VALIDATOR_FUNCTIONS['archive_library_entry_request'],
    ],
    [
      'authorize_localization_isolated_qc_200',
      VALIDATOR_FUNCTIONS['authorize_localization_isolated_qc_200'],
    ],
    ['bind_credential_200', VALIDATOR_FUNCTIONS['bind_credential_200']],
    ['bind_credential_request', VALIDATOR_FUNCTIONS['bind_credential_request']],
    [
      'bind_experiment_candidate_200',
      VALIDATOR_FUNCTIONS['bind_experiment_candidate_200'],
    ],
    [
      'bind_experiment_candidate_request',
      VALIDATOR_FUNCTIONS['bind_experiment_candidate_request'],
    ],
    [
      'bind_library_data_source_credential_201',
      VALIDATOR_FUNCTIONS['bind_library_data_source_credential_201'],
    ],
    [
      'bind_library_data_source_credential_request',
      VALIDATOR_FUNCTIONS['bind_library_data_source_credential_request'],
    ],
    [
      'bind_library_reference_201',
      VALIDATOR_FUNCTIONS['bind_library_reference_201'],
    ],
    [
      'bind_library_reference_request',
      VALIDATOR_FUNCTIONS['bind_library_reference_request'],
    ],
    [
      'bind_marketing_delivery_201',
      VALIDATOR_FUNCTIONS['bind_marketing_delivery_201'],
    ],
    [
      'bind_marketing_delivery_request',
      VALIDATOR_FUNCTIONS['bind_marketing_delivery_request'],
    ],
    [
      'cancel_generation_run_200',
      VALIDATOR_FUNCTIONS['cancel_generation_run_200'],
    ],
    [
      'cancel_localization_batch_200',
      VALIDATOR_FUNCTIONS['cancel_localization_batch_200'],
    ],
    [
      'check_library_data_source_connection_202',
      VALIDATOR_FUNCTIONS['check_library_data_source_connection_202'],
    ],
    [
      'check_library_data_source_connection_request',
      VALIDATOR_FUNCTIONS['check_library_data_source_connection_request'],
    ],
    [
      'clone_library_product_201',
      VALIDATOR_FUNCTIONS['clone_library_product_201'],
    ],
    [
      'clone_library_product_request',
      VALIDATOR_FUNCTIONS['clone_library_product_request'],
    ],
    [
      'close_marketing_round_200',
      VALIDATOR_FUNCTIONS['close_marketing_round_200'],
    ],
    [
      'close_marketing_round_request',
      VALIDATOR_FUNCTIONS['close_marketing_round_request'],
    ],
    [
      'close_scale_enrollment_200',
      VALIDATOR_FUNCTIONS['close_scale_enrollment_200'],
    ],
    [
      'close_scale_enrollment_request',
      VALIDATOR_FUNCTIONS['close_scale_enrollment_request'],
    ],
    [
      'commit_results_import_200',
      VALIDATOR_FUNCTIONS['commit_results_import_200'],
    ],
    ['confirm_competitor_200', VALIDATOR_FUNCTIONS['confirm_competitor_200']],
    [
      'confirm_competitor_request',
      VALIDATOR_FUNCTIONS['confirm_competitor_request'],
    ],
    [
      'confirm_competitor_ad_tags_200',
      VALIDATOR_FUNCTIONS['confirm_competitor_ad_tags_200'],
    ],
    [
      'confirm_competitor_ad_tags_request',
      VALIDATOR_FUNCTIONS['confirm_competitor_ad_tags_request'],
    ],
    [
      'confirm_experiment_proposal_200',
      VALIDATOR_FUNCTIONS['confirm_experiment_proposal_200'],
    ],
    [
      'confirm_experiment_proposal_request',
      VALIDATOR_FUNCTIONS['confirm_experiment_proposal_request'],
    ],
    [
      'confirm_external_creative_200',
      VALIDATOR_FUNCTIONS['confirm_external_creative_200'],
    ],
    [
      'confirm_external_creative_request',
      VALIDATOR_FUNCTIONS['confirm_external_creative_request'],
    ],
    [
      'confirm_localization_batch_200',
      VALIDATOR_FUNCTIONS['confirm_localization_batch_200'],
    ],
    [
      'confirm_localization_batch_request',
      VALIDATOR_FUNCTIONS['confirm_localization_batch_request'],
    ],
    [
      'confirm_marketing_ai_qc_200',
      VALIDATOR_FUNCTIONS['confirm_marketing_ai_qc_200'],
    ],
    [
      'confirm_marketing_ai_qc_request',
      VALIDATOR_FUNCTIONS['confirm_marketing_ai_qc_request'],
    ],
    [
      'confirm_marketing_metrics_200',
      VALIDATOR_FUNCTIONS['confirm_marketing_metrics_200'],
    ],
    [
      'confirm_marketing_metrics_request',
      VALIDATOR_FUNCTIONS['confirm_marketing_metrics_request'],
    ],
    [
      'confirm_meta_launch_preview_200',
      VALIDATOR_FUNCTIONS['confirm_meta_launch_preview_200'],
    ],
    [
      'confirm_meta_launch_preview_request',
      VALIDATOR_FUNCTIONS['confirm_meta_launch_preview_request'],
    ],
    [
      'confirm_product_intake_200',
      VALIDATOR_FUNCTIONS['confirm_product_intake_200'],
    ],
    [
      'confirm_product_intake_request',
      VALIDATOR_FUNCTIONS['confirm_product_intake_request'],
    ],
    [
      'confirm_spend_guardrail_200',
      VALIDATOR_FUNCTIONS['confirm_spend_guardrail_200'],
    ],
    [
      'confirm_spend_guardrail_request',
      VALIDATOR_FUNCTIONS['confirm_spend_guardrail_request'],
    ],
    [
      'convert_scale_advice_occurrence_201',
      VALIDATOR_FUNCTIONS['convert_scale_advice_occurrence_201'],
    ],
    [
      'convert_scale_advice_occurrence_request',
      VALIDATOR_FUNCTIONS['convert_scale_advice_occurrence_request'],
    ],
    ['create_competitor_201', VALIDATOR_FUNCTIONS['create_competitor_201']],
    [
      'create_competitor_request',
      VALIDATOR_FUNCTIONS['create_competitor_request'],
    ],
    [
      'create_competitor_scan_202',
      VALIDATOR_FUNCTIONS['create_competitor_scan_202'],
    ],
    [
      'create_competitor_scan_request',
      VALIDATOR_FUNCTIONS['create_competitor_scan_request'],
    ],
    ['create_credential_200', VALIDATOR_FUNCTIONS['create_credential_200']],
    [
      'create_credential_request',
      VALIDATOR_FUNCTIONS['create_credential_request'],
    ],
    [
      'create_download_intent_201',
      VALIDATOR_FUNCTIONS['create_download_intent_201'],
    ],
    [
      'create_download_intent_request',
      VALIDATOR_FUNCTIONS['create_download_intent_request'],
    ],
    ['create_experiment_201', VALIDATOR_FUNCTIONS['create_experiment_201']],
    [
      'create_experiment_request',
      VALIDATOR_FUNCTIONS['create_experiment_request'],
    ],
    [
      'create_external_creatives_202',
      VALIDATOR_FUNCTIONS['create_external_creatives_202'],
    ],
    [
      'create_external_creatives_request',
      VALIDATOR_FUNCTIONS['create_external_creatives_request'],
    ],
    [
      'create_generation_asset_review_201',
      VALIDATOR_FUNCTIONS['create_generation_asset_review_201'],
    ],
    [
      'create_generation_asset_review_request',
      VALIDATOR_FUNCTIONS['create_generation_asset_review_request'],
    ],
    [
      'create_generation_asset_version_download_intent_201',
      VALIDATOR_FUNCTIONS[
        'create_generation_asset_version_download_intent_201'
      ],
    ],
    [
      'create_generation_draft_201',
      VALIDATOR_FUNCTIONS['create_generation_draft_201'],
    ],
    [
      'create_generation_draft_request',
      VALIDATOR_FUNCTIONS['create_generation_draft_request'],
    ],
    [
      'create_generation_export_download_intent_201',
      VALIDATOR_FUNCTIONS['create_generation_export_download_intent_201'],
    ],
    [
      'create_generation_export_package_202',
      VALIDATOR_FUNCTIONS['create_generation_export_package_202'],
    ],
    [
      'create_generation_export_package_request',
      VALIDATOR_FUNCTIONS['create_generation_export_package_request'],
    ],
    [
      'create_generation_preview_201',
      VALIDATOR_FUNCTIONS['create_generation_preview_201'],
    ],
    [
      'create_generation_regeneration_draft_201',
      VALIDATOR_FUNCTIONS['create_generation_regeneration_draft_201'],
    ],
    [
      'create_generation_regeneration_draft_request',
      VALIDATOR_FUNCTIONS['create_generation_regeneration_draft_request'],
    ],
    [
      'create_generation_review_summary_regeneration_201',
      VALIDATOR_FUNCTIONS['create_generation_review_summary_regeneration_201'],
    ],
    [
      'create_generation_review_summary_regeneration_request',
      VALIDATOR_FUNCTIONS[
        'create_generation_review_summary_regeneration_request'
      ],
    ],
    [
      'create_generation_run_recovery_preview_201',
      VALIDATOR_FUNCTIONS['create_generation_run_recovery_preview_201'],
    ],
    [
      'create_generation_run_recovery_preview_request',
      VALIDATOR_FUNCTIONS['create_generation_run_recovery_preview_request'],
    ],
    [
      'create_library_entry_201',
      VALIDATOR_FUNCTIONS['create_library_entry_201'],
    ],
    [
      'create_library_entry_request',
      VALIDATOR_FUNCTIONS['create_library_entry_request'],
    ],
    [
      'create_library_internal_entry_201',
      VALIDATOR_FUNCTIONS['create_library_internal_entry_201'],
    ],
    [
      'create_library_internal_entry_request',
      VALIDATOR_FUNCTIONS['create_library_internal_entry_request'],
    ],
    [
      'create_library_reference_download_intent_201',
      VALIDATOR_FUNCTIONS['create_library_reference_download_intent_201'],
    ],
    [
      'create_library_reference_object_202',
      VALIDATOR_FUNCTIONS['create_library_reference_object_202'],
    ],
    [
      'create_library_reference_object_request',
      VALIDATOR_FUNCTIONS['create_library_reference_object_request'],
    ],
    [
      'create_localization_batch_201',
      VALIDATOR_FUNCTIONS['create_localization_batch_201'],
    ],
    [
      'create_localization_batch_request',
      VALIDATOR_FUNCTIONS['create_localization_batch_request'],
    ],
    [
      'create_marketing_evidence_handoff_201',
      VALIDATOR_FUNCTIONS['create_marketing_evidence_handoff_201'],
    ],
    [
      'create_marketing_evidence_handoff_request',
      VALIDATOR_FUNCTIONS['create_marketing_evidence_handoff_request'],
    ],
    [
      'create_marketing_handoff_202',
      VALIDATOR_FUNCTIONS['create_marketing_handoff_202'],
    ],
    [
      'create_marketing_handoff_request',
      VALIDATOR_FUNCTIONS['create_marketing_handoff_request'],
    ],
    [
      'create_marketing_image_edit_202',
      VALIDATOR_FUNCTIONS['create_marketing_image_edit_202'],
    ],
    [
      'create_marketing_image_edit_request',
      VALIDATOR_FUNCTIONS['create_marketing_image_edit_request'],
    ],
    [
      'create_marketing_project_201',
      VALIDATOR_FUNCTIONS['create_marketing_project_201'],
    ],
    [
      'create_marketing_project_request',
      VALIDATOR_FUNCTIONS['create_marketing_project_request'],
    ],
    ['create_meta_binding_201', VALIDATOR_FUNCTIONS['create_meta_binding_201']],
    [
      'create_meta_binding_request',
      VALIDATOR_FUNCTIONS['create_meta_binding_request'],
    ],
    [
      'create_meta_connection_201',
      VALIDATOR_FUNCTIONS['create_meta_connection_201'],
    ],
    [
      'create_meta_connection_request',
      VALIDATOR_FUNCTIONS['create_meta_connection_request'],
    ],
    [
      'create_meta_contract_201',
      VALIDATOR_FUNCTIONS['create_meta_contract_201'],
    ],
    [
      'create_meta_contract_request',
      VALIDATOR_FUNCTIONS['create_meta_contract_request'],
    ],
    [
      'create_meta_launch_preview_201',
      VALIDATOR_FUNCTIONS['create_meta_launch_preview_201'],
    ],
    [
      'create_meta_launch_preview_request',
      VALIDATOR_FUNCTIONS['create_meta_launch_preview_request'],
    ],
    [
      'create_product_intake_202',
      VALIDATOR_FUNCTIONS['create_product_intake_202'],
    ],
    [
      'create_product_intake_request',
      VALIDATOR_FUNCTIONS['create_product_intake_request'],
    ],
    [
      'create_product_intake_image_download_intent_201',
      VALIDATOR_FUNCTIONS['create_product_intake_image_download_intent_201'],
    ],
    [
      'create_results_import_201',
      VALIDATOR_FUNCTIONS['create_results_import_201'],
    ],
    [
      'create_results_import_request',
      VALIDATOR_FUNCTIONS['create_results_import_request'],
    ],
    [
      'create_scale_activation_preview_201',
      VALIDATOR_FUNCTIONS['create_scale_activation_preview_201'],
    ],
    [
      'create_scale_activation_preview_request',
      VALIDATOR_FUNCTIONS['create_scale_activation_preview_request'],
    ],
    [
      'create_scale_advice_disposition_201',
      VALIDATOR_FUNCTIONS['create_scale_advice_disposition_201'],
    ],
    [
      'create_scale_advice_disposition_request',
      VALIDATOR_FUNCTIONS['create_scale_advice_disposition_request'],
    ],
    [
      'create_scale_advice_snooze_201',
      VALIDATOR_FUNCTIONS['create_scale_advice_snooze_201'],
    ],
    [
      'create_scale_advice_snooze_request',
      VALIDATOR_FUNCTIONS['create_scale_advice_snooze_request'],
    ],
    [
      'create_scale_enrollment_201',
      VALIDATOR_FUNCTIONS['create_scale_enrollment_201'],
    ],
    [
      'create_scale_enrollment_request',
      VALIDATOR_FUNCTIONS['create_scale_enrollment_request'],
    ],
    [
      'create_scale_enrollment_candidate_201',
      VALIDATOR_FUNCTIONS['create_scale_enrollment_candidate_201'],
    ],
    [
      'create_scale_enrollment_candidate_request',
      VALIDATOR_FUNCTIONS['create_scale_enrollment_candidate_request'],
    ],
    ['create_scale_export_202', VALIDATOR_FUNCTIONS['create_scale_export_202']],
    [
      'create_scale_export_request',
      VALIDATOR_FUNCTIONS['create_scale_export_request'],
    ],
    [
      'create_scale_export_download_intent_201',
      VALIDATOR_FUNCTIONS['create_scale_export_download_intent_201'],
    ],
    ['create_scale_target_201', VALIDATOR_FUNCTIONS['create_scale_target_201']],
    [
      'create_scale_target_request',
      VALIDATOR_FUNCTIONS['create_scale_target_request'],
    ],
    [
      'create_scale_target_publication_preview_201',
      VALIDATOR_FUNCTIONS['create_scale_target_publication_preview_201'],
    ],
    [
      'create_scale_target_publication_preview_request',
      VALIDATOR_FUNCTIONS['create_scale_target_publication_preview_request'],
    ],
    [
      'create_scale_target_withdrawal_preview_201',
      VALIDATOR_FUNCTIONS['create_scale_target_withdrawal_preview_201'],
    ],
    [
      'create_scale_target_withdrawal_preview_request',
      VALIDATOR_FUNCTIONS['create_scale_target_withdrawal_preview_request'],
    ],
    ['create_test_plan_202', VALIDATOR_FUNCTIONS['create_test_plan_202']],
    [
      'create_test_plan_request',
      VALIDATOR_FUNCTIONS['create_test_plan_request'],
    ],
    [
      'create_test_plan_run_202',
      VALIDATOR_FUNCTIONS['create_test_plan_run_202'],
    ],
    [
      'create_test_plan_run_request',
      VALIDATOR_FUNCTIONS['create_test_plan_run_request'],
    ],
    [
      'create_upload_intent_201',
      VALIDATOR_FUNCTIONS['create_upload_intent_201'],
    ],
    [
      'create_upload_intent_request',
      VALIDATOR_FUNCTIONS['create_upload_intent_request'],
    ],
    [
      'decide_competitor_discovery_200',
      VALIDATOR_FUNCTIONS['decide_competitor_discovery_200'],
    ],
    [
      'decide_competitor_discovery_request',
      VALIDATOR_FUNCTIONS['decide_competitor_discovery_request'],
    ],
    [
      'delete_product_reference_200',
      VALIDATOR_FUNCTIONS['delete_product_reference_200'],
    ],
    [
      'download_marketing_handoff_201',
      VALIDATOR_FUNCTIONS['download_marketing_handoff_201'],
    ],
    [
      'edit_marketing_project_200',
      VALIDATOR_FUNCTIONS['edit_marketing_project_200'],
    ],
    [
      'edit_marketing_project_request',
      VALIDATOR_FUNCTIONS['edit_marketing_project_request'],
    ],
    [
      'evaluate_marketing_round_200',
      VALIDATOR_FUNCTIONS['evaluate_marketing_round_200'],
    ],
    [
      'execute_meta_launch_preview_202',
      VALIDATOR_FUNCTIONS['execute_meta_launch_preview_202'],
    ],
    [
      'execute_meta_launch_preview_request',
      VALIDATOR_FUNCTIONS['execute_meta_launch_preview_request'],
    ],
    ['finalize_upload_202', VALIDATOR_FUNCTIONS['finalize_upload_202']],
    [
      'flip_competitor_ad_relevance_200',
      VALIDATOR_FUNCTIONS['flip_competitor_ad_relevance_200'],
    ],
    [
      'flip_competitor_ad_relevance_request',
      VALIDATOR_FUNCTIONS['flip_competitor_ad_relevance_request'],
    ],
    [
      'fork_library_version_201',
      VALIDATOR_FUNCTIONS['fork_library_version_201'],
    ],
    [
      'fork_library_version_request',
      VALIDATOR_FUNCTIONS['fork_library_version_request'],
    ],
    ['freeze_experiment_200', VALIDATOR_FUNCTIONS['freeze_experiment_200']],
    [
      'freeze_experiment_request',
      VALIDATOR_FUNCTIONS['freeze_experiment_request'],
    ],
    [
      'freeze_marketing_round_201',
      VALIDATOR_FUNCTIONS['freeze_marketing_round_201'],
    ],
    [
      'freeze_marketing_round_request',
      VALIDATOR_FUNCTIONS['freeze_marketing_round_request'],
    ],
    ['get_competitor_200', VALIDATOR_FUNCTIONS['get_competitor_200']],
    [
      'get_competitor_resolution_200',
      VALIDATOR_FUNCTIONS['get_competitor_resolution_200'],
    ],
    ['get_competitor_scan_200', VALIDATOR_FUNCTIONS['get_competitor_scan_200']],
    [
      'get_competitor_scan_snapshot_200',
      VALIDATOR_FUNCTIONS['get_competitor_scan_snapshot_200'],
    ],
    [
      'get_competitor_scan_summary_200',
      VALIDATOR_FUNCTIONS['get_competitor_scan_summary_200'],
    ],
    ['get_cost_summary_200', VALIDATOR_FUNCTIONS['get_cost_summary_200']],
    ['get_experiment_200', VALIDATOR_FUNCTIONS['get_experiment_200']],
    [
      'get_experiment_timeline_200',
      VALIDATOR_FUNCTIONS['get_experiment_timeline_200'],
    ],
    [
      'get_external_creative_200',
      VALIDATOR_FUNCTIONS['get_external_creative_200'],
    ],
    [
      'get_generation_asset_version_200',
      VALIDATOR_FUNCTIONS['get_generation_asset_version_200'],
    ],
    [
      'get_generation_capabilities_200',
      VALIDATOR_FUNCTIONS['get_generation_capabilities_200'],
    ],
    [
      'get_generation_defaults_200',
      VALIDATOR_FUNCTIONS['get_generation_defaults_200'],
    ],
    [
      'get_generation_draft_200',
      VALIDATOR_FUNCTIONS['get_generation_draft_200'],
    ],
    [
      'get_generation_export_manifest_200',
      VALIDATOR_FUNCTIONS['get_generation_export_manifest_200'],
    ],
    [
      'get_generation_export_package_200',
      VALIDATOR_FUNCTIONS['get_generation_export_package_200'],
    ],
    [
      'get_generation_preview_200',
      VALIDATOR_FUNCTIONS['get_generation_preview_200'],
    ],
    ['get_generation_run_200', VALIDATOR_FUNCTIONS['get_generation_run_200']],
    [
      'get_generation_run_item_200',
      VALIDATOR_FUNCTIONS['get_generation_run_item_200'],
    ],
    ['get_identity_probe_200', VALIDATOR_FUNCTIONS['get_identity_probe_200']],
    [
      'get_library_command_outcome_200',
      VALIDATOR_FUNCTIONS['get_library_command_outcome_200'],
    ],
    [
      'get_library_data_source_connection_check_200',
      VALIDATOR_FUNCTIONS['get_library_data_source_connection_check_200'],
    ],
    [
      'get_library_data_source_credential_binding_200',
      VALIDATOR_FUNCTIONS['get_library_data_source_credential_binding_200'],
    ],
    ['get_library_entry_200', VALIDATOR_FUNCTIONS['get_library_entry_200']],
    [
      'get_library_internal_entry_200',
      VALIDATOR_FUNCTIONS['get_library_internal_entry_200'],
    ],
    [
      'get_library_internal_entry_context_200',
      VALIDATOR_FUNCTIONS['get_library_internal_entry_context_200'],
    ],
    [
      'get_library_reference_object_200',
      VALIDATOR_FUNCTIONS['get_library_reference_object_200'],
    ],
    ['get_library_version_200', VALIDATOR_FUNCTIONS['get_library_version_200']],
    [
      'get_library_version_impact_200',
      VALIDATOR_FUNCTIONS['get_library_version_impact_200'],
    ],
    [
      'get_localization_batch_200',
      VALIDATOR_FUNCTIONS['get_localization_batch_200'],
    ],
    [
      'get_localization_batch_outcome_200',
      VALIDATOR_FUNCTIONS['get_localization_batch_outcome_200'],
    ],
    [
      'get_localization_item_200',
      VALIDATOR_FUNCTIONS['get_localization_item_200'],
    ],
    [
      'get_localization_workspace_200',
      VALIDATOR_FUNCTIONS['get_localization_workspace_200'],
    ],
    [
      'get_marketing_ai_qc_quote_200',
      VALIDATOR_FUNCTIONS['get_marketing_ai_qc_quote_200'],
    ],
    [
      'get_marketing_capabilities_200',
      VALIDATOR_FUNCTIONS['get_marketing_capabilities_200'],
    ],
    [
      'get_marketing_evidence_handoff_200',
      VALIDATOR_FUNCTIONS['get_marketing_evidence_handoff_200'],
    ],
    [
      'get_marketing_project_200',
      VALIDATOR_FUNCTIONS['get_marketing_project_200'],
    ],
    ['get_marketing_round_200', VALIDATOR_FUNCTIONS['get_marketing_round_200']],
    [
      'get_marketing_workflow_200',
      VALIDATOR_FUNCTIONS['get_marketing_workflow_200'],
    ],
    ['get_meta_connection_200', VALIDATOR_FUNCTIONS['get_meta_connection_200']],
    [
      'get_meta_launch_preview_200',
      VALIDATOR_FUNCTIONS['get_meta_launch_preview_200'],
    ],
    [
      'get_meta_launch_provisioning_200',
      VALIDATOR_FUNCTIONS['get_meta_launch_provisioning_200'],
    ],
    ['get_operation_200', VALIDATOR_FUNCTIONS['get_operation_200']],
    [
      'get_product_brand_kit_200',
      VALIDATOR_FUNCTIONS['get_product_brand_kit_200'],
    ],
    [
      'get_product_competitor_settings_200',
      VALIDATOR_FUNCTIONS['get_product_competitor_settings_200'],
    ],
    ['get_product_intake_200', VALIDATOR_FUNCTIONS['get_product_intake_200']],
    [
      'get_product_learnings_200',
      VALIDATOR_FUNCTIONS['get_product_learnings_200'],
    ],
    [
      'get_product_references_200',
      VALIDATOR_FUNCTIONS['get_product_references_200'],
    ],
    ['get_results_import_200', VALIDATOR_FUNCTIONS['get_results_import_200']],
    [
      'get_results_template_200',
      VALIDATOR_FUNCTIONS['get_results_template_200'],
    ],
    [
      'get_scale_command_outcome_200',
      VALIDATOR_FUNCTIONS['get_scale_command_outcome_200'],
    ],
    [
      'get_scale_enrollment_200',
      VALIDATOR_FUNCTIONS['get_scale_enrollment_200'],
    ],
    [
      'get_scale_enrollment_breakdowns_200',
      VALIDATOR_FUNCTIONS['get_scale_enrollment_breakdowns_200'],
    ],
    [
      'get_scale_enrollment_evidence_200',
      VALIDATOR_FUNCTIONS['get_scale_enrollment_evidence_200'],
    ],
    ['get_scale_episode_200', VALIDATOR_FUNCTIONS['get_scale_episode_200']],
    ['get_scale_target_200', VALIDATOR_FUNCTIONS['get_scale_target_200']],
    ['get_spend_guardrail_200', VALIDATOR_FUNCTIONS['get_spend_guardrail_200']],
    ['get_test_plan_200', VALIDATOR_FUNCTIONS['get_test_plan_200']],
    [
      'get_test_plan_results_200',
      VALIDATOR_FUNCTIONS['get_test_plan_results_200'],
    ],
    ['get_usage_200', VALIDATOR_FUNCTIONS['get_usage_200']],
    [
      'import_library_manual_facts_202',
      VALIDATOR_FUNCTIONS['import_library_manual_facts_202'],
    ],
    [
      'import_library_manual_facts_request',
      VALIDATOR_FUNCTIONS['import_library_manual_facts_request'],
    ],
    ['judge_experiment_200', VALIDATOR_FUNCTIONS['judge_experiment_200']],
    [
      'judge_experiment_request',
      VALIDATOR_FUNCTIONS['judge_experiment_request'],
    ],
    ['list_audit_events_200', VALIDATOR_FUNCTIONS['list_audit_events_200']],
    [
      'list_competitor_ad_assessments_200',
      VALIDATOR_FUNCTIONS['list_competitor_ad_assessments_200'],
    ],
    [
      'list_competitor_ad_tags_200',
      VALIDATOR_FUNCTIONS['list_competitor_ad_tags_200'],
    ],
    [
      'list_competitor_scan_ads_200',
      VALIDATOR_FUNCTIONS['list_competitor_scan_ads_200'],
    ],
    [
      'list_competitor_scan_discoveries_200',
      VALIDATOR_FUNCTIONS['list_competitor_scan_discoveries_200'],
    ],
    [
      'list_competitor_scan_snapshots_200',
      VALIDATOR_FUNCTIONS['list_competitor_scan_snapshots_200'],
    ],
    [
      'list_competitor_scans_200',
      VALIDATOR_FUNCTIONS['list_competitor_scans_200'],
    ],
    ['list_competitors_200', VALIDATOR_FUNCTIONS['list_competitors_200']],
    ['list_credentials_200', VALIDATOR_FUNCTIONS['list_credentials_200']],
    ['list_experiments_200', VALIDATOR_FUNCTIONS['list_experiments_200']],
    [
      'list_external_creatives_200',
      VALIDATOR_FUNCTIONS['list_external_creatives_200'],
    ],
    [
      'list_generation_assets_200',
      VALIDATOR_FUNCTIONS['list_generation_assets_200'],
    ],
    [
      'list_generation_drafts_200',
      VALIDATOR_FUNCTIONS['list_generation_drafts_200'],
    ],
    [
      'list_generation_run_items_200',
      VALIDATOR_FUNCTIONS['list_generation_run_items_200'],
    ],
    [
      'list_generation_runs_200',
      VALIDATOR_FUNCTIONS['list_generation_runs_200'],
    ],
    [
      'list_library_entries_200',
      VALIDATOR_FUNCTIONS['list_library_entries_200'],
    ],
    [
      'list_library_entry_events_200',
      VALIDATOR_FUNCTIONS['list_library_entry_events_200'],
    ],
    [
      'list_library_entry_types_200',
      VALIDATOR_FUNCTIONS['list_library_entry_types_200'],
    ],
    [
      'list_library_entry_versions_200',
      VALIDATOR_FUNCTIONS['list_library_entry_versions_200'],
    ],
    [
      'list_library_internal_entries_200',
      VALIDATOR_FUNCTIONS['list_library_internal_entries_200'],
    ],
    [
      'list_library_version_bindings_200',
      VALIDATOR_FUNCTIONS['list_library_version_bindings_200'],
    ],
    [
      'list_localization_batches_200',
      VALIDATOR_FUNCTIONS['list_localization_batches_200'],
    ],
    [
      'list_marketing_brand_kits_200',
      VALIDATOR_FUNCTIONS['list_marketing_brand_kits_200'],
    ],
    [
      'list_marketing_projects_200',
      VALIDATOR_FUNCTIONS['list_marketing_projects_200'],
    ],
    ['list_meta_bindings_200', VALIDATOR_FUNCTIONS['list_meta_bindings_200']],
    [
      'list_meta_connections_200',
      VALIDATOR_FUNCTIONS['list_meta_connections_200'],
    ],
    ['list_meta_contracts_200', VALIDATOR_FUNCTIONS['list_meta_contracts_200']],
    ['list_products_200', VALIDATOR_FUNCTIONS['list_products_200']],
    [
      'list_scale_advice_threads_200',
      VALIDATOR_FUNCTIONS['list_scale_advice_threads_200'],
    ],
    [
      'list_scale_available_modes_200',
      VALIDATOR_FUNCTIONS['list_scale_available_modes_200'],
    ],
    [
      'list_scale_enrollment_candidates_200',
      VALIDATOR_FUNCTIONS['list_scale_enrollment_candidates_200'],
    ],
    [
      'list_scale_enrollments_200',
      VALIDATOR_FUNCTIONS['list_scale_enrollments_200'],
    ],
    [
      'list_scale_target_contexts_200',
      VALIDATOR_FUNCTIONS['list_scale_target_contexts_200'],
    ],
    ['list_scale_targets_200', VALIDATOR_FUNCTIONS['list_scale_targets_200']],
    ['list_test_plans_200', VALIDATOR_FUNCTIONS['list_test_plans_200']],
    [
      'pause_scale_enrollment_200',
      VALIDATOR_FUNCTIONS['pause_scale_enrollment_200'],
    ],
    [
      'pause_scale_enrollment_request',
      VALIDATOR_FUNCTIONS['pause_scale_enrollment_request'],
    ],
    [
      'prepare_marketing_feedback_201',
      VALIDATOR_FUNCTIONS['prepare_marketing_feedback_201'],
    ],
    [
      'prepare_marketing_feedback_request',
      VALIDATOR_FUNCTIONS['prepare_marketing_feedback_request'],
    ],
    [
      'prepare_marketing_generation_201',
      VALIDATOR_FUNCTIONS['prepare_marketing_generation_201'],
    ],
    [
      'prepare_marketing_generation_request',
      VALIDATOR_FUNCTIONS['prepare_marketing_generation_request'],
    ],
    [
      'preview_competitor_scan_200',
      VALIDATOR_FUNCTIONS['preview_competitor_scan_200'],
    ],
    [
      'preview_competitor_scan_request',
      VALIDATOR_FUNCTIONS['preview_competitor_scan_request'],
    ],
    [
      'preview_library_archive_impact_201',
      VALIDATOR_FUNCTIONS['preview_library_archive_impact_201'],
    ],
    [
      'preview_library_publish_impact_201',
      VALIDATOR_FUNCTIONS['preview_library_publish_impact_201'],
    ],
    [
      'preview_library_template_reapply_201',
      VALIDATOR_FUNCTIONS['preview_library_template_reapply_201'],
    ],
    [
      'preview_library_template_reapply_request',
      VALIDATOR_FUNCTIONS['preview_library_template_reapply_request'],
    ],
    [
      'preview_library_withdraw_impact_201',
      VALIDATOR_FUNCTIONS['preview_library_withdraw_impact_201'],
    ],
    [
      'preview_marketing_metrics_201',
      VALIDATOR_FUNCTIONS['preview_marketing_metrics_201'],
    ],
    [
      'preview_marketing_metrics_request',
      VALIDATOR_FUNCTIONS['preview_marketing_metrics_request'],
    ],
    [
      'preview_spend_guardrail_200',
      VALIDATOR_FUNCTIONS['preview_spend_guardrail_200'],
    ],
    [
      'preview_spend_guardrail_request',
      VALIDATOR_FUNCTIONS['preview_spend_guardrail_request'],
    ],
    [
      'promote_library_reference_object_201',
      VALIDATOR_FUNCTIONS['promote_library_reference_object_201'],
    ],
    [
      'promote_library_reference_object_request',
      VALIDATOR_FUNCTIONS['promote_library_reference_object_request'],
    ],
    [
      'publish_library_entry_201',
      VALIDATOR_FUNCTIONS['publish_library_entry_201'],
    ],
    [
      'publish_library_entry_request',
      VALIDATOR_FUNCTIONS['publish_library_entry_request'],
    ],
    [
      'quote_competitor_resolve_200',
      VALIDATOR_FUNCTIONS['quote_competitor_resolve_200'],
    ],
    [
      'quote_competitor_scan_recluster_200',
      VALIDATOR_FUNCTIONS['quote_competitor_scan_recluster_200'],
    ],
    [
      'quote_competitor_scan_resume_200',
      VALIDATOR_FUNCTIONS['quote_competitor_scan_resume_200'],
    ],
    [
      'recluster_competitor_scan_202',
      VALIDATOR_FUNCTIONS['recluster_competitor_scan_202'],
    ],
    [
      'reconcile_competitor_scan_200',
      VALIDATOR_FUNCTIONS['reconcile_competitor_scan_200'],
    ],
    [
      'reconcile_competitor_scan_request',
      VALIDATOR_FUNCTIONS['reconcile_competitor_scan_request'],
    ],
    [
      'reconcile_generation_run_200',
      VALIDATOR_FUNCTIONS['reconcile_generation_run_200'],
    ],
    [
      'reconcile_generation_run_request',
      VALIDATOR_FUNCTIONS['reconcile_generation_run_request'],
    ],
    [
      'reconcile_library_command_outcome_200',
      VALIDATOR_FUNCTIONS['reconcile_library_command_outcome_200'],
    ],
    [
      'recover_meta_launch_operation_202',
      VALIDATOR_FUNCTIONS['recover_meta_launch_operation_202'],
    ],
    [
      'recover_meta_launch_operation_request',
      VALIDATOR_FUNCTIONS['recover_meta_launch_operation_request'],
    ],
    [
      'refreeze_competitor_scan_200',
      VALIDATOR_FUNCTIONS['refreeze_competitor_scan_200'],
    ],
    ['resolve_competitor_202', VALIDATOR_FUNCTIONS['resolve_competitor_202']],
    [
      'resolve_competitor_request',
      VALIDATOR_FUNCTIONS['resolve_competitor_request'],
    ],
    [
      'resolve_marketing_call_202',
      VALIDATOR_FUNCTIONS['resolve_marketing_call_202'],
    ],
    [
      'resolve_marketing_call_request',
      VALIDATOR_FUNCTIONS['resolve_marketing_call_request'],
    ],
    [
      'resume_competitor_scan_202',
      VALIDATOR_FUNCTIONS['resume_competitor_scan_202'],
    ],
    [
      'resume_generation_run_paused_items_200',
      VALIDATOR_FUNCTIONS['resume_generation_run_paused_items_200'],
    ],
    [
      'resume_generation_run_paused_items_request',
      VALIDATOR_FUNCTIONS['resume_generation_run_paused_items_request'],
    ],
    [
      'resume_localization_item_200',
      VALIDATOR_FUNCTIONS['resume_localization_item_200'],
    ],
    [
      'resume_scale_enrollment_200',
      VALIDATOR_FUNCTIONS['resume_scale_enrollment_200'],
    ],
    [
      'resume_scale_enrollment_request',
      VALIDATOR_FUNCTIONS['resume_scale_enrollment_request'],
    ],
    [
      'retire_meta_connection_200',
      VALIDATOR_FUNCTIONS['retire_meta_connection_200'],
    ],
    [
      'retire_meta_connection_request',
      VALIDATOR_FUNCTIONS['retire_meta_connection_request'],
    ],
    [
      'retry_generation_run_failed_items_200',
      VALIDATOR_FUNCTIONS['retry_generation_run_failed_items_200'],
    ],
    [
      'retry_generation_run_failed_items_request',
      VALIDATOR_FUNCTIONS['retry_generation_run_failed_items_request'],
    ],
    [
      'retry_localization_item_200',
      VALIDATOR_FUNCTIONS['retry_localization_item_200'],
    ],
    [
      'retry_marketing_handoff_202',
      VALIDATOR_FUNCTIONS['retry_marketing_handoff_202'],
    ],
    [
      'reuse_marketing_creative_201',
      VALIDATOR_FUNCTIONS['reuse_marketing_creative_201'],
    ],
    [
      'reuse_marketing_creative_request',
      VALIDATOR_FUNCTIONS['reuse_marketing_creative_request'],
    ],
    [
      'review_marketing_creative_201',
      VALIDATOR_FUNCTIONS['review_marketing_creative_201'],
    ],
    [
      'review_marketing_creative_request',
      VALIDATOR_FUNCTIONS['review_marketing_creative_request'],
    ],
    ['revise_test_plan_200', VALIDATOR_FUNCTIONS['revise_test_plan_200']],
    [
      'revise_test_plan_request',
      VALIDATOR_FUNCTIONS['revise_test_plan_request'],
    ],
    ['revoke_credential_200', VALIDATOR_FUNCTIONS['revoke_credential_200']],
    [
      'revoke_credential_request',
      VALIDATOR_FUNCTIONS['revoke_credential_request'],
    ],
    ['rotate_credential_200', VALIDATOR_FUNCTIONS['rotate_credential_200']],
    [
      'rotate_credential_request',
      VALIDATOR_FUNCTIONS['rotate_credential_request'],
    ],
    ['save_library_draft_200', VALIDATOR_FUNCTIONS['save_library_draft_200']],
    [
      'save_library_draft_request',
      VALIDATOR_FUNCTIONS['save_library_draft_request'],
    ],
    [
      'save_product_brand_kit_200',
      VALIDATOR_FUNCTIONS['save_product_brand_kit_200'],
    ],
    [
      'save_product_brand_kit_request',
      VALIDATOR_FUNCTIONS['save_product_brand_kit_request'],
    ],
    ['start_experiment_200', VALIDATOR_FUNCTIONS['start_experiment_200']],
    [
      'start_experiment_request',
      VALIDATOR_FUNCTIONS['start_experiment_request'],
    ],
    [
      'start_localization_preparation_200',
      VALIDATOR_FUNCTIONS['start_localization_preparation_200'],
    ],
    [
      'start_localization_preparation_request',
      VALIDATOR_FUNCTIONS['start_localization_preparation_request'],
    ],
    [
      'start_marketing_ai_qc_202',
      VALIDATOR_FUNCTIONS['start_marketing_ai_qc_202'],
    ],
    [
      'start_marketing_ai_qc_request',
      VALIDATOR_FUNCTIONS['start_marketing_ai_qc_request'],
    ],
    [
      'start_marketing_explanation_202',
      VALIDATOR_FUNCTIONS['start_marketing_explanation_202'],
    ],
    [
      'start_marketing_explanation_request',
      VALIDATOR_FUNCTIONS['start_marketing_explanation_request'],
    ],
    [
      'start_marketing_planning_202',
      VALIDATOR_FUNCTIONS['start_marketing_planning_202'],
    ],
    [
      'start_marketing_planning_request',
      VALIDATOR_FUNCTIONS['start_marketing_planning_request'],
    ],
    [
      'submit_generation_run_202',
      VALIDATOR_FUNCTIONS['submit_generation_run_202'],
    ],
    [
      'submit_generation_run_request',
      VALIDATOR_FUNCTIONS['submit_generation_run_request'],
    ],
    [
      'submit_localization_claim_decisions_200',
      VALIDATOR_FUNCTIONS['submit_localization_claim_decisions_200'],
    ],
    [
      'submit_localization_claim_decisions_request',
      VALIDATOR_FUNCTIONS['submit_localization_claim_decisions_request'],
    ],
    [
      'supersede_meta_binding_201',
      VALIDATOR_FUNCTIONS['supersede_meta_binding_201'],
    ],
    [
      'supersede_meta_binding_request',
      VALIDATOR_FUNCTIONS['supersede_meta_binding_request'],
    ],
    [
      'update_experiment_context_200',
      VALIDATOR_FUNCTIONS['update_experiment_context_200'],
    ],
    [
      'update_experiment_context_request',
      VALIDATOR_FUNCTIONS['update_experiment_context_request'],
    ],
    [
      'update_experiment_entry_200',
      VALIDATOR_FUNCTIONS['update_experiment_entry_200'],
    ],
    [
      'update_experiment_entry_request',
      VALIDATOR_FUNCTIONS['update_experiment_entry_request'],
    ],
    [
      'update_generation_defaults_200',
      VALIDATOR_FUNCTIONS['update_generation_defaults_200'],
    ],
    [
      'update_generation_defaults_request',
      VALIDATOR_FUNCTIONS['update_generation_defaults_request'],
    ],
    [
      'update_generation_draft_200',
      VALIDATOR_FUNCTIONS['update_generation_draft_200'],
    ],
    [
      'update_generation_draft_request',
      VALIDATOR_FUNCTIONS['update_generation_draft_request'],
    ],
    [
      'update_product_competitor_settings_200',
      VALIDATOR_FUNCTIONS['update_product_competitor_settings_200'],
    ],
    [
      'update_product_competitor_settings_request',
      VALIDATOR_FUNCTIONS['update_product_competitor_settings_request'],
    ],
    [
      'update_product_reference_200',
      VALIDATOR_FUNCTIONS['update_product_reference_200'],
    ],
    [
      'update_product_reference_request',
      VALIDATOR_FUNCTIONS['update_product_reference_request'],
    ],
    [
      'validate_library_draft_200',
      VALIDATOR_FUNCTIONS['validate_library_draft_200'],
    ],
    [
      'withdraw_library_version_200',
      VALIDATOR_FUNCTIONS['withdraw_library_version_200'],
    ],
    [
      'withdraw_library_version_request',
      VALIDATOR_FUNCTIONS['withdraw_library_version_request'],
    ],
    [
      'withdraw_scale_target_200',
      VALIDATOR_FUNCTIONS['withdraw_scale_target_200'],
    ],
    [
      'withdraw_scale_target_request',
      VALIDATOR_FUNCTIONS['withdraw_scale_target_request'],
    ],
  ]);

type OperationId = keyof DomainOperationMap;
type BodyOperationId = {
  [K in OperationId]: [DomainOperationMap[K]['requestBody']] extends [never]
    ? never
    : K;
}[OperationId];

type RequestValidatorMap = {
  [K in BodyOperationId]: ValidateFunction<
    DomainOperationMap[K]['requestBody']
  >;
};

/** One request-body validator per body-bearing operation (spec §6.2.5). Empty at the tracer milestone — no operation declares a request body yet. */
export const REQUEST_VALIDATORS = {
  activate_scale_enrollment: VALIDATOR_FUNCTIONS[
    'activate_scale_enrollment_request'
  ] as ValidateFunction<
    DomainOperationMap['activate_scale_enrollment']['requestBody']
  >,
  add_experiment_entry: VALIDATOR_FUNCTIONS[
    'add_experiment_entry_request'
  ] as ValidateFunction<
    DomainOperationMap['add_experiment_entry']['requestBody']
  >,
  add_product_reference: VALIDATOR_FUNCTIONS[
    'add_product_reference_request'
  ] as ValidateFunction<
    DomainOperationMap['add_product_reference']['requestBody']
  >,
  apply_library_template: VALIDATOR_FUNCTIONS[
    'apply_library_template_request'
  ] as ValidateFunction<
    DomainOperationMap['apply_library_template']['requestBody']
  >,
  approve_localization_copy: VALIDATOR_FUNCTIONS[
    'approve_localization_copy_request'
  ] as ValidateFunction<
    DomainOperationMap['approve_localization_copy']['requestBody']
  >,
  archive_library_entry: VALIDATOR_FUNCTIONS[
    'archive_library_entry_request'
  ] as ValidateFunction<
    DomainOperationMap['archive_library_entry']['requestBody']
  >,
  bind_credential: VALIDATOR_FUNCTIONS[
    'bind_credential_request'
  ] as ValidateFunction<DomainOperationMap['bind_credential']['requestBody']>,
  bind_experiment_candidate: VALIDATOR_FUNCTIONS[
    'bind_experiment_candidate_request'
  ] as ValidateFunction<
    DomainOperationMap['bind_experiment_candidate']['requestBody']
  >,
  bind_library_data_source_credential: VALIDATOR_FUNCTIONS[
    'bind_library_data_source_credential_request'
  ] as ValidateFunction<
    DomainOperationMap['bind_library_data_source_credential']['requestBody']
  >,
  bind_library_reference: VALIDATOR_FUNCTIONS[
    'bind_library_reference_request'
  ] as ValidateFunction<
    DomainOperationMap['bind_library_reference']['requestBody']
  >,
  bind_marketing_delivery: VALIDATOR_FUNCTIONS[
    'bind_marketing_delivery_request'
  ] as ValidateFunction<
    DomainOperationMap['bind_marketing_delivery']['requestBody']
  >,
  check_library_data_source_connection: VALIDATOR_FUNCTIONS[
    'check_library_data_source_connection_request'
  ] as ValidateFunction<
    DomainOperationMap['check_library_data_source_connection']['requestBody']
  >,
  clone_library_product: VALIDATOR_FUNCTIONS[
    'clone_library_product_request'
  ] as ValidateFunction<
    DomainOperationMap['clone_library_product']['requestBody']
  >,
  close_marketing_round: VALIDATOR_FUNCTIONS[
    'close_marketing_round_request'
  ] as ValidateFunction<
    DomainOperationMap['close_marketing_round']['requestBody']
  >,
  close_scale_enrollment: VALIDATOR_FUNCTIONS[
    'close_scale_enrollment_request'
  ] as ValidateFunction<
    DomainOperationMap['close_scale_enrollment']['requestBody']
  >,
  confirm_competitor: VALIDATOR_FUNCTIONS[
    'confirm_competitor_request'
  ] as ValidateFunction<
    DomainOperationMap['confirm_competitor']['requestBody']
  >,
  confirm_competitor_ad_tags: VALIDATOR_FUNCTIONS[
    'confirm_competitor_ad_tags_request'
  ] as ValidateFunction<
    DomainOperationMap['confirm_competitor_ad_tags']['requestBody']
  >,
  confirm_experiment_proposal: VALIDATOR_FUNCTIONS[
    'confirm_experiment_proposal_request'
  ] as ValidateFunction<
    DomainOperationMap['confirm_experiment_proposal']['requestBody']
  >,
  confirm_external_creative: VALIDATOR_FUNCTIONS[
    'confirm_external_creative_request'
  ] as ValidateFunction<
    DomainOperationMap['confirm_external_creative']['requestBody']
  >,
  confirm_localization_batch: VALIDATOR_FUNCTIONS[
    'confirm_localization_batch_request'
  ] as ValidateFunction<
    DomainOperationMap['confirm_localization_batch']['requestBody']
  >,
  confirm_marketing_ai_qc: VALIDATOR_FUNCTIONS[
    'confirm_marketing_ai_qc_request'
  ] as ValidateFunction<
    DomainOperationMap['confirm_marketing_ai_qc']['requestBody']
  >,
  confirm_marketing_metrics: VALIDATOR_FUNCTIONS[
    'confirm_marketing_metrics_request'
  ] as ValidateFunction<
    DomainOperationMap['confirm_marketing_metrics']['requestBody']
  >,
  confirm_meta_launch_preview: VALIDATOR_FUNCTIONS[
    'confirm_meta_launch_preview_request'
  ] as ValidateFunction<
    DomainOperationMap['confirm_meta_launch_preview']['requestBody']
  >,
  confirm_product_intake: VALIDATOR_FUNCTIONS[
    'confirm_product_intake_request'
  ] as ValidateFunction<
    DomainOperationMap['confirm_product_intake']['requestBody']
  >,
  confirm_spend_guardrail: VALIDATOR_FUNCTIONS[
    'confirm_spend_guardrail_request'
  ] as ValidateFunction<
    DomainOperationMap['confirm_spend_guardrail']['requestBody']
  >,
  convert_scale_advice_occurrence: VALIDATOR_FUNCTIONS[
    'convert_scale_advice_occurrence_request'
  ] as ValidateFunction<
    DomainOperationMap['convert_scale_advice_occurrence']['requestBody']
  >,
  create_competitor: VALIDATOR_FUNCTIONS[
    'create_competitor_request'
  ] as ValidateFunction<DomainOperationMap['create_competitor']['requestBody']>,
  create_competitor_scan: VALIDATOR_FUNCTIONS[
    'create_competitor_scan_request'
  ] as ValidateFunction<
    DomainOperationMap['create_competitor_scan']['requestBody']
  >,
  create_credential: VALIDATOR_FUNCTIONS[
    'create_credential_request'
  ] as ValidateFunction<DomainOperationMap['create_credential']['requestBody']>,
  create_download_intent: VALIDATOR_FUNCTIONS[
    'create_download_intent_request'
  ] as ValidateFunction<
    DomainOperationMap['create_download_intent']['requestBody']
  >,
  create_experiment: VALIDATOR_FUNCTIONS[
    'create_experiment_request'
  ] as ValidateFunction<DomainOperationMap['create_experiment']['requestBody']>,
  create_external_creatives: VALIDATOR_FUNCTIONS[
    'create_external_creatives_request'
  ] as ValidateFunction<
    DomainOperationMap['create_external_creatives']['requestBody']
  >,
  create_generation_asset_review: VALIDATOR_FUNCTIONS[
    'create_generation_asset_review_request'
  ] as ValidateFunction<
    DomainOperationMap['create_generation_asset_review']['requestBody']
  >,
  create_generation_draft: VALIDATOR_FUNCTIONS[
    'create_generation_draft_request'
  ] as ValidateFunction<
    DomainOperationMap['create_generation_draft']['requestBody']
  >,
  create_generation_export_package: VALIDATOR_FUNCTIONS[
    'create_generation_export_package_request'
  ] as ValidateFunction<
    DomainOperationMap['create_generation_export_package']['requestBody']
  >,
  create_generation_regeneration_draft: VALIDATOR_FUNCTIONS[
    'create_generation_regeneration_draft_request'
  ] as ValidateFunction<
    DomainOperationMap['create_generation_regeneration_draft']['requestBody']
  >,
  create_generation_review_summary_regeneration: VALIDATOR_FUNCTIONS[
    'create_generation_review_summary_regeneration_request'
  ] as ValidateFunction<
    DomainOperationMap['create_generation_review_summary_regeneration']['requestBody']
  >,
  create_generation_run_recovery_preview: VALIDATOR_FUNCTIONS[
    'create_generation_run_recovery_preview_request'
  ] as ValidateFunction<
    DomainOperationMap['create_generation_run_recovery_preview']['requestBody']
  >,
  create_library_entry: VALIDATOR_FUNCTIONS[
    'create_library_entry_request'
  ] as ValidateFunction<
    DomainOperationMap['create_library_entry']['requestBody']
  >,
  create_library_internal_entry: VALIDATOR_FUNCTIONS[
    'create_library_internal_entry_request'
  ] as ValidateFunction<
    DomainOperationMap['create_library_internal_entry']['requestBody']
  >,
  create_library_reference_object: VALIDATOR_FUNCTIONS[
    'create_library_reference_object_request'
  ] as ValidateFunction<
    DomainOperationMap['create_library_reference_object']['requestBody']
  >,
  create_localization_batch: VALIDATOR_FUNCTIONS[
    'create_localization_batch_request'
  ] as ValidateFunction<
    DomainOperationMap['create_localization_batch']['requestBody']
  >,
  create_marketing_evidence_handoff: VALIDATOR_FUNCTIONS[
    'create_marketing_evidence_handoff_request'
  ] as ValidateFunction<
    DomainOperationMap['create_marketing_evidence_handoff']['requestBody']
  >,
  create_marketing_handoff: VALIDATOR_FUNCTIONS[
    'create_marketing_handoff_request'
  ] as ValidateFunction<
    DomainOperationMap['create_marketing_handoff']['requestBody']
  >,
  create_marketing_image_edit: VALIDATOR_FUNCTIONS[
    'create_marketing_image_edit_request'
  ] as ValidateFunction<
    DomainOperationMap['create_marketing_image_edit']['requestBody']
  >,
  create_marketing_project: VALIDATOR_FUNCTIONS[
    'create_marketing_project_request'
  ] as ValidateFunction<
    DomainOperationMap['create_marketing_project']['requestBody']
  >,
  create_meta_binding: VALIDATOR_FUNCTIONS[
    'create_meta_binding_request'
  ] as ValidateFunction<
    DomainOperationMap['create_meta_binding']['requestBody']
  >,
  create_meta_connection: VALIDATOR_FUNCTIONS[
    'create_meta_connection_request'
  ] as ValidateFunction<
    DomainOperationMap['create_meta_connection']['requestBody']
  >,
  create_meta_contract: VALIDATOR_FUNCTIONS[
    'create_meta_contract_request'
  ] as ValidateFunction<
    DomainOperationMap['create_meta_contract']['requestBody']
  >,
  create_meta_launch_preview: VALIDATOR_FUNCTIONS[
    'create_meta_launch_preview_request'
  ] as ValidateFunction<
    DomainOperationMap['create_meta_launch_preview']['requestBody']
  >,
  create_product_intake: VALIDATOR_FUNCTIONS[
    'create_product_intake_request'
  ] as ValidateFunction<
    DomainOperationMap['create_product_intake']['requestBody']
  >,
  create_results_import: VALIDATOR_FUNCTIONS[
    'create_results_import_request'
  ] as ValidateFunction<
    DomainOperationMap['create_results_import']['requestBody']
  >,
  create_scale_activation_preview: VALIDATOR_FUNCTIONS[
    'create_scale_activation_preview_request'
  ] as ValidateFunction<
    DomainOperationMap['create_scale_activation_preview']['requestBody']
  >,
  create_scale_advice_disposition: VALIDATOR_FUNCTIONS[
    'create_scale_advice_disposition_request'
  ] as ValidateFunction<
    DomainOperationMap['create_scale_advice_disposition']['requestBody']
  >,
  create_scale_advice_snooze: VALIDATOR_FUNCTIONS[
    'create_scale_advice_snooze_request'
  ] as ValidateFunction<
    DomainOperationMap['create_scale_advice_snooze']['requestBody']
  >,
  create_scale_enrollment: VALIDATOR_FUNCTIONS[
    'create_scale_enrollment_request'
  ] as ValidateFunction<
    DomainOperationMap['create_scale_enrollment']['requestBody']
  >,
  create_scale_enrollment_candidate: VALIDATOR_FUNCTIONS[
    'create_scale_enrollment_candidate_request'
  ] as ValidateFunction<
    DomainOperationMap['create_scale_enrollment_candidate']['requestBody']
  >,
  create_scale_export: VALIDATOR_FUNCTIONS[
    'create_scale_export_request'
  ] as ValidateFunction<
    DomainOperationMap['create_scale_export']['requestBody']
  >,
  create_scale_target: VALIDATOR_FUNCTIONS[
    'create_scale_target_request'
  ] as ValidateFunction<
    DomainOperationMap['create_scale_target']['requestBody']
  >,
  create_scale_target_publication_preview: VALIDATOR_FUNCTIONS[
    'create_scale_target_publication_preview_request'
  ] as ValidateFunction<
    DomainOperationMap['create_scale_target_publication_preview']['requestBody']
  >,
  create_scale_target_withdrawal_preview: VALIDATOR_FUNCTIONS[
    'create_scale_target_withdrawal_preview_request'
  ] as ValidateFunction<
    DomainOperationMap['create_scale_target_withdrawal_preview']['requestBody']
  >,
  create_test_plan: VALIDATOR_FUNCTIONS[
    'create_test_plan_request'
  ] as ValidateFunction<DomainOperationMap['create_test_plan']['requestBody']>,
  create_test_plan_run: VALIDATOR_FUNCTIONS[
    'create_test_plan_run_request'
  ] as ValidateFunction<
    DomainOperationMap['create_test_plan_run']['requestBody']
  >,
  create_upload_intent: VALIDATOR_FUNCTIONS[
    'create_upload_intent_request'
  ] as ValidateFunction<
    DomainOperationMap['create_upload_intent']['requestBody']
  >,
  decide_competitor_discovery: VALIDATOR_FUNCTIONS[
    'decide_competitor_discovery_request'
  ] as ValidateFunction<
    DomainOperationMap['decide_competitor_discovery']['requestBody']
  >,
  edit_marketing_project: VALIDATOR_FUNCTIONS[
    'edit_marketing_project_request'
  ] as ValidateFunction<
    DomainOperationMap['edit_marketing_project']['requestBody']
  >,
  execute_meta_launch_preview: VALIDATOR_FUNCTIONS[
    'execute_meta_launch_preview_request'
  ] as ValidateFunction<
    DomainOperationMap['execute_meta_launch_preview']['requestBody']
  >,
  flip_competitor_ad_relevance: VALIDATOR_FUNCTIONS[
    'flip_competitor_ad_relevance_request'
  ] as ValidateFunction<
    DomainOperationMap['flip_competitor_ad_relevance']['requestBody']
  >,
  fork_library_version: VALIDATOR_FUNCTIONS[
    'fork_library_version_request'
  ] as ValidateFunction<
    DomainOperationMap['fork_library_version']['requestBody']
  >,
  freeze_experiment: VALIDATOR_FUNCTIONS[
    'freeze_experiment_request'
  ] as ValidateFunction<DomainOperationMap['freeze_experiment']['requestBody']>,
  freeze_marketing_round: VALIDATOR_FUNCTIONS[
    'freeze_marketing_round_request'
  ] as ValidateFunction<
    DomainOperationMap['freeze_marketing_round']['requestBody']
  >,
  import_library_manual_facts: VALIDATOR_FUNCTIONS[
    'import_library_manual_facts_request'
  ] as ValidateFunction<
    DomainOperationMap['import_library_manual_facts']['requestBody']
  >,
  judge_experiment: VALIDATOR_FUNCTIONS[
    'judge_experiment_request'
  ] as ValidateFunction<DomainOperationMap['judge_experiment']['requestBody']>,
  pause_scale_enrollment: VALIDATOR_FUNCTIONS[
    'pause_scale_enrollment_request'
  ] as ValidateFunction<
    DomainOperationMap['pause_scale_enrollment']['requestBody']
  >,
  prepare_marketing_feedback: VALIDATOR_FUNCTIONS[
    'prepare_marketing_feedback_request'
  ] as ValidateFunction<
    DomainOperationMap['prepare_marketing_feedback']['requestBody']
  >,
  prepare_marketing_generation: VALIDATOR_FUNCTIONS[
    'prepare_marketing_generation_request'
  ] as ValidateFunction<
    DomainOperationMap['prepare_marketing_generation']['requestBody']
  >,
  preview_competitor_scan: VALIDATOR_FUNCTIONS[
    'preview_competitor_scan_request'
  ] as ValidateFunction<
    DomainOperationMap['preview_competitor_scan']['requestBody']
  >,
  preview_library_template_reapply: VALIDATOR_FUNCTIONS[
    'preview_library_template_reapply_request'
  ] as ValidateFunction<
    DomainOperationMap['preview_library_template_reapply']['requestBody']
  >,
  preview_marketing_metrics: VALIDATOR_FUNCTIONS[
    'preview_marketing_metrics_request'
  ] as ValidateFunction<
    DomainOperationMap['preview_marketing_metrics']['requestBody']
  >,
  preview_spend_guardrail: VALIDATOR_FUNCTIONS[
    'preview_spend_guardrail_request'
  ] as ValidateFunction<
    DomainOperationMap['preview_spend_guardrail']['requestBody']
  >,
  promote_library_reference_object: VALIDATOR_FUNCTIONS[
    'promote_library_reference_object_request'
  ] as ValidateFunction<
    DomainOperationMap['promote_library_reference_object']['requestBody']
  >,
  publish_library_entry: VALIDATOR_FUNCTIONS[
    'publish_library_entry_request'
  ] as ValidateFunction<
    DomainOperationMap['publish_library_entry']['requestBody']
  >,
  reconcile_competitor_scan: VALIDATOR_FUNCTIONS[
    'reconcile_competitor_scan_request'
  ] as ValidateFunction<
    DomainOperationMap['reconcile_competitor_scan']['requestBody']
  >,
  reconcile_generation_run: VALIDATOR_FUNCTIONS[
    'reconcile_generation_run_request'
  ] as ValidateFunction<
    DomainOperationMap['reconcile_generation_run']['requestBody']
  >,
  recover_meta_launch_operation: VALIDATOR_FUNCTIONS[
    'recover_meta_launch_operation_request'
  ] as ValidateFunction<
    DomainOperationMap['recover_meta_launch_operation']['requestBody']
  >,
  resolve_competitor: VALIDATOR_FUNCTIONS[
    'resolve_competitor_request'
  ] as ValidateFunction<
    DomainOperationMap['resolve_competitor']['requestBody']
  >,
  resolve_marketing_call: VALIDATOR_FUNCTIONS[
    'resolve_marketing_call_request'
  ] as ValidateFunction<
    DomainOperationMap['resolve_marketing_call']['requestBody']
  >,
  resume_generation_run_paused_items: VALIDATOR_FUNCTIONS[
    'resume_generation_run_paused_items_request'
  ] as ValidateFunction<
    DomainOperationMap['resume_generation_run_paused_items']['requestBody']
  >,
  resume_scale_enrollment: VALIDATOR_FUNCTIONS[
    'resume_scale_enrollment_request'
  ] as ValidateFunction<
    DomainOperationMap['resume_scale_enrollment']['requestBody']
  >,
  retire_meta_connection: VALIDATOR_FUNCTIONS[
    'retire_meta_connection_request'
  ] as ValidateFunction<
    DomainOperationMap['retire_meta_connection']['requestBody']
  >,
  retry_generation_run_failed_items: VALIDATOR_FUNCTIONS[
    'retry_generation_run_failed_items_request'
  ] as ValidateFunction<
    DomainOperationMap['retry_generation_run_failed_items']['requestBody']
  >,
  reuse_marketing_creative: VALIDATOR_FUNCTIONS[
    'reuse_marketing_creative_request'
  ] as ValidateFunction<
    DomainOperationMap['reuse_marketing_creative']['requestBody']
  >,
  review_marketing_creative: VALIDATOR_FUNCTIONS[
    'review_marketing_creative_request'
  ] as ValidateFunction<
    DomainOperationMap['review_marketing_creative']['requestBody']
  >,
  revise_test_plan: VALIDATOR_FUNCTIONS[
    'revise_test_plan_request'
  ] as ValidateFunction<DomainOperationMap['revise_test_plan']['requestBody']>,
  revoke_credential: VALIDATOR_FUNCTIONS[
    'revoke_credential_request'
  ] as ValidateFunction<DomainOperationMap['revoke_credential']['requestBody']>,
  rotate_credential: VALIDATOR_FUNCTIONS[
    'rotate_credential_request'
  ] as ValidateFunction<DomainOperationMap['rotate_credential']['requestBody']>,
  save_library_draft: VALIDATOR_FUNCTIONS[
    'save_library_draft_request'
  ] as ValidateFunction<
    DomainOperationMap['save_library_draft']['requestBody']
  >,
  save_product_brand_kit: VALIDATOR_FUNCTIONS[
    'save_product_brand_kit_request'
  ] as ValidateFunction<
    DomainOperationMap['save_product_brand_kit']['requestBody']
  >,
  start_experiment: VALIDATOR_FUNCTIONS[
    'start_experiment_request'
  ] as ValidateFunction<DomainOperationMap['start_experiment']['requestBody']>,
  start_localization_preparation: VALIDATOR_FUNCTIONS[
    'start_localization_preparation_request'
  ] as ValidateFunction<
    DomainOperationMap['start_localization_preparation']['requestBody']
  >,
  start_marketing_ai_qc: VALIDATOR_FUNCTIONS[
    'start_marketing_ai_qc_request'
  ] as ValidateFunction<
    DomainOperationMap['start_marketing_ai_qc']['requestBody']
  >,
  start_marketing_explanation: VALIDATOR_FUNCTIONS[
    'start_marketing_explanation_request'
  ] as ValidateFunction<
    DomainOperationMap['start_marketing_explanation']['requestBody']
  >,
  start_marketing_planning: VALIDATOR_FUNCTIONS[
    'start_marketing_planning_request'
  ] as ValidateFunction<
    DomainOperationMap['start_marketing_planning']['requestBody']
  >,
  submit_generation_run: VALIDATOR_FUNCTIONS[
    'submit_generation_run_request'
  ] as ValidateFunction<
    DomainOperationMap['submit_generation_run']['requestBody']
  >,
  submit_localization_claim_decisions: VALIDATOR_FUNCTIONS[
    'submit_localization_claim_decisions_request'
  ] as ValidateFunction<
    DomainOperationMap['submit_localization_claim_decisions']['requestBody']
  >,
  supersede_meta_binding: VALIDATOR_FUNCTIONS[
    'supersede_meta_binding_request'
  ] as ValidateFunction<
    DomainOperationMap['supersede_meta_binding']['requestBody']
  >,
  update_experiment_context: VALIDATOR_FUNCTIONS[
    'update_experiment_context_request'
  ] as ValidateFunction<
    DomainOperationMap['update_experiment_context']['requestBody']
  >,
  update_experiment_entry: VALIDATOR_FUNCTIONS[
    'update_experiment_entry_request'
  ] as ValidateFunction<
    DomainOperationMap['update_experiment_entry']['requestBody']
  >,
  update_generation_defaults: VALIDATOR_FUNCTIONS[
    'update_generation_defaults_request'
  ] as ValidateFunction<
    DomainOperationMap['update_generation_defaults']['requestBody']
  >,
  update_generation_draft: VALIDATOR_FUNCTIONS[
    'update_generation_draft_request'
  ] as ValidateFunction<
    DomainOperationMap['update_generation_draft']['requestBody']
  >,
  update_product_competitor_settings: VALIDATOR_FUNCTIONS[
    'update_product_competitor_settings_request'
  ] as ValidateFunction<
    DomainOperationMap['update_product_competitor_settings']['requestBody']
  >,
  update_product_reference: VALIDATOR_FUNCTIONS[
    'update_product_reference_request'
  ] as ValidateFunction<
    DomainOperationMap['update_product_reference']['requestBody']
  >,
  withdraw_library_version: VALIDATOR_FUNCTIONS[
    'withdraw_library_version_request'
  ] as ValidateFunction<
    DomainOperationMap['withdraw_library_version']['requestBody']
  >,
  withdraw_scale_target: VALIDATOR_FUNCTIONS[
    'withdraw_scale_target_request'
  ] as ValidateFunction<
    DomainOperationMap['withdraw_scale_target']['requestBody']
  >,
} satisfies RequestValidatorMap;
