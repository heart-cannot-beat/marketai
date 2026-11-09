# Design Section 2 (Final): Pure-Function Scale Evaluator

Status: final design section

This section defines the sole evaluation seam for Scale. It incorporates the
accepted Section 1 corrections for canonical Meta mode provenance, sealed
evidence, versioned policy and target inputs, deterministic streak lineage,
and immutable value objects. It also resolves the statistical and state-model
ambiguities in the earlier Section 2 draft.

The normative domain behavior remains in
[`docs/product/scale-advice-semantics-contract.md`](../../product/scale-advice-semantics-contract.md).
The persisted aggregate and immutable-record model is defined in
[`2026-09-08-scale-section-1-domain-model.md`](2026-09-08-scale-section-1-domain-model.md).
This section defines how that behavior becomes a deterministic implementation.

## 2.1 External seam and dependency rule

Scale exposes one evaluation interface:

```python
def evaluate_scale(snapshot: ScaleEvidenceSnapshot) -> ScaleEvaluation:
    """Return the deterministic evaluation of one sealed evidence snapshot."""
```

Callers and tests cross this same seam. The evaluator:

- receives immutable domain value objects, never SQLAlchemy records;
- performs no database, network, filesystem, environment, logging, or clock
  access;
- creates no Jobs, AdviceThreads, notifications, drafts, or audit rows;
- reads no mutable process state and mutates no input;
- returns a value or raises a deterministic `InvalidScaleSnapshot` with a
  stable reason code;
- uses the immutable Policy's algorithm version to select a registered
  implementation, never a caller-supplied label, and rejects an unregistered or
  incompatible Policy. New runs always freeze `CURRENT_ALGORITHM_VERSION` from
  the deployed evaluator package.

The implementation may be divided into private modules such as
`app.scale._statistics` and `app.scale._canonical`, but the public interface
remains `evaluate_scale()` in `app/scale/evaluator.py`.

`evaluate_scale()` owns a private immutable registry from algorithm version to
implementation. The registry preserves the one external interface while
allowing a durable run or retained sealed snapshot to replay under its frozen
version after a deployment. Every version remains registered while any
non-terminal run or replayable retained snapshot references it. Removing a
version requires an explicit retention/archive migration and a database proof
that no replayable record depends on it.

Allowed dependencies are Python standard-library value and arithmetic modules
such as `dataclasses`, `datetime.date`, `datetime.datetime`, `decimal`, `enum`,
`hashlib`, `json`, and `typing`, plus Scale's own frozen contracts. Importing
date/time value types does not grant clock access. Calls such as
`datetime.now()`, `date.today()`,
`time.time()`, use of `os.environ`, the module-global random generator, and all
imports from persistence, configuration, adapters, or store modules are
forbidden.

An AST-based test enforces the dependency rule. A text grep is insufficient
because aliases and indirect imports can bypass it.

## 2.2 Immutable input contracts

All public input types are frozen dataclasses whose members are themselves
immutable. Flexible dictionaries and `MappingProxyType` are not part of the
interface. Open-ended evidence is represented by a canonically sorted tuple of
typed key/value records. No float is accepted anywhere in a snapshot.

Domain states use enums rather than unconstrained strings. At minimum these
include `MetaMode`, `EvidenceMaturity`, `DataQualityState`, `ScaleHealth`,
`SignalState`, `AdviceType`, `BreakdownType`, `WindowRole`, and
`DeliveryDiagnosticState`. `PriorStreakAbsenceReason` is also typed; a missing
predecessor cannot be represented by an unexplained `None`.

The canonical values that cross persistence and transport are fixed here:

```python
class MetaMode(str, Enum):
    CUSTOMER_SANDBOX = "customer_sandbox"
    CONTROLLED_MOCK = "controlled_mock"
    INTERNAL_VALIDATION = "internal_validation"


class EvidenceMaturity(str, Enum):
    WARMING = "warming"
    MONITORABLE = "monitorable"
    ECONOMICS_READY = "economics_ready"
    INSUFFICIENT = "insufficient"
```

`sandbox`, `mock`, `real_read_only`, and `insufficient_evidence` may appear in
human prose or historical material but are not accepted enum aliases. Rejecting
aliases keeps RLS predicates, unique keys, hashes, OpenAPI, and browser state on
one vocabulary.

### Daily resampling unit

CPA and ROAS cannot be bootstrapped from one aggregate row. The immutable
resampling unit is one complete account-local day:

```python
@dataclass(frozen=True)
class DailyMetricValues:
    impressions: int
    link_clicks: int
    purchases: Decimal
    spend: Decimal
    purchase_value: Decimal
    delivered: bool


@dataclass(frozen=True)
class DailyObservation:
    local_date: date
    evidence_state: DailyEvidenceState  # present | missing | before_episode
    metrics: DailyMetricValues | None
```

`purchases` is Decimal because attributed Meta actions may be fractional.
Impressions and link clicks remain integers. All monetary values use the
snapshot currency. `evidence_state='present'` requires metrics;
`evidence_state='missing'` requires metrics to be absent and a matching typed
gap DataQualityItem. A missing day is never represented by invented zero
metrics.

