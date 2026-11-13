/**
 * A refusal a control-plane route answers with its HTTP status.
 *
 * `message` is the route's English diagnostic. A refusal the page has to show
 * in the reader's language also carries a stable `code`, rendered as the
 * `error-<code>` copy, and the `params` that copy interpolates (#157).
 */
export class ApiError extends Error {
  status: number;
  code?: string;
  params?: Record<string, unknown>;

  constructor(
    status: number,
    message: string,
    coded?: { code: string; params?: Record<string, unknown> }
  ) {
    super(message);
    this.status = status;
    this.code = coded?.code;
    this.params = coded?.params;
  }
}

/**
 * The `error` body a route answers a failure with. Only an `ApiError`'s own
 * code is passed on: other errors (a Prisma `P2002`, say) carry a `code` of
 * their own that is not one of the page's codes.
 */
export const apiErrorBody = (
  error: unknown
): { message: string; code?: string; params?: Record<string, unknown> } => {
  const message =
    (error as { message?: string } | null)?.message || 'Something went wrong';
  if (error instanceof ApiError && error.code) {
    return error.params
      ? { message, code: error.code, params: error.params }
      : { message, code: error.code };
  }
  return { message };
};
