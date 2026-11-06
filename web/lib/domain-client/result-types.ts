/**
 * Shared result/body types both `DomainGateway` (browser) and `DomainProxy`
 * (server) build on (spec §6.4, §6.5). Imports only the generated
 * type-only `DomainOperationMap` — safe for both runtimes.
 */
import type { DomainOperationMap } from './generated/operation-map';

export type FieldError = {
  field: string;
  code: string;
  message: string;
};

export type ProblemDetails = {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance: string;
  code: string;
  trace_id: string;
  retryable: boolean;
  field_errors: FieldError[];
};

export type ResponseMeta = {
  etag: string | null;
  retryAt: number | null;
  location: string | null;
};

type RequestBody<K extends keyof DomainOperationMap> =
  DomainOperationMap[K]['requestBody'];

/**
 * `[T] extends [never]` (wrapped in a tuple) avoids the distributive
 * conditional trap `T extends never` falls into for a bare type parameter
 * (spec §6.4).
 */
export type BodyParam<K extends keyof DomainOperationMap> = [
  RequestBody<K>,
] extends [never]
  ? { body?: never }
  : { body: RequestBody<K> };

type SuccessResponses<K extends keyof DomainOperationMap> =
  DomainOperationMap[K]['successResponses'];

type StatusKindMap = {
  200: 'ok';
  201: 'created';
  202: 'accepted';
  204: 'no_content';
  304: 'not_modified';
};

type SuccessVariants<K extends keyof DomainOperationMap> = {
  [S in keyof SuccessResponses<K> & number]: {
    kind: S extends keyof StatusKindMap ? StatusKindMap[S] : `status_${S}`;
    status: S;
    data: SuccessResponses<K>[S];
    meta: ResponseMeta;
  };
}[keyof SuccessResponses<K> & number];

/**
 * Narrows the result union to exactly the success statuses `K` declares
 * (spec §6.5) — `get_identity_probe` resolves to only `{ kind: "ok"; status:
 * 200; ... } | { kind: "problem"; ... }`, never a `201`/`202`/`204`/`304`
 * variant a future operation might add.
 */
export type DomainResultFor<K extends keyof DomainOperationMap> =
  | SuccessVariants<K>
  | {
      kind: 'problem';
      status: number;
      problem: ProblemDetails;
      meta: ResponseMeta;
    };

/**
 * The `data` payload of a `DomainResultFor<K>` once the `problem` variant is
 * excluded — the shape domain hooks (`useDomainQuery`, `useDomainCommand`)
 * expose to callers as `data`/`result`, so a hook consumer never has to
 * narrow on `kind` itself.
 */
export type SuccessDataFor<K extends keyof DomainOperationMap> = Exclude<
  DomainResultFor<K>,
  { kind: 'problem' }
>['data'];

/**
 * Normalized three-way outcome returned by `useDomainCommand.execute()` and
 * `retry()`. Collapses every `DomainResultFor` success variant (200/201/202/
 * 204/304) into a single `'success'` kind so callers await one discriminant
 * instead of pattern-matching status codes after a mutation.
 */
export type CommandOutcome<K extends keyof DomainOperationMap> =
  | { kind: 'success'; data: SuccessDataFor<K>; meta: ResponseMeta }
  | { kind: 'problem'; problem: ProblemDetails; meta: ResponseMeta }
  | {
      kind: 'reconciliation';
      actionInstanceId: string;
      staleSince: number;
    };