`evidence_state='before_episode'` (snapshot schema `scale-snapshot-v2`, Issue
#57) marks a window day before the Episode's `started_local_day`. It has no
metrics and no DataQualityItem: the Episode's own start explains it (product
contract §8). Such days may only open the 14-day span (baseline, then
current); the as-of day always belongs to the Episode, and breakdown days
never carry the state. Snapshots sealed as `scale-snapshot-v1` keep recording
those days as `missing` with a blocking `daily_slot_missing`, and reassemble
that way.

```python
@dataclass(frozen=True)
class MetricWindow:
    role: WindowRole
    start_date: date
    end_date_exclusive: date
    daily: tuple[DailyObservation, ...]
```

Window intervals are account-local and half-open: `[start_date,
end_date_exclusive)`. `daily` is strictly ordered by `local_date`, contains no
duplicate date, and contains exactly one record for every complete local day
in the interval. Paused and observed zero-delivery days remain present with
metrics and `delivered=False`; they are distinct from a missing-evidence slot.

The evaluator derives totals and CTR, CVR, CPC, CPM, CPA, and ROAS from the
present primitive daily observations. These ratios are not duplicated as
caller inputs. A zero denominator produces an unavailable value, never zero.
Any missing slot is blocking quality, so arithmetic and maturity classification
cannot proceed as if the incomplete window were valid.

### Exact-window evidence

Reach and frequency are never reconstructed from daily rows:

```python
@dataclass(frozen=True)
class ExactWindowMetrics:
    role: WindowRole
    start_date: date
    end_date_exclusive: date
    reach: int | None
    frequency: Decimal | None
    observation_contract_version_id: str
    evidence_hash: str
```

The snapshot contains distinct `current_7d`, `baseline_7d`, and `rolling_14d`
exact-window objects. A generic “7 or 14 days” field is not accepted because
it does not prove which semantic window the evidence represents.
Unavailable reach/frequency requires a matching typed DataQualityItem; `None`
is not interpreted as zero.

### Breakdown evidence

Each placement, country, or age/gender segment contains its stable segment
key, privacy/suppression state, and a tuple of daily observations covering the
same current window:

```python
class BreakdownEvidenceState(str, Enum):
    OBSERVED = "observed"
    PRIVACY_SUPPRESSED = "privacy_suppressed"


@dataclass(frozen=True)
class BreakdownSegment:
    breakdown_type: BreakdownType
    segment_key: str
    evidence_state: BreakdownEvidenceState
    daily: tuple[DailyObservation, ...] | None
    suppression_reason: BreakdownSuppressionReason | None
```

The shape is discriminated rather than controlled by an independent boolean:

- `observed` requires a complete current-window `daily` tuple and forbids a
  suppression reason;
- `privacy_suppressed` requires `daily is None` and a typed suppression reason;
- a suppressed segment therefore cannot retain, hash, return, or serialize
  protected metric values; and
- an unknown or omitted segment is represented by a typed DataQualityItem, not
  by manufacturing a suppressed or zero-valued segment.

Segments are canonically ordered by `(breakdown_type, segment_key)`. Placement
and country are separate statistical families. Age/gender is descriptive only
and can never produce an AdviceCandidate.

Quality evidence is also typed and immutable:

```python
CanonicalScalar = None | bool | int | str | Decimal | date | datetime


@dataclass(frozen=True)
class CanonicalKeyValue:
    key: str
    value: CanonicalScalar


class DataQualityImpact(str, Enum):
    WARNING = "warning"
    BLOCKING = "blocking"


@dataclass(frozen=True)
class DataQualityLocator:
    contract_type: MetricContractType
    window_role: WindowRole | None
    local_date: date | None
    breakdown_type: BreakdownType | None
    segment_key: str | None
    source_evidence_hash: str


@dataclass(frozen=True)
class DataQualityItem:
    code: DataQualityIssueCode
    impact: DataQualityImpact
    locator: DataQualityLocator
    details: tuple[CanonicalKeyValue, ...]
```

`details` is a canonically sorted tuple of an allowlisted typed key and scalar
value, never a free dictionary or raw Provider payload. The total issue sort key
is `(impact, code, locator canonical tuple, details canonical tuple)`. Duplicate
issues are invalid. Missing daily slots and unavailable exact-window values must
have a matching locator; unrelated generic issues cannot satisfy that invariant.

### Policy, target, contracts, and delivery state

`ScalePolicySnapshot` freezes every numerical and methodological choice used
by the evaluator, including:

- compatible algorithm version;
- Decimal precision and rounding mode;
- Wilson/Newcombe confidence level and its exact Decimal z value;
- complete-day, active-day, impression, click, purchase, and spend floors;
- frequency, CTR, CPA, ROAS, CPM, and delivery thresholds;
- required consecutive-hit counts;
- bootstrap PRNG version, draw count, minimum valid-draw ratio, and percentile
  rule;
- breakdown spend-share and sample gates;
- Benjamini-Hochberg family definition and FDR limit;
- quality-warning permissions and streak-reset reason codes;
- advice-construction values needed to construct stable candidates.

`ScaleTargetSnapshot` is optional. When present it contains the exact Target
version ID, currency, target CPA, break-even ROAS, minimum economic spend, and
any Owner thresholds that are stricter than the Policy. A target currency must
equal the snapshot currency.

`MetricContractRef` retains the Scale contract version, wrapped #23
observation-contract generation/version, contract type, contract hash,
connection, mode, scenario, timezone, and currency identities. The tuple is
ordered by contract type and contains exactly one required contract for daily,
exact-window, and delivery configuration plus one contract for every supplied
breakdown family.

