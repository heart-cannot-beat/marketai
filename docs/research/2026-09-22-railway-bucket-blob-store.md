# Railway Bucket capabilities for a versionless `BlobStore`

Date checked: 2026-09-22
Scope: Railway Bucket behavior needed by MarketAI's browser-upload, inspection,
commit, download, and staging-cleanup protocol

## Source policy and evidence status

This note uses Railway's official documentation for claims about Railway
Buckets and AWS's first-party S3 API documentation only as the reference
contract for the S3 operations Railway says it supports. An AWS S3 behavior is
not treated as proof that Railway implements the same optional header or
failure mode. Railway documents the broad operations, but it does not publish
an option-by-option conformance matrix for checksums, conditional requests, or
ETag/consistency semantics. Those items therefore remain **unqualified until a
live probe passes against the exact Railway Bucket used by the environment**.
[Railway Storage Buckets](https://docs.railway.com/storage-buckets)

No live Railway credentials were available during this documentation review,
so this note does not claim that the qualification matrix below has passed.
The release service must run that matrix without printing credentials,
presigned URLs, signed form fields, or authorization headers.

## Decision

The existing version-aware S3 adapter cannot be pointed at Railway Bucket.
Railway explicitly does not support object versioning or bucket lifecycle
configuration, while MarketAI's existing adapter uses a `VersionId` as the
inspected storage revision and delegates stale staging cleanup to lifecycle
rules. Railway does document object copy, presigned URLs, and the ordinary
object operations needed by a separate adapter. [Railway Storage Buckets — S3
compatibility](https://docs.railway.com/storage-buckets#s3-compatibility)

The implemented adapter deliberately does **not** make ETag, storage-provided
SHA-256, or conditional copy part of its correctness proof because Railway
does not document those semantics and no live credentials were available at
implementation time. Instead it uses a content-addressed relay protocol:

1. `inspect_upload` performs one complete `GetObject`, deriving metadata and
   magic bytes from that same response while computing the actual SHA-256 and
   byte count;
2. `commit_upload` performs a new complete GET into a Worker-private temporary
   file and recomputes both values;
3. if the content revision differs from inspection, it raises
   `ObjectChangedError` before any committed write;
4. if it matches, one PutObject publishes exactly that already-verified local
   snapshot; and
5. the destination is read back and compared before a `CommittedBlobRef` can
   escape. An existing different committed key is never overwritten.

This avoids the unsafe `HeadObject` followed by unconditional server-side copy
pattern. A plain unconditional copy remains forbidden because it can copy a
different latest source object. The release probe records checksum, ETag, and
conditional-copy behavior as optional booleans; those results may justify a
future optimization but cannot weaken or silently replace the relay protocol.

## Capability matrix

| Capability | Railway evidence | S3 reference behavior | MarketAI conclusion |
|---|---|---|---|
| Object PUT/GET/HEAD/DELETE/LIST/COPY | Explicitly supported | Each has a separate S3 API contract | Available at operation level; required options still need qualification |
| Presigned downloads | Explicitly supported | A presigned request is a time-limited bearer capability | Supported; keep URLs out of logs |
| Presigned browser upload | Railway documents direct presigned browser uploads and presigned URLs generally | S3 presigned PUT/POST are time-limited bearer capabilities | Adapter uses PUT; release executes the exact generated grant against the deployed bucket |
| POST content type and length policy | Railway's example uses exact key, content-type prefix, and `content-length-range` conditions | S3 POST policy evaluates signed conditions | Documented by Railway |
| SHA-256 checksum field and enforcement | Not described by Railway | S3 supports additional checksums | Probed and reported, but not trusted; Worker hashes bytes itself |
| Checksum retrieval from HEAD/GET | Not described by Railway | S3 returns stored checksum only when checksum mode is enabled | Probed and reported, but not required by the relay path |
| ETag meaning | Not described by Railway | S3 defines ETag as an entity tag that changes with content, but it may or may not be an MD5 digest | Never used as a checksum or revision by the implemented adapter |
| Conditional GET/HEAD | Not described by Railway | S3 `If-Match` returns the object only for a matching ETag and otherwise returns 412 | Must pass live stale-token test |
| Conditional source copy | Railway says copy is supported but does not mention `x-amz-copy-source-if-match` | S3 copies only when the source ETag matches | Live probe reports stale-source behavior; current commit path does not call CopyObject |
| Conditional destination copy | Not described by Railway | S3 `If-None-Match: *` creates only when the destination key is absent; conflicts return 409/412 | Not relied upon; an existing different committed object fails closed |
| Atomic key updates / strong consistency | Not described by Railway | S3 says a completed single-key PUT is atomic and reads are strongly consistent | Read-after-write is a release gate; unverifiable publication returns `BlobStoreError` |
| Object versioning | Explicitly unsupported | An unversioned S3 destination returns no usable version ID | Never send or require `VersionId` |
| Bucket lifecycle | Explicitly unsupported | S3 lifecycle would otherwise expire objects automatically | Application owns staging cleanup |
| Object lock | Explicitly unsupported | S3 Object Lock can prevent mutation/deletion | Cannot be part of the safety argument |
| Bucket snapshots/backups | Railway says it does not currently offer automatic bucket backups or snapshots | N/A | Recovery/backup is an application/operations responsibility |
| Private networking | Railway says buckets are currently accessible only through public networking | N/A | Use authenticated TLS outbound access; do not claim a `railway.internal` bucket endpoint |

Railway sources for the matrix: [Storage Buckets](https://docs.railway.com/storage-buckets)
and [Uploading & Serving Files](https://docs.railway.com/storage-buckets/uploading-serving).
S3 sources: [POST Object](https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectPOST.html),
[POST Policy](https://docs.aws.amazon.com/AmazonS3/latest/API/sigv4-HTTPPOSTConstructPolicy.html),
[HeadObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html),
[GetObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html),
[CopyObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CopyObject.html),
[Object/ETag](https://docs.aws.amazon.com/AmazonS3/latest/API/API_Object.html), and
[S3 consistency model](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html#ConsistencyModel).

## Presigning and upload integrity

Railway's current guide explicitly generates a presigned POST with the AWS SDK,
uses a server-selected key, constrains content type, and applies a
`content-length-range` condition. That matches MarketAI's existing
`PresignedPostGrant` shape and avoids routing binary data through FastAPI.
[Railway Uploading & Serving Files — Upload files with presigned URLs](https://docs.railway.com/storage-buckets/uploading-serving#upload-files-with-presigned-urls)

A presigned upload grant is not a one-time token. Until it expires, the holder
can submit another request allowed by the same policy to the same key. AWS's
unversioned POST contract states that concurrent writes leave only the last
stored version. MarketAI must therefore assume a staging key can be replaced
after inspection even when the first upload was valid. [AWS POST
Object](https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectPOST.html)

AWS's POST API additionally defines `x-amz-checksum-algorithm` and
`x-amz-checksum-sha256` form fields. For SHA-256, the algorithm field is
`SHA256` and the checksum is the base64-encoded 256-bit digest. AWS states that
it validates provided checksums before accepting the object and stores the
checksum with the object. These are AWS semantics, not a Railway-specific
attestation. [AWS POST Object — Additional Checksum Request Form
Fields](https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectPOST.html#RESTObjectPOST-requests-form-fields)
and [Checking object integrity for uploads](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html)

The Release service must execute the same generated upload grant and headers as
production, not treat local SDK URL generation as connectivity proof. The
implemented adapter signs the expected SHA-256 as ordinary object metadata and
then computes the actual digest from the complete uploaded body. That metadata
is diagnostic only: a client-supplied value never proves byte integrity.

The optional capability probe separately attempts a correct S3
`ChecksumSHA256` upload, a wrong-checksum upload, and
`HeadObject(ChecksumMode="ENABLED")`. The release output records only booleans.
Missing support does not weaken the application hash; a surprising core
upload/read-back result aborts release. S3 requires checksum mode to retrieve
stored checksum values, while Railway does not document this option. [AWS
HeadObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html)

Do not confuse the SigV4 payload-signing value (`x-amz-content-sha256`) with the
stored additional checksum (`x-amz-checksum-sha256`). The latter is the value
MarketAI compares with the checksum persisted on the upload intent. AWS also
documents additional checksums for SigV4 presigned PUT URLs, but Railway's
first-party browser example is POST, so POST is the better-supported route for
MarketAI's size policy. [AWS presigned URL checksum support](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html)

## ETag semantics and coherent inspection

AWS says an ETag may or may not be an MD5 digest; multipart uploads in
particular do not have a whole-object MD5 ETag. Code must not derive a SHA-256
claim from it. Railway does not document whether an overwrite always changes
ETag, so the implemented adapter does not store ETag as `storage_revision` at
all. [AWS Object — ETag](https://docs.aws.amazon.com/AmazonS3/latest/API/API_Object.html)
and [Checking object integrity for uploads](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html)

There is still an inspection race to avoid: HEAD followed by an unconditional
range GET can combine metadata from one object with magic bytes from another.
The adapter instead uses a single complete GetObject response for declared
metadata, magic bytes, byte count, and SHA-256. Its opaque revision is derived
from that observed size and digest. A later overwrite simply causes commit's
second full digest to differ and fail before publication.

The release probe still writes equal-length fixtures A and B, captures their
ETags, and tests stale `CopySourceIfMatch`. These are evidence about the exact
deployed Railway implementation, not preconditions silently assumed by the
adapter. AWS documents the conditional behavior used by the probe, while
Railway documents only GET/COPY at the operation level. [AWS
GetObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html)

## Versionless commit protocol

For objects within MarketAI's current size limits (at most 500 MiB), commit
uses bounded Worker temporary disk rather than an unqualified direct copy:

1. compare the caller's expected revision, length, and SHA-256 with the
   `InspectedUpload` before I/O;
2. issue a fresh GetObject for the staging key, streaming its complete body to
   a private temporary file while computing byte count and SHA-256;
3. raise `RevisionGoneError` if absent, or `ObjectChangedError` if either value
   differs; do not write a destination in either case;
4. if the destination already exists, hash it and accept only an exact match,
   otherwise raise `BlobStoreError` without overwriting;
5. rewind the verified snapshot and issue one PutObject to the committed key;
6. immediately read the destination and require the same size and SHA-256;
   otherwise best-effort delete it and return `BlobStoreError`; and
7. return a content-revision `CommittedBlobRef` only after verification.

The second GET is not a re-inspection that silently updates expectations: its
bytes must match the already-inspected digest. An overwrite with equal length
but different content is therefore rejected. A replacement with the exact same
SHA-256 is byte-identical for this protocol's cryptographic identity.

The single PutObject carries only the already-verified local snapshot, so the
source can change again without changing what is being uploaded. The adapter
depends on the baseline S3-compatible promise that a successful PutObject
stores the request body as one object; it does not depend on a separate
HEAD-to-COPY race. A transport failure, read-back mismatch, existing different
destination, or any unverifiable result returns `BlobStoreError` and never a
committed reference.

Conditional CopyObject remains a potential bandwidth optimization. AWS
documents `CopySourceIfMatch` and destination `If-None-Match`, but Railway does
not. The release probe tests a stale source and reports whether it fails with no
destination. The production path does not call CopyObject until a separate ADR
elects that optimization using accumulated live evidence. [AWS
CopyObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CopyObject.html)

## Live release qualification matrix

`scripts.verify_railway_bucket` runs against a random, release-scoped prefix in
every Railway environment when `railway_bucket` is selected. It uses small
non-secret fixtures `A` and `B` with different SHA-256 digests. Core relay-path
checks abort the release; undocumented optional features are reported as
booleans and are never substituted into the safety argument.

| Probe | Expected result | Release treatment |
|---|---|---|
| Execute the production-generated presigned PUT and inspect the stored object | Actual size and SHA-256 match | Core gate; failure aborts |
| Commit the inspected upload through the production relay path, read it back, create a presigned download, delete it, and prove absence | All operations converge | Core gate; failure aborts |
| Put `A` with S3 `ChecksumSHA256`, then HEAD with checksum mode | Exact checksum round-trips | Optional boolean |
| Put `B` using `A`'s checksum | Request rejected and key absent | Optional boolean |
| Put `A`, overwrite with `B`, compare ETags | Both present and different | Optional boolean; ETag is never treated as a digest |
| Copy overwritten source using stale `CopySourceIfMatch` and create-only destination | 412/precondition failure; destination absent | Optional boolean; false keeps CopyObject disabled |
| Delete all canaries again in `finally` | Absence is success | Mandatory cleanup behavior |

The probe emits only a connectivity status plus named booleans. It must not
emit bucket names, credentials, authorization headers, POST fields, secret
headers, endpoints, or full presigned URLs. A stale conditional copy that
returns success is recorded as unsupported and cannot enable a copy-based
commit path.

## Application-owned staging cleanup

Railway explicitly does not support bucket lifecycle configuration, so expiry
of an upload intent does not remove its object. The application must own a
durable sweeper driven by persisted upload-intent state, not by an in-memory
timer. [Railway Storage Buckets — unsupported features](https://docs.railway.com/storage-buckets#s3-compatibility)

A safe cleanup policy, implemented for terminal intents, is:

1. the same transaction that marks an intent `finalized`, `rejected`, or
   `expired` enqueues one idempotency-keyed `cleanup_staging_upload` Job;
2. the Job carries the persisted intent expiry as `not_before`, because a
   presigned upload capability can recreate the same key until it expires;
3. the normal durable queue lease/CAS mechanism defers the Job until that time
   and marks it safe to retry after a Worker crash;
4. the handler deletes the unique server-generated staging key; absence is the
   desired state and is treated as success; and
5. a storage failure persists a retryable Job outcome with bounded backoff.

The remaining operational responsibility is periodic reconciliation of old
`staging/` keys against UploadIntent records, covering an object uploaded for
an intent that was never finalized or revisited after expiry. Ambiguous orphans
must be alerted/quarantined before deletion. Railway has no lifecycle rule or
automatic inventory process that performs this duty.

Waiting beyond capability expiry prevents an old browser grant from recreating
the object after cleanup. Server-generated unique staging keys must never be
reused for a later intent. If conditional delete by ETag is added, it too must
be live-qualified because Railway only documents DELETE at the operation
level. AWS's `DeleteObject` supports `If-Match`, but that is not a Railway
guarantee. [AWS DeleteObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObject.html)

## Configuration, networking, and operational implications

Railway supplies `BUCKET`, `ACCESS_KEY_ID`, `SECRET_ACCESS_KEY`, `REGION`, and
`ENDPOINT` as referenceable bucket variables. `BUCKET` is the globally unique
S3 API bucket name; `RAILWAY_BUCKET_NAME` is only the display name and must not
be sent to the S3 API. New buckets generally use virtual-hosted-style URLs,
while older buckets can require path-style; the Credentials tab is the
authority for the deployed instance. [Railway Storage Buckets — Connecting to
your bucket](https://docs.railway.com/storage-buckets#connecting-to-your-bucket)

Each Railway environment receives a separate bucket instance with isolated
credentials. Use variable references so staging services receive only the
staging bucket and production services receive only the production bucket.
[Railway Storage Buckets — Buckets in environments](https://docs.railway.com/storage-buckets#buckets-in-environments)

Railway's FAQ says bucket access currently uses **public networking only**.
"Private bucket" means authenticated object access and no public-bucket mode;
it does not mean a `*.railway.internal` endpoint. API, Worker, and Release may
make authenticated outbound TLS calls to the Railway-provided endpoint without
adding a public domain or inbound listener to those services. Web must receive
only short-lived presigned capabilities, never bucket credentials. This
corrects any architecture text that calls Railway Bucket a private-network
attachment. [Railway Storage Buckets — FAQ](https://docs.railway.com/storage-buckets#faq)

Bucket credentials are not database schema-owner credentials. The Release
service can keep its existing exclusive schema-owner role while also receiving
the environment's bucket variables for the capability probe; API and Worker
receive only their existing runtime database roles plus the minimum bucket
credentials required by their adapter. Nothing in this storage design requires
opening PostgreSQL, Worker, or Release to public ingress.

Railway says buckets have no automatic backups or snapshots. Committed-object
backup/RPO requirements therefore need a separate export or replication plan;
the 52-hour restoration window for deletion of an entire bucket is not an
object-level backup mechanism. [Railway Storage Buckets — FAQ and deleting a
bucket](https://docs.railway.com/storage-buckets#deleting-a-bucket)

## Open uncertainties that must remain explicit

- Railway does not document SHA-256 upload fields, validation errors, or
  checksum retrieval. The release probe records both positive and negative
  behavior; application-computed SHA-256 remains authoritative.
- Railway does not document `If-Match` for GET/HEAD/DELETE,
  `CopySourceIfMatch`, destination `IfNoneMatch`, 409/412 behavior, or copy
  atomicity. A false stale-copy result keeps CopyObject out of the commit path.
- Railway does not document its ETag construction or strong-consistency model.
  ETag is evidence only; immediate post-write read-back is a core release gate.
- Railway documents multipart uploads, but MarketAI's current maximum upload is
  below S3's 5 GB single-copy limit. If multipart browser upload is introduced,
  its checksum type and ETag behavior require a separate qualification path.
- Railway documentation and behavior can change. Re-run the probe on a new
  bucket, after credential/endpoint migration, and before accepting a changed
  storage SDK or provider behavior in production.
