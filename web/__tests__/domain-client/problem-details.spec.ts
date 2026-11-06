import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

import {
  parseProblemDetails,
  problemDetails,
  writeProblemResponse,
} from '../../lib/domain-client/problem-details';

describe('problemDetails', () => {
  it('builds the fixed RFC 9457 shape with defaults', () => {
    expect(
      problemDetails({
        title: 'Route not found',
        status: 404,
        code: 'route_not_found',
        traceId: 'req_1',
      })
    ).toEqual({
      type: 'urn:marketai:error:route-not-found',
      title: 'Route not found',
      status: 404,
      code: 'route_not_found',
      detail: 'Route not found',
      trace_id: 'req_1',
      retryable: false,
      field_errors: [],
    });
  });

  it('honors explicit type, detail, retryable, and fieldErrors', () => {
    const result = problemDetails({
      type: 'urn:marketai:error:custom',
      title: 'Bad thing',
      status: 422,
      code: 'request_validation_failed',
      detail: 'Field x is required',
      traceId: 'req_2',
      retryable: true,
      fieldErrors: [{ field: 'x', code: 'required', detail: 'x is required' }],
    });

    expect(result).toEqual({
      type: 'urn:marketai:error:custom',
      title: 'Bad thing',
      status: 422,
      code: 'request_validation_failed',
      detail: 'Field x is required',
      trace_id: 'req_2',
      retryable: true,
      field_errors: [{ field: 'x', code: 'required', detail: 'x is required' }],
    });
  });

  it('derives the type URN from the code, replacing underscores with dashes', () => {
    const result = problemDetails({
      title: 'x',
      status: 500,
      code: 'bff_contract_misconfigured',
      traceId: 'req_3',
    });
    expect(result.type).toBe('urn:marketai:error:bff-contract-misconfigured');
  });
});

describe('writeProblemResponse', () => {
  it('writes status and a well-formed Problem Details body', () => {
    const { res } = createMocks<NextApiRequest, NextApiResponse>();

    writeProblemResponse(res, 504, 'gateway_timeout', 'req_4', true);

    expect(res.statusCode).toBe(504);
    expect(res._getJSONData()).toEqual({
      type: 'urn:marketai:error:gateway-timeout',
      title: 'gateway timeout',
      status: 504,
      code: 'gateway_timeout',
      detail: 'gateway timeout',
      trace_id: 'req_4',
      retryable: true,
      field_errors: [],
    });
  });

  it('is a no-op once headers have already been sent', () => {
    const { res } = createMocks<NextApiRequest, NextApiResponse>();
    res.status(200).json({ ok: true });

    expect(() =>
      writeProblemResponse(res, 500, 'domain_unavailable', 'req_5', true)
    ).not.toThrow();
    expect(res.statusCode).toBe(200);
    expect(res._getJSONData()).toEqual({ ok: true });
  });
});

describe('parseProblemDetails', () => {
  const valid = {
    type: 'urn:marketai:error:workspace-not-found',
    title: 'Workspace not found',
    status: 404,
    code: 'workspace_not_found',
    detail: 'No workspace with that slug',
    trace_id: 'req_6',
    retryable: false,
    field_errors: [],
  };

  it('parses a well-formed Problem Details body', () => {
    expect(parseProblemDetails(valid)).toEqual({
      type: 'urn:marketai:error:workspace-not-found',
      title: 'Workspace not found',
      status: 404,
      code: 'workspace_not_found',
      detail: 'No workspace with that slug',
      traceId: 'req_6',
      retryable: false,
      fieldErrors: [],
    });
  });

  it('parses a body carrying field errors', () => {
    const withFieldErrors = {
      ...valid,
      code: 'request_validation_failed',
      field_errors: [{ field: 'name', code: 'required', detail: 'required' }],
    };
    expect(parseProblemDetails(withFieldErrors)?.fieldErrors).toEqual([
      { field: 'name', code: 'required', detail: 'required' },
    ]);
  });

  it('fills in defaulted fields when type/detail/retryable/field_errors are absent', () => {
    const minimal = {
      title: 'Not found',
      status: 404,
      code: 'not_found',
      trace_id: 'req_7',
    };
    expect(parseProblemDetails(minimal)).toEqual({
      type: 'urn:marketai:error:not-found',
      title: 'Not found',
      status: 404,
      code: 'not_found',
      detail: 'Not found',
      traceId: 'req_7',
      retryable: false,
      fieldErrors: [],
    });
  });

  it.each([
    ['null', null],
    ['a string', 'not an object'],
    ['an array', []],
    ['missing title', { ...valid, title: undefined }],
    ['missing status', { ...valid, status: undefined }],
    ['a non-integer status', { ...valid, status: 404.5 }],
    ['missing code', { ...valid, code: undefined }],
    ['an empty code', { ...valid, code: '' }],
    ['missing trace_id', { ...valid, trace_id: undefined }],
    ['an empty trace_id', { ...valid, trace_id: '' }],
    ['a non-array field_errors', { ...valid, field_errors: 'nope' }],
    [
      'a malformed field_errors entry',
      { ...valid, field_errors: [{ field: 'x' }] },
    ],
  ])('returns null for %s', (_label, candidate) => {
    expect(parseProblemDetails(candidate)).toBeNull();
  });
});