`DeliveryConfigSnapshot` includes the Episode baseline configuration hash,
current configuration hash, effective campaign/ad-set/ad status, applicable
status history for the frozen windows, attribution setting, and explicit
delivery diagnostic evidence. Configuration incompatibility is represented by
a typed DataQualityItem; it is not a free boolean that can drift from the
evidence.

Delivery diagnostics distinguish at least:

- `unknown`: the contracted field was unavailable;
- `none`: the contracted field was available and reported no diagnostic;
- `learning`;
- `learning_limited`;
- another explicitly contracted diagnostic value.

`None` is not used to mean both “unknown” and “no problem”.

### Consecutive-hit lineage

```python
class PriorStreakAbsenceReason(str, Enum):
    NO_EARLIER_DISTINCT_DAY = "no_earlier_distinct_day"


@dataclass(frozen=True)
class StreakCounter:
    signal_key: str
    consecutive_hits: int


@dataclass(frozen=True)
class StreakCompatibility:
    policy_version_id: str
    target_version_id: str | None
    metric_contract_set_hash: str
    configuration_baseline_hash: str
    data_continuity_generation: str
    canonical_hash: str


@dataclass(frozen=True)
class PriorStreakState:
    evaluation_id: str
    evaluated_local_day: date
    compatibility: StreakCompatibility
    health: ScaleHealth | None
    healthy_evaluations: int
    counters: tuple[StreakCounter, ...]
```

The current compatibility value covers Policy version, Target version or the
explicit no-target marker, metric-contract-set hash, Episode configuration
baseline hash, and the data-continuity generation. Its canonical hash protects
that structured identity but does not replace it: the evaluator compares the
individual fields so the Policy can reset the affected streaks explicitly. The
snapshot builder never pre-zeros an incompatible prior state. A mismatch is
surfaced through stable reset reason codes in the Evaluation.

The snapshot builder freezes `prior_evaluation_id` and its complete
`PriorStreakState` before sealing. It must constrain the predecessor to the same
Workspace, Meta mode, Connection, Episode, and lineage branch and to an earlier
account-local evaluation day. A same-day revision cannot use another same-day
Evaluation as its predecessor. When no eligible predecessor exists, the
snapshot carries a typed `PriorStreakAbsenceReason`; absence without a reason is
invalid. Here lineage branch means live/forward-replay chronology; it does not
filter out a different Policy, Target, contract set, configuration baseline, or
continuity generation, because those structured mismatches are precisely what
the evaluator must inspect and reset. The evaluator verifies the value-level
lineage defensively.

### Complete snapshot

```python
@dataclass(frozen=True)
class ScaleEvidenceSnapshot:
    snapshot_id: str
    canonical_hash: str
    canonical_schema_version: str
    episode_id: str
    meta_mode: MetaMode
    meta_connection_id: str
    scenario_version_id: str | None
    timezone_name: str
    currency: str
    policy: ScalePolicySnapshot
    target: ScaleTargetSnapshot | None
    metric_contract_set_hash: str
    metric_contracts: tuple[MetricContractRef, ...]
    delivery_config: DeliveryConfigSnapshot
    current_7d: MetricWindow
    baseline_7d: MetricWindow
    current_exact_7d: ExactWindowMetrics
    baseline_exact_7d: ExactWindowMetrics
    rolling_exact_14d: ExactWindowMetrics
    breakdowns: tuple[BreakdownSegment, ...]
    data_quality_issues: tuple[DataQualityItem, ...]
    complete_local_days_in_episode: int
    active_delivery_days_in_episode: int
    data_continuity_generation: str
    prior_streak: PriorStreakState | None
prior_streak_absence_reason: PriorStreakAbsenceReason | None
    last_complete_local_day: date
    last_sync_at: datetime
    freshness_reference_at: datetime
    as_of_local_day: date
```

`prior_streak_absence_reason` is required exactly when `prior_streak` is absent
and must be absent when a predecessor is supplied. Compatibility resets retain
the predecessor and therefore do not use the absence reason.

`last_sync_at` and `freshness_reference_at` are offset-aware. The latter is
frozen by the durable evaluation request, not read from the clock by the
assembler. Freshness has already been verified while sealing the snapshot and
represented through DataQualityItems; the evaluator does not obtain a current
time. It derives
`has_blocking_quality_issue` from `data_quality_issues`; no duplicate boolean
is accepted.

UnobservedFactor is deliberately absent. It remains auditable Episode evidence
but never enters automated evaluation.

## 2.3 Snapshot validation and error modes

The persistence assembler is the primary integrity gate and may construct a
value object only from a sealed snapshot. The evaluator nevertheless validates
all assumptions required by its arithmetic. Invalid input raises:

```python
class InvalidScaleSnapshot(ValueError):
    code: str
```

Stable error codes cover at least:

- `snapshot_not_canonical`;
- `algorithm_policy_mismatch`;
- `algorithm_version_unavailable`;
- `invalid_window_boundary`;
- `missing_or_duplicate_local_day`;
- `invalid_or_unsorted_contract_set`;
- `invalid_or_unsorted_breakdown`;
- `currency_mismatch`;
- `negative_metric`;
- `clicks_exceed_impressions`;
- `invalid_exact_window`;
- `invalid_prior_evaluation`;
- `invalid_freshness_reference`;
- `invalid_decimal`.

Invalid input never degrades into `warming`, `healthy`, or a partial result.
Data insufficiency is valid domain evidence; structurally contradictory input
is not.

For the Beta algorithm, window validation requires:

```text
current_7d.end_date_exclusive = as_of_local_day + 1 day
current_7d length             = 7 complete local days
baseline_7d.end               = current_7d.start
baseline_7d length            = 7 complete local days
current_exact_7d bounds       = current_7d bounds
baseline_exact_7d bounds      = baseline_7d bounds
rolling_exact_14d.start       = baseline_7d.start
rolling_exact_14d.end         = current_7d.end
```

Every observed breakdown uses the current 7-day bounds. Policy window changes
require a new algorithm/canonical contract rather than silently reinterpreting
fields named `current_7d` and `baseline_7d`.

## 2.4 Deterministic arithmetic

Every evaluation runs inside a local Decimal context whose precision and
rounding mode are frozen by the Policy. Ambient process Decimal settings cannot
change a result. The implementation uses `Decimal.sqrt()` and never converts a
metric, probability, threshold, or interval to float.

Operations are evaluated in the order written by the algorithm-versioned
pseudocode; implementations may not reassociate expressions. Decimal context
rounding applies to every arithmetic operation. There is no extra quantization
before hashing. Canonical Decimal serialization removes insignificant trailing
zeros, writes zero as `0` (including negative zero), and never uses exponent
notation.

### CTR comparison

CTR is `link_clicks / impressions`. The evaluator computes Wilson intervals
for both windows and the Newcombe method-10 interval for the difference between
two independent proportions. It does not use “two intervals do not overlap”
as a hypothesis test. The implementation freezes the exact orientation as
`baseline_ctr - current_ctr`, so a positive interval means decline.

For successes `x`, trials `n`, and the Policy's exact Decimal `z`, the Wilson
interval without continuity correction is calculated in this order:

```text
p      = x / n
z2     = z * z
denom  = 1 + (z2 / n)
center = (p + (z2 / (2 * n))) / denom
radius = (z * sqrt(((p * (1 - p)) / n) + (z2 / (4 * n * n)))) / denom
lower  = max(0, center - radius)
upper  = min(1, center + radius)
```

Let `(lb, ub)` be the baseline Wilson bounds, `(lc, uc)` the current bounds,
and `d = baseline_ctr - current_ctr`. Newcombe method 10 is:

```text
d_lower = d - sqrt(((baseline_ctr - lb) ** 2) + ((uc - current_ctr) ** 2))
d_upper = d + sqrt(((ub - baseline_ctr) ** 2) + ((current_ctr - lc) ** 2))
```

The confidence interval supports decline exactly when `d_lower > 0`.

The CTR-decline signal is a hit only when:

1. baseline CTR is non-zero;
2. relative decline meets the effective Policy/Target threshold; and
3. the configured Newcombe confidence interval supports the decline direction.

A zero baseline denominator or baseline CTR of zero makes the relative signal
`unavailable`, not `miss`.

Frequency growth and every direction-normalized relative economic effect follow
the same baseline rule. With available positive baselines:

```text
frequency_growth = (current_frequency / baseline_frequency) - 1
cpa_worsening     = (current_cpa / baseline_cpa) - 1
roas_worsening    = 1 - (current_roas / baseline_roas)
```

An unavailable or zero baseline makes the relative signal `unavailable`. A hit
uses inclusive magnitude comparison (`observed >= threshold`) and strict
directional confidence (`interval_lower > 0`).

### Deterministic day-block bootstrap

Current and baseline present daily observations are resampled independently
with replacement. Each resampled day carries spend, purchases, and purchase
value together so their within-day relationship is retained. The bootstrap is
never entered when a missing slot has made either input window quality-blocked.

For each draw the evaluator consumes PRNG words in this exact order: every
baseline slot in canonical local-date order, then every current slot in
canonical local-date order. Breakdown streams consume their current-window
slots in canonical local-date order. Rejection sampling may consume additional
words but does not alter this logical order. The evaluator derives:

- CPA as `sum(spend) / sum(purchases)` when purchases are non-zero;
- ROAS as `sum(purchase_value) / sum(spend)` when spend is non-zero;
- the direction-normalized relative effect, where a positive effect always
  means worse performance.

Draws with an unavailable denominator are excluded. If the valid-draw ratio is
below the Policy floor, the signal is `unavailable`. Otherwise sort the valid
direction-normalized effects by Decimal numeric order. Beta uses 10,000 draws
and the nearest-rank interval without interpolation. For rational percentile
`a / b`, its zero-based index is `max(0, ceil(a * valid_draws / b) - 1)`.
Therefore the Beta lower and upper bounds use `25/1000` and `975/1000`.

No `random` module is used. `bootstrap-prng-v1` is a local, specified
SplitMix64 stream:

1. seed with the first unsigned 64 bits of
   `SHA-256(domain_separator || NUL || snapshot_hash || NUL || signal_key)`;
2. generate unsigned 64-bit words with the frozen SplitMix64-v1 transition;
3. select an index with rejection sampling, not `% n`, so sampling has no
   modulo bias;
4. use a separate signal-keyed stream for every signal and segment.

For v1, `domain_separator` is the UTF-8 byte string
`scale-bootstrap-prng-v1`; `snapshot_hash` is the lowercase ASCII hexadecimal
SHA-256 digest; separators are one zero byte; and `signal_key` is its canonical
UTF-8 value. The first eight digest bytes are decoded unsigned big-endian.

The frozen unsigned transition is:

```text
MASK  = 0xffffffffffffffff
state = (state + 0x9e3779b97f4a7c15) & MASK
z     = state
z     = ((z ^ (z >> 30)) * 0xbf58476d1ce4e5b9) & MASK
z     = ((z ^ (z >> 27)) * 0x94d049bb133111eb) & MASK
word  = (z ^ (z >> 31)) & MASK
```

For a population of size `n`, index selection accepts a word only when it is
less than `2**64 - (2**64 % n)`, then returns `word % n`. Seed byte order is
big-endian. These details are golden-vector tested.

Changing the PRNG, stream derivation, draw order, valid-draw rule, or percentile
rule requires a new algorithm version.

### Breakdown hypothesis and FDR

Only non-suppressed placement and country segments that pass the sample gates
enter statistical testing. For each segment and applicable economic metric,
define a direction-normalized effect against the Target where positive means
economically worse:

- CPA effect: `segment_cpa - target_cpa`;
- ROAS effect: `breakeven_roas - segment_roas`.

Let `observed_effect` be the effect from the original segment observations and
`draw_effect` each valid bootstrap effect. For the one-sided alternative
"segment is economically worse than Target":

```text
if observed_effect <= 0:
    p_value = 1
else:
    null_centered = draw_effect - observed_effect
    extreme_count = count(null_centered >= observed_effect)
    p_value = (extreme_count + 1) / (valid_draws + 1)
```

The comparison is inclusive. The uncentered nearest-rank bootstrap interval is
reported separately, and target-relative confidence is supported only when its
lower bound is strictly greater than zero. An insufficient valid-draw ratio
makes the segment signal unavailable and excludes it from the sample-eligible
family.

Benjamini-Hochberg runs over every sample-eligible segment in one family before
significance filtering. A family is one `(snapshot, breakdown_type,
economic_metric)` tuple. Ordering is `(p_value, segment_key)` so ties are
deterministic. Only a segment that passes spend share, sample floors, economic
effect, confidence, FDR, and consecutive-hit gates can contribute to
`review_delivery_mix`.

For sorted one-based rank `i` in a family of size `m`, calculate raw
`q_i = min(1, p_i * m / i)`, then walk from `m` to `1` and replace each value
with `min(q_i, q_(i+1))`. A segment passes FDR exactly when its adjusted
`q_value <= policy.fdr_limit`. All operations use the Policy Decimal context.

The output SignalResult records raw p-value and adjusted q-value when
applicable. Age/gender never enters an FDR family.

## 2.5 Evaluation pipeline

The evaluator runs these phases in a fixed order.

### Phase 0: validate and establish the arithmetic context

Validate structural invariants, canonical ordering, algorithm compatibility,
window identity, currency, and prior lineage. Establish the Policy's local
Decimal context.

### Phase 1: data-quality state

Derive DataQualityState from the typed issues:

- any blocking issue -> `blocked`;
- otherwise any issue -> `warning`;
- no issue -> `pass`.

Blocked quality returns maturity `insufficient`, health `None`, zero Advice
candidates, and blocked/unavailable SignalResults that preserve reason codes.
Streaks whose Policy reset rules include the blocking reason are reset. The
result remains a complete deterministic Evaluation rather than an exception,
because bad data quality is valid evidence.

### Phase 2: delivery classification

Classify delivery before a maturity early return. Zero current-window
impressions plus the frozen status history can produce `not_delivering` without
pretending that CTR or CPA declined. Remote paused and zero-delivery days do
not count as active delivery days.

The evaluator emits a stable no-delivery signal. Episode closure after seven
complete no-delivery days is applied by the lifecycle implementation in
Section 3; the pure evaluator does not mutate the Episode.

### Phase 3: maturity

The effective thresholds are the stricter combination of Platform Policy and
Owner Target where the Target is allowed to tighten a threshold.

The default Beta rules are:

- `monitorable`: at least 14 complete account-local days, at least 10 active
  delivery days, and both seven-day windows have at least 1,000 impressions
  and 20 link clicks;
- `economics_ready`: monitorable plus at least five attributed purchases in
  each window and the applicable spend floor.

The spend floor is the strictest applicable value among the Platform economic
floor, Target minimum economic spend, and three times Target CPA when Target
CPA exists. A missing Target does not prevent relative economic-health
evaluation when Platform floors are met, but it does prevent
`consider_scale_test` and target-relative breakdown Advice.

Evidence below monitorable maturity is `warming` and produces no deterministic
business Advice. Structurally valid but blocked evidence is `insufficient`.

A span that opens with `before_episode` days is `warming` whatever the day
counts say, because the 7-day comparison needs both windows inside the
Episode. After the quality gate it skips Phases 4–8: every current-versus-
baseline signal and `frequency_high` are `unavailable` with reason
`episode_window_incomplete`, `configuration_drift` and
`no_delivery_complete_day` are computed as usual (so the seven-day
no-delivery streak counts from the Episode's first day), health is
`not_delivering` when the Episode's current-window days have no impressions
and is otherwise withheld with `episode_window_incomplete`, and there is no
Advice.

### Phase 4: primitive signals

Compute frequency level/change, CTR change with Newcombe support, CVR, CPM,
delivery, and bootstrapped CPA/ROAS effects. SignalResult uses a four-state
result:

- `hit`;
- `miss`;
- `unavailable`;
- `blocked`.

A boolean `passed` is not used because it would conflate contrary evidence,
missing denominators, insufficient samples, and blocked quality.

Primitive signal identities and point estimates are fixed:

| Signal | Observed value and hit rule |
| --- | --- |
| `frequency_high` | current exact frequency; hit when `>=` effective level threshold |
| `frequency_growth` | `(current / baseline) - 1`; hit when `>=` effective growth threshold |
| `ctr_decline` | `1 - (current_ctr / baseline_ctr)` plus §2.4 Newcombe support; both magnitude and support must pass |
| `cvr_decline` | `1 - (current_cvr / baseline_cvr)`; hit when `>=` effective threshold after the required click/purchase floors |
| `cpm_increase` | `(current_cpm / baseline_cpm) - 1`; hit when `>=` effective threshold |
| `delivery_decline` | `1 - (current_impressions / baseline_impressions)`; hit when `>=` effective threshold and status history does not explain the change as a user/remote pause |
| `cpa_worsening` | `(current_cpa / baseline_cpa) - 1` plus §2.4 bootstrap support |
| `roas_worsening` | `1 - (current_roas / baseline_roas)` plus §2.4 bootstrap support |
| `configuration_drift` | current evidence hash differs from the Episode baseline while the frozen comparability rule still permits the Episode to continue |
| `no_delivery_complete_day` | the latest complete daily slot is present, has zero primitive delivery, `delivered=false`, and frozen status history does not classify it as missing evidence |

Every relative signal requires an available positive baseline. `cvr` is
`purchases / link_clicks`; `cpm` is `1000 * spend / impressions`. The no-delivery
counter advances only once per distinct `as_of_local_day`; health
`not_delivering` requires zero current-window impressions plus compatible frozen
status history, while Episode closure requires seven consecutive
`no_delivery_complete_day` hits.

The Policy freezes symmetric `ctr_stability_tolerance` and
`cvr_stability_tolerance`. `stable_ctr` or `stable_cvr` is true only when both
ratios are available, the corresponding adverse signal is `miss`, and the
absolute relative change is within the inclusive tolerance. `unavailable` and
`blocked` never mean stable.

### Phase 5: streak reducer

Compare the current structured StreakCompatibility with PriorStreakState.
Reset affected counters on Policy, Target, contract-set,
configuration-baseline, or data-continuity changes according to the Policy.
Then increment every current hit and reset every current miss. Unavailable and
blocked signals follow the Policy's explicit carry-or-reset rule; there is no
implicit choice.

Segment streak keys include breakdown type, stable segment key, economic
metric, and contributing signal identity.

### Phase 6: health classification

Health precedence is deterministic:

1. quality blocked -> `None`;
2. not delivering -> `not_delivering`;
3. confirmed composite fatigue -> `creative_fatigue`;
4. economic decline with stable CTR -> `economics_decline`;
5. CPM/delivery anomaly with stable CTR and CVR -> `delivery_anomaly`;
6. first composite fatigue hit, or frequency+CTR with insufficient economics
   -> `fatigue_watch`;
7. materially contrary directions, such as CTR deterioration with clear
   economic improvement, -> `None` with `health_reason='conflicting_signals'`;
8. another isolated adverse hit that does not satisfy a classified composite
   -> `None` with `health_reason='partial_adverse_signals'`;
9. every Policy-required health signal is available, with no adverse or
   conflicting signal -> `healthy`.

The evaluator never “keeps the current health” without receiving that state as
input. Conflicting evidence is not labeled healthy and produces no Advice.

Confirmed creative fatigue requires all of:

- exact current frequency at or above the effective threshold;
- frequency growth at or above the effective threshold;
- interval-supported CTR decline at or above the effective threshold;
- bootstrap-supported CPA increase or ROAS decline at or above the effective
  threshold;
- economics-ready maturity;
- the configured number of consecutive composite hits.

After classifying current health, update the consecutive healthy-evaluation
counter. Only an actual `healthy` evaluation increments it; any other health,
`None`, blocked quality, or compatibility reset returns it to zero.

### Phase 7: breakdown analysis

Run placement and country analysis only at monitorable or economics-ready
maturity and only when the required Target metric exists. Apply sample gates,
bootstrap hypothesis, BH correction, and segment streaks in the order defined
in §2.4. Age/gender remains descriptive.

### Phase 8: scale-test eligibility

Emit `consider_scale_test` only when all of these are true:

- economics-ready maturity;
- current health is healthy and the updated healthy streak meets the Policy;
- every configured Target is satisfied by the appropriate confidence bound;
- an Owner Target with at least one economic target exists;
- DataQualityState is pass, not merely non-blocked;
- there is no configuration-drift signal;
- there is no fatigue, watch, or delivery anomaly;
- the explicit delivery diagnostic is neither unknown nor a blocking state
  such as `learning_limited`;
- Meta mode and Policy permit the candidate. Sandbox and controlled-mock
  evaluations cannot be promoted into internal-validation outcomes.

### Phase 9: advice candidate assembly

The five possible Advice types remain:

- `refresh_creative`;
- `review_offer_or_landing_page`;
- `review_delivery_mix`;
- `investigate_delivery_anomaly`;
- `consider_scale_test`.

Warming, insufficient, not-delivering, healthy, and fatigue-watch states do not
create placeholder Advice.

Each candidate contains typed evidence details and a `reason_signature` over:

- advice type;
- sorted contributing signal IDs;
- sorted segment keys where applicable;
- Policy version;
- Target version or explicit no-target marker;
- algorithm version;
- metric-contract-set hash;
- Episode configuration-baseline hash.

Including semantic versions prevents a newly applicable Policy or Target from
colliding with an already superseded AdviceThread.

### Phase 10: result hash

Compute `result_hash` from a versioned canonical payload containing the input
snapshot canonical hash, actual algorithm version, maturity, quality state,
health and reason, ordered signals, ordered candidates, and output StreakState.
The payload excludes `result_hash` itself and excludes persistence timestamps.

## 2.6 Immutable output contracts

```python
@dataclass(frozen=True)
class SignalResult:
    signal_id: str
    state: SignalState
    expected: Decimal | None
    observed: Decimal | None
    interval_lower: Decimal | None
    interval_upper: Decimal | None
    threshold: Decimal | None
    p_value: Decimal | None
    adjusted_q_value: Decimal | None
    reason_code: str


@dataclass(frozen=True)
class AdviceEvidenceItem:
    key: str
    value: CanonicalScalar


@dataclass(frozen=True)
class AdviceCandidate:
    advice_type: AdviceType
    reason_signature: str
    severity: AdviceSeverity
    signal_ids: tuple[str, ...]
    evidence: tuple[AdviceEvidenceItem, ...]


@dataclass(frozen=True)
class StreakReset:
    streak_id: str  # "healthy_evaluations" or the exact signal/segment key
    previous_value: int
    reason_code: str


@dataclass(frozen=True)
class StreakState:
    compatibility: StreakCompatibility
    healthy_evaluations: int
    counters: tuple[StreakCounter, ...]
    resets: tuple[StreakReset, ...]


@dataclass(frozen=True)
class ScaleEvaluation:
    snapshot_canonical_hash: str
    algorithm_version: str
    maturity: EvidenceMaturity
    data_quality_state: DataQualityState
    health: ScaleHealth | None
    health_reason: str
    signals: tuple[SignalResult, ...]
    advice_candidates: tuple[AdviceCandidate, ...]
    streak_state: StreakState
    result_hash: str
```

Signals, candidate evidence, candidate signal IDs, streak counters, and
`StreakReset` records have documented canonical sort keys. The evaluator returns
them in canonical order. `resets` is empty when no reset occurred and identifies
the exact previous streak and Policy-governed compatibility or continuity reason
when one did. Reset records are ordered by `(streak_id, reason_code)`, require a
non-negative previous value, and reject duplicate pairs.

## 2.7 Canonical serialization

Scale uses a versioned canonical encoder rather than relying on
`json.dumps(..., default=str)`. The latter does not define enough semantics for
long-lived statistical replay.

The Scale canonical format accepts only:

- null, booleans, integers, and Unicode strings;
- enums encoded by their canonical value;
- Decimal encoded as a tagged object containing a normalized non-exponent
  decimal string;
- date and offset-aware datetime encoded as tagged ISO values;
- tuples encoded as arrays in their documented canonical order;
- frozen dataclasses encoded as objects with stable field names;
- no float, set, arbitrary mapping, object repr, NaN, or infinity.

Object keys are sorted, JSON separators are fixed, UTF-8 is used without
locale-sensitive transformations, and each hash payload begins with a domain
separator and canonical-schema version. Snapshot hash, compatibility hash,
reason signature, statistical seed, and result hash use distinct domain
separators.

The snapshot-hash payload is explicit rather than a reflection of every ORM
column. It includes all evaluator-relevant value fields, including
`last_sync_at`, `freshness_reference_at`, source identities and hashes, and
prior-streak input. It excludes `snapshot_id`, `canonical_hash` itself, ORM-only
surrogate IDs, `created_at`, `sealed_at`, and other persistence metadata. This
makes content-identical snapshots hash identically without introducing a
circular or ambient-clock-derived input.

## 2.8 Performance contract

The assembler bounds the number of breakdown segments per family and rejects
evidence beyond the published contract limit. The evaluator validates the
bound before bootstrap work.

The Beta benchmark covers the maximum permitted placement and country
segments at 10,000 bootstrap draws. The evaluator must finish within the Worker
lease budget with no unbounded allocation. Any future parallel implementation
must preserve the exact per-signal stream and output order; performance work
cannot change results under the same algorithm version.

## 2.9 Persistence assembler responsibilities

`app/scale/evidence.py` is the adapter between ORM records and the evaluator
interface. In one read-consistent operation it:

1. loads a sealed ScaleEvidenceSnapshot and all immutable children;
2. verifies Workspace, Meta mode, connection, scenario, Episode, Policy,
   Target, contract-set, timezone, currency, and source provenance;
3. verifies every source and DataQualityIssue is included in the stored
   canonical hash;
4. loads only the predecessor frozen by the sealed snapshot and verifies its
   complete `PriorStreakState`;
5. constructs canonically ordered frozen value objects;
6. recomputes and verifies the snapshot canonical hash;
7. returns the immutable `ScaleEvidenceSnapshot` value.

The assembler never patches incomplete evidence, selects a newer Policy or
Target or predecessor, reads the current clock, applies a streak reset, calls
the evaluator, persists an Evaluation, or silently changes a contract. Snapshot
creation and sealing happen before this interface. The orchestration module in
Section 3 is the sole caller that invokes `evaluate_scale()` and persists its
result.

## 2.10 Required tests

### Dependency and purity

- AST scan rejects persistence, adapter, config, store, OS, network, clock,
  logging, and non-versioned random dependencies.
- the registry rejects caller labels, routes each frozen Policy only to its
  matching implementation, and retains a previous version across a simulated
  deployment while a durable run or replayable snapshot references it;
- Repeated calls with the same snapshot produce byte-identical canonical output
  and the same result hash.
- Different ambient Decimal contexts and `PYTHONHASHSEED` values do not change
  output.
- Evaluation leaves every input value unchanged.

### Input integrity

- invalid, overlapping, non-adjacent, duplicate, or unsorted windows fail with
  stable error codes; a declared missing daily slot instead requires a matching
  blocking gap issue and returns a valid insufficient evaluation;
- an observed breakdown requires complete daily evidence, while a
  privacy-suppressed breakdown rejects any metric values and requires its typed
  suppression reason;
- negative values, clicks greater than impressions, currency mismatch,
  contract mismatch, and stale/cross-Episode prior state fail closed;
- an absent predecessor is valid only with
  `no_earlier_distinct_day`, while an unexplained gap or a supplied prior plus
  an absence reason fails closed; this reason also covers a legitimate same-day
  semantic reevaluation or replay of the Episode's first day, where another
  same-day Evaluation may exist but is deliberately ineligible as predecessor;
- freshness timestamps must be offset-aware and
  `freshness_reference_at >= last_sync_at`; exceeding the frozen freshness SLO
  is typed quality evidence, while a reversed timestamp is structurally
  invalid;
- blocked DataQuality evidence returns insufficient/health-none and zero
  Advice rather than raising an input-shape error;
- target-less snapshots remain valid but cannot produce target-relative or
  scale-test Advice.

### Statistical golden vectors

- Wilson and Newcombe intervals match frozen reference vectors including zero
  and boundary proportions and the strict `difference_lower > 0` rule;
- bootstrap PRNG words, baseline-before-current draw order, rejection sampling,
  selected indices, valid-draw filtering, nearest-rank percentiles, CPA/ROAS
  effects, inclusive null-centered p-values, and BH reverse cumulative minima
  match frozen vectors;
- baseline-zero and resampled zero-denominator cases are unavailable rather
  than zero;
- BH handles ties deterministically and includes every sample-eligible segment
  in the declared family;
- permuting source insertion order cannot change canonical segment order,
  statistical streams, candidates, or hashes.

### Domain behavior

- one high-frequency metric alone never yields fatigue;
- the first complete fatigue hit yields watch and the required consecutive hit
  yields creative fatigue;
- stable CTR with economic decline routes to Offer/Landing Page review rather
  than refresh creative;
- stable CTR/CVR with CPM or delivery degradation routes to delivery anomaly;
- contradictory signals yield health `None` and no Advice;
- an unavailable Policy-required health signal, or an isolated adverse signal
  that satisfies no classified composite, yields health `None` and no Advice;
- paused and zero-delivery evidence never masquerade as CTR/CPA decline;
- placement/country require spend share, samples, target effect, FDR, and
  streak; age/gender never creates Advice;
- Policy, Target, contract-set, configuration, and data-continuity changes
  reset the specified streaks and emit canonically ordered `StreakReset`
  records;
- consider-scale requires pass quality, a known non-blocking diagnostic, an
  Owner Target, confidence-bound success, and consecutive healthy evaluations;
- controlled-mock and customer-sandbox evidence can produce correctly labeled
  evaluation evidence but cannot become internal-validation AdviceOutcome.

### Hashing and replay

- Decimal spellings, dates, enums, Unicode, and tuple order have frozen
  canonical vectors;
- snapshot hash vectors prove that changing only `snapshot_id`, `created_at`,
  or `sealed_at` does not change content identity, while changing `last_sync_at`
  or `freshness_reference_at` does; every hash payload excludes its own hash
  field;
- reason signatures change when Policy, Target, algorithm, contract set,
  configuration baseline, contributing signals, or segment identity changes;
- a stored sealed snapshot replayed after database, environment, clock, or
  current Policy changes returns the original result byte for byte.

## 2.11 Explicit non-responsibilities

The evaluator does not:

- decide whether a snapshot is ready to seal;
- fetch, reconcile, repair, interpolate, or backfill Meta evidence;
- read an active Policy, Target, contract, or delivery configuration;
- create or update Enrollment, Episode, AdviceThread, Occurrence, Disposition,
  Outcome, notification, GenerationDraft, or ScaleTestPlanDraft records;
- close an Episode or resolve/supersede/expire Advice;
- infer UnobservedFactors or missing DeliveryDiagnostics;
- claim causal attribution;
- produce Meta mutation commands, budget values, targeting changes, or status
  changes.

Those responsibilities belong to the ingestion, sealing, orchestration, and
Advice lifecycle modules defined in Section 3.
