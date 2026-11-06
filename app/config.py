"""全局配置：从 .env / 环境变量读取，全部可覆盖。"""
import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _normalize_sqlalchemy_database_url(value: str) -> str:
    """Use the installed psycopg 3 driver for bare PostgreSQL URLs.

    ``psql`` and ``pg_dump`` intentionally receive the standard
    ``postgresql://`` form, while SQLAlchemy otherwise selects the absent
    psycopg2 driver for that form.  Keep the deployment-facing variable
    portable and make the Python connection choice explicit here.
    """
    for prefix in ("postgres://", "postgresql://"):
        if value.startswith(prefix):
            return "postgresql+psycopg://" + value[len(prefix) :]
    return value

DATA_DIR = Path(os.getenv("MARKETAI_DATA_DIR", ROOT_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = _normalize_sqlalchemy_database_url(
    os.getenv(
        "MARKETAI_DATABASE_URL",
        f"sqlite:///{(DATA_DIR / 'marketai.db').as_posix()}",
    ).strip()
)
TENANT_CONTEXT_SECRET = os.getenv("MARKETAI_TENANT_CONTEXT_SECRET", "").strip()
DEFAULT_WORKSPACE_ID = os.getenv("MARKETAI_WORKSPACE_ID", "ws_default").strip()
DEFAULT_CURRENCY = os.getenv("MARKETAI_CURRENCY", "USD").strip().upper()
LOCAL_IDENTITY_ENABLED = os.getenv(
    "MARKETAI_LOCAL_IDENTITY_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
LOCAL_WORKSPACE_ID = os.getenv("MARKETAI_LOCAL_WORKSPACE_ID", "ws_local").strip()
LOCAL_INITIATOR_ID = os.getenv(
    "MARKETAI_LOCAL_INITIATOR_ID", "local-owner"
).strip()

# ---- API keys ----
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ARK_API_KEY = os.getenv("ARK_API_KEY", "").strip()

# ---- 端点 ----
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")

# ---- 图片模型默认值（2026-08 核实的现役模型，可在 .env 覆盖）----
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image")
GEMINI_IMAGE_MODELS = ["gemini-3.1-flash-image", "gemini-3-pro-image", "gemini-3.1-flash-lite-image", "gemini-2.5-flash-image"]
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1.5")
OPENAI_IMAGE_MODELS = ["gpt-image-1.5", "gpt-image-1", "gpt-image-1-mini", "gpt-image-2"]
SEEDREAM_MODEL = os.getenv("SEEDREAM_MODEL", "doubao-seedream-4-5-251128")
SEEDREAM_MODELS = ["doubao-seedream-4-5-251128", "doubao-seedream-4-0-250828"]

# ---- 角度引擎 LLM ----
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "").strip().lower()  # 留空=自动选有 key 的
LLM_MODEL = os.getenv("LLM_MODEL", "").strip()
LLM_DEFAULTS = {"gemini": "gemini-2.5-flash", "openai": "gpt-4o-mini"}

# ---- 生成参数 ----
GEN_CONCURRENCY = int(os.getenv("GEN_CONCURRENCY", "2"))
GEN_TIMEOUT = float(os.getenv("GEN_TIMEOUT", "180"))
GEN_RETRIES = int(os.getenv("GEN_RETRIES", "2"))
GENERATION_MAX_COSTS_JSON = os.getenv("MARKETAI_GENERATION_MAX_COSTS_JSON", "{}")
REF_MAX_SIDE = 1024  # 参考图送入 API 前压到的最长边

# 每张图成本估算（美元，粗略值，仅供预算提示）
COST_PER_IMAGE = {
    "gemini": Decimal("0.04"),
    "openai": Decimal("0.06"),
    "seedream": Decimal("0.03"),
    "mock": Decimal("0"),
}

# 硬上限：任何请求参数都不能绕过。要调整必须改环境配置并留下部署记录。
MAX_COST_PER_RUN = Decimal(os.getenv("MAX_COST_PER_RUN", "5"))
MAX_COST_PER_DAY = Decimal(os.getenv("MAX_COST_PER_DAY", "20"))

# ---- Admission contract version gate ----
# 0 = legacy cost-check path; >= 1 = PaidOperationAdmission flow.
MARKETAI_ADMISSION_CONTRACT_VERSION = int(os.getenv("MARKETAI_ADMISSION_CONTRACT_VERSION", "0"))

# SQLite 首版运行参数。切 PostgreSQL 时保留同一 repository 接口。
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000"))
JOB_LEASE_SECONDS = int(os.getenv("JOB_LEASE_SECONDS", "300"))
JOB_POLL_SECONDS = float(os.getenv("JOB_POLL_SECONDS", "0.5"))

# ---- Worker drain (spec §110) ----
WORKER_DRAIN_TIMEOUT_SECONDS = int(os.getenv("WORKER_DRAIN_TIMEOUT_SECONDS", "300"))

# ---- JWKS key revocation ----
JWKS_REVOKED_KEY_IDS = frozenset(
    kid.strip()
    for kid in os.getenv("JWKS_REVOKED_KEY_IDS", "").split(",")
    if kid.strip()
)

# ---- Retention boundaries ----
RETENTION_APP_LOG_DAYS = int(os.getenv("RETENTION_APP_LOG_DAYS", "30"))
RETENTION_SECURITY_LOG_DAYS = int(os.getenv("RETENTION_SECURITY_LOG_DAYS", "90"))
RETENTION_TRACE_DAYS = int(os.getenv("RETENTION_TRACE_DAYS", "7"))

# ---- Meta Marketing API（本阶段严格只读）----
# API 版本必须由部署显式冻结；不提供“永远跟着最新”的隐式默认值。
# 升级时通过环境变量切换，并由 metric contract hash 隔离新旧口径。
META_API_VERSION = os.getenv("META_API_VERSION", "").strip()
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "").strip()
META_APP_SECRET = os.getenv("META_APP_SECRET", "").strip()
META_AD_ACCOUNT_ID = os.getenv("META_AD_ACCOUNT_ID", "").strip()
if META_AD_ACCOUNT_ID and not META_AD_ACCOUNT_ID.startswith("act_"):
    META_AD_ACCOUNT_ID = f"act_{META_AD_ACCOUNT_ID}"
META_CREDENTIAL_WORKSPACE_ID = os.getenv(
    "META_CREDENTIAL_WORKSPACE_ID", ""
).strip()
META_SYNC_ENABLED = os.getenv("META_SYNC_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
META_SYNC_HOUR = int(os.getenv("META_SYNC_HOUR", "3"))
META_TIMEZONE = os.getenv("META_TIMEZONE", "UTC").strip()
META_LOOKBACK_DAYS = int(os.getenv("META_LOOKBACK_DAYS", "3"))
META_HTTP_TIMEOUT = float(os.getenv("META_HTTP_TIMEOUT", "45"))
META_PAGE_LIMIT = int(os.getenv("META_PAGE_LIMIT", "100"))
META_PURCHASE_ACTION_TYPE = os.getenv("META_PURCHASE_ACTION_TYPE", "omni_purchase").strip()
META_RAW_DIR = DATA_DIR / "meta" / "raw"
META_FRESHNESS_SLO_HOURS = int(os.getenv("META_FRESHNESS_SLO_HOURS", "36"))

# ---- Meta Launch canary（默认硬禁用）----
META_WRITE_ENABLED = os.getenv("META_WRITE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
META_WRITE_ACCESS_TOKEN = os.getenv("META_WRITE_ACCESS_TOKEN", "").strip()
LAUNCH_CONFIRMATION_SECRET_FILE = os.getenv("LAUNCH_CONFIRMATION_SECRET_FILE", "").strip()
LAUNCH_CONFIRMATION_SECRET = os.getenv("LAUNCH_CONFIRMATION_SECRET", "").strip()
if not LAUNCH_CONFIRMATION_SECRET and LAUNCH_CONFIRMATION_SECRET_FILE:
    secret_path = Path(LAUNCH_CONFIRMATION_SECRET_FILE)
    if not secret_path.is_absolute():
        secret_path = ROOT_DIR / secret_path
    try:
        LAUNCH_CONFIRMATION_SECRET = secret_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        LAUNCH_CONFIRMATION_SECRET = ""
LAUNCH_PREVIEW_TTL_SECONDS = int(os.getenv("LAUNCH_PREVIEW_TTL_SECONDS", "900"))
LAUNCH_CONFIRM_TTL_SECONDS = int(os.getenv("LAUNCH_CONFIRM_TTL_SECONDS", "300"))
LAUNCH_MAX_DAILY_BUDGET_MINOR = int(os.getenv("LAUNCH_MAX_DAILY_BUDGET_MINOR", "500"))


# ---- Delegated Identity (spec §3) ----
DELEGATED_JWKS_URL = os.getenv("DELEGATED_JWKS_URL", "").strip()
DELEGATED_JWKS_MAX_STALE_SECONDS = int(os.getenv("DELEGATED_JWKS_MAX_STALE_SECONDS", "600"))
DELEGATED_JWKS_REFRESH_INTERVAL_SECONDS = int(os.getenv("DELEGATED_JWKS_REFRESH_INTERVAL_SECONDS", "300"))
#: Startup-only retry budget for the very first JWKS fetch. Web and API
#: restart together on a full redeploy, so the JWKS host may not resolve for
#: the first minute or two; a bounded retry keeps API from failing its
#: healthcheck window behind Web. 20 attempts at 2s doubling to 20s is about
#: five and a half minutes -- keep the Railway healthcheck timeout above it.
DELEGATED_JWKS_INITIAL_LOAD_ATTEMPTS = int(os.getenv("DELEGATED_JWKS_INITIAL_LOAD_ATTEMPTS", "20"))
DELEGATED_JWKS_INITIAL_LOAD_BACKOFF_SECONDS = float(
    os.getenv("DELEGATED_JWKS_INITIAL_LOAD_BACKOFF_SECONDS", "2")
)
DELEGATED_JWKS_INITIAL_LOAD_MAX_BACKOFF_SECONDS = float(
    os.getenv("DELEGATED_JWKS_INITIAL_LOAD_MAX_BACKOFF_SECONDS", "20")
)
DELEGATED_JWT_ISSUER = os.getenv("DELEGATED_JWT_ISSUER", "urn:marketai:control-plane").strip()
DELEGATED_JWT_AUDIENCE = os.getenv("DELEGATED_JWT_AUDIENCE", "urn:marketai:domain-api").strip()
DELEGATED_JWT_ALLOWED_CLOCK_SKEW_SECONDS = int(os.getenv("DELEGATED_JWT_ALLOWED_CLOCK_SKEW_SECONDS", "5"))
DELEGATED_JWT_MAX_TOKEN_BYTES = int(os.getenv("DELEGATED_JWT_MAX_TOKEN_BYTES", "4096"))
DELEGATED_TOKEN_MAX_TTL_SECONDS = int(os.getenv("DELEGATED_TOKEN_MAX_TTL_SECONDS", "60"))

# ---- Platform-Operator Identity (spec §3, /api/platform/v1/* only) ----
# A separate JWKS instance from the delegated-identity one above: platform-
# operator key rotation/outage must never affect delegated-user
# verification, and vice versa (see app/platform/platform_operator_identity.py).
PLATFORM_OPERATOR_JWKS_URL = os.getenv("PLATFORM_OPERATOR_JWKS_URL", "").strip()
PLATFORM_OPERATOR_JWKS_MAX_STALE_SECONDS = int(
    os.getenv("PLATFORM_OPERATOR_JWKS_MAX_STALE_SECONDS", "300")
)
PLATFORM_OPERATOR_JWKS_REFRESH_INTERVAL_SECONDS = int(
    os.getenv("PLATFORM_OPERATOR_JWKS_REFRESH_INTERVAL_SECONDS", "60")
)

PROJECTION_REPAIR_INTERVAL_SECONDS = int(os.getenv("PROJECTION_REPAIR_INTERVAL_SECONDS", "60"))
CONTROL_PLANE_INTERNAL_BASE_URL = os.getenv("MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL", "").strip()
PROJECTION_SYNC_SERVICE_TOKEN = os.getenv("MARKETAI_PROJECTION_SYNC_SERVICE_TOKEN", "").strip()
PROJECTION_SYNC_TIMEOUT_MS = int(os.getenv("MARKETAI_PROJECTION_SYNC_TIMEOUT_MS", "3000"))

# ---- Entitlement projection v2 (signed envelope) ----
DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN = os.getenv("DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN", "").strip()
CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN = os.getenv("CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN", "").strip()
ENTITLEMENT_VERIFICATION_KEY_RING = os.getenv("ENTITLEMENT_VERIFICATION_KEY_RING", "").strip()
ENTITLEMENT_MAX_LEASE_SECONDS = int(os.getenv("ENTITLEMENT_MAX_LEASE_SECONDS", "86400"))
MARKETAI_CREDENTIAL_KMS_ADAPTER = os.getenv("MARKETAI_CREDENTIAL_KMS_ADAPTER", "local").strip()
MARKETAI_CREDENTIAL_LOCAL_KMS_SECRET = os.getenv("MARKETAI_CREDENTIAL_LOCAL_KMS_SECRET", "").strip()
MARKETAI_PLATFORM_MAX_PER_OPERATION = Decimal(os.getenv("MARKETAI_PLATFORM_MAX_PER_OPERATION", "5"))
MARKETAI_PLATFORM_MAX_ROLLING_24H = Decimal(os.getenv("MARKETAI_PLATFORM_MAX_ROLLING_24H", "20"))
MARKETAI_PLATFORM_MAX_PERIOD = Decimal(os.getenv("MARKETAI_PLATFORM_MAX_PERIOD", "100"))
MARKETAI_PLATFORM_MAX_OUTSTANDING = Decimal(os.getenv("MARKETAI_PLATFORM_MAX_OUTSTANDING", "10"))
MARKETAI_PLATFORM_POLICY_VERSION = int(os.getenv("MARKETAI_PLATFORM_POLICY_VERSION", "1"))
MARKETAI_PRICE_REGISTRY_VERSION = os.getenv("MARKETAI_PRICE_REGISTRY_VERSION", "v1")

# ---- 市场数据（竞品扫描，binding_scope=market_data）----
# 价格表：供应商 → 操作 → {"credits": 1, "credit_usd": "0.00188"}，五种操作缺一不可；
# 改价同步 MARKETAI_PRICE_REGISTRY_VERSION。未配置时不能提交真实解析或扫描（受控 Mock 不受影响）。
MARKET_DATA_MAX_COSTS_JSON = os.getenv("MARKETAI_MARKET_DATA_MAX_COSTS_JSON", "{}")
SCRAPECREATORS_BASE_URL = os.getenv(
    "MARKETAI_SCRAPECREATORS_BASE_URL", "https://api.scrapecreators.com"
).strip().rstrip("/")
# 保存 Key 时的免费余额检查；须短于 BFF 等待响应头的 10 秒。
SCRAPECREATORS_CHECK_TIMEOUT_SECONDS = float(
    os.getenv("MARKETAI_SCRAPECREATORS_CHECK_TIMEOUT_SECONDS", "5")
)
# 相关性判断的规则前置（规格 §7.1）：关键词发现的观察若落地到这些批发站（主域，逗号分隔），
# 或广告主分类含下列词（不区分大小写，逗号分隔），直接判为无关，不送 LLM。
# 末尾的 bazaar-wholesale.example 是受控 Mock 夹具里的虚构批发站（#428）。
COMPETITOR_WHOLESALE_DOMAINS = os.getenv(
    "MARKETAI_COMPETITOR_WHOLESALE_DOMAINS",
    "alibaba.com,aliexpress.com,1688.com,made-in-china.com,dhgate.com,globalsources.com,indiamart.com,"
    "bazaar-wholesale.example",
)
COMPETITOR_DEALER_CATEGORY_TERMS = os.getenv("MARKETAI_COMPETITOR_DEALER_CATEGORY_TERMS", "dealer,经销商,车行")
# 方案自由文本 market → 两位国家码（规格 §7.2），起草时据此选同市场的竞品快照；键在匹配前小写、去空白，
# 如 {"北美": "US", "north america": "US", "usa": "US", "美国": "US"}。未配置时只认两位国家码本身，其余都判为不匹配。
MARKET_COUNTRY_ALIASES_JSON = os.getenv("MARKETAI_MARKET_COUNTRY_ALIASES_JSON", "{}")

# ---- Platform administration routes (spec §8.2, §8.7) ----
# Placeholder bearer-token gate for `/api/platform/v1/*` -- see
# `app.meta.platform_api.request_platform_context` for why this is not yet
# a real `typ: "platform+jwt"` validator with its own JWKS endpoint. Empty
# by default, matching every other unconfigured service-token gate in this
# module (e.g. CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN): the routes exist
# but refuse every request (503) until a deployment opts in.
PLATFORM_ADMIN_TOKEN = os.getenv("PLATFORM_ADMIN_TOKEN", "").strip()

# ---- Entitlement inbox push limits ----
ENTITLEMENT_INBOX_MAX_BODY_BYTES = int(os.getenv("MARKETAI_ENTITLEMENT_INBOX_MAX_BODY_BYTES", "65536"))


def parse_entitlement_key_ring() -> dict[str, bytes]:
    """Parse ENTITLEMENT_VERIFICATION_KEY_RING into {key_id: key_bytes}."""
    if not ENTITLEMENT_VERIFICATION_KEY_RING:
        return {}
    ring: dict[str, bytes] = {}
    for pair in ENTITLEMENT_VERIFICATION_KEY_RING.split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        key_id, hex_key = pair.split(":", 1)
        ring[key_id.strip()] = bytes.fromhex(hex_key.strip())
    return ring


#: Delegated identity is wired into the ASGI app only when explicitly
#: configured with a JWKS URL and local identity is not the active
#: development bypass. Neither is set in CI or the default `.env`, so the
#: legacy `/api/*` surface (outside the middleware's `/api/v1/*` protection
#: scope regardless) and the existing test suite are unaffected until a
#: deployment opts in by setting `DELEGATED_JWKS_URL`.
DELEGATED_IDENTITY_ENABLED = bool(DELEGATED_JWKS_URL) and not LOCAL_IDENTITY_ENABLED

# ---- BlobStore (spec §7) ----
#: "s3" (versioned S3), "railway_bucket" (versionless Railway Bucket), or
#: "local" (dev-only, backed by LocalBlobTransferApp).
BLOB_STORE_ADAPTER = os.getenv("MARKETAI_BLOB_STORE_ADAPTER", "local").strip().lower()
BLOB_STORE_S3_BUCKET = os.getenv("MARKETAI_BLOB_STORE_S3_BUCKET", "").strip()
BLOB_STORE_S3_REGION = os.getenv("MARKETAI_BLOB_STORE_S3_REGION", "").strip() or None
BLOB_STORE_S3_ENDPOINT_URL = os.getenv(
    "MARKETAI_BLOB_STORE_S3_ENDPOINT_URL", ""
).strip()
BLOB_STORE_S3_ADDRESSING_STYLE = os.getenv(
    "MARKETAI_BLOB_STORE_S3_ADDRESSING_STYLE", "virtual"
).strip().lower()
# Exact https origins the browser transfers blobs from (the Web APP_URL).
# The Release converges the bucket CORS rule from this list; see
# `scripts/verify_railway_bucket.py` and `RailwayBucketBlobStore.converge_browser_cors`.
BLOB_STORE_BROWSER_ORIGINS = tuple(
    origin.strip()
    for origin in os.getenv("MARKETAI_BLOB_STORE_BROWSER_ORIGINS", "").split(",")
    if origin.strip()
)
#: Filesystem root for the local Adapter and LocalBlobTransferApp.
BLOB_STORE_LOCAL_ROOT = Path(
    os.getenv("MARKETAI_BLOB_STORE_LOCAL_ROOT", str(DATA_DIR / "blob_store"))
)
#: HMAC secret binding local capability signatures (spec §7.4). Distinct
#: from TENANT_CONTEXT_SECRET -- a leaked blob-transfer secret should not
#: also compromise tenant context signing, or vice versa.
BLOB_STORE_LOCAL_SIGNING_SECRET = os.getenv("MARKETAI_BLOB_STORE_LOCAL_SIGNING_SECRET", "").strip()
#: Origin LocalBlobTransferApp issues in CORS responses (browser -> storage).
BLOB_STORE_LOCAL_CORS_ORIGIN = os.getenv(
    "MARKETAI_BLOB_STORE_LOCAL_CORS_ORIGIN", "http://localhost:3000"
).strip()
#: Base URL LocalFileBlobStore signs capabilities against; must match where
#: LocalBlobTransferApp actually listens.
BLOB_STORE_LOCAL_PUBLIC_URL = os.getenv(
    "MARKETAI_BLOB_STORE_LOCAL_PUBLIC_URL", "http://localhost:9000"
).strip()
BLOB_STORE_LOCAL_HOST = os.getenv(
    "MARKETAI_BLOB_STORE_LOCAL_HOST", "127.0.0.1"
).strip()
BLOB_STORE_LOCAL_PORT = int(os.getenv("MARKETAI_BLOB_STORE_LOCAL_PORT", "9000"))

# ---- 创意库参考素材门禁（契约 §7.2）----
# 原件永远私有；Provider 只能读取受控派生。上限是硬门禁，请求参数不能绕过。
LIBRARY_OBJECT_DIR = DATA_DIR / "library" / "objects"
LIBRARY_MAX_REFERENCE_BYTES = int(os.getenv("LIBRARY_MAX_REFERENCE_BYTES", str(20 * 1024 * 1024)))
LIBRARY_MAX_REFERENCE_PIXELS = int(os.getenv("LIBRARY_MAX_REFERENCE_PIXELS", str(50_000_000)))
LIBRARY_MAX_REFERENCE_SIDE = int(os.getenv("LIBRARY_MAX_REFERENCE_SIDE", "12000"))
LIBRARY_DERIVATIVE_MAX_SIDE = int(os.getenv("LIBRARY_DERIVATIVE_MAX_SIDE", str(REF_MAX_SIDE)))
LIBRARY_ALLOWED_REFERENCE_MIME = {"image/jpeg", "image/png", "image/webp"}
# 内容安全检查默认走受控 Mock；真实 adapter 由 #20 的数据源/外部能力接入。
# 模式与 Mock 场景都是部署决定，绝不能由调用方在请求里选择。
LIBRARY_CONTENT_SAFETY_MODE = os.getenv("LIBRARY_CONTENT_SAFETY_MODE", "mock").strip().lower()
LIBRARY_CONTENT_SAFETY_MOCK_SCENARIO = os.getenv("LIBRARY_CONTENT_SAFETY_MOCK_SCENARIO", "clean").strip().lower()


def meta_write_ready() -> bool:
    return bool(
        META_WRITE_ENABLED and META_WRITE_ACCESS_TOKEN and META_APP_SECRET
        and META_AD_ACCOUNT_ID and META_API_VERSION and LAUNCH_CONFIRMATION_SECRET
    )


def meta_credentials_ready() -> bool:
    """只检查配置完整性，不泄露任何凭证内容。"""
    return bool(META_ACCESS_TOKEN and META_APP_SECRET and META_AD_ACCOUNT_ID and META_API_VERSION)


def resolve_llm() -> tuple[str, str] | None:
    """返回 (provider, model)；没有可用 key 时返回 None（走离线模板）。"""
    if LLM_PROVIDER in ("gemini", "openai"):
        key = GEMINI_API_KEY if LLM_PROVIDER == "gemini" else OPENAI_API_KEY
        if key:
            return LLM_PROVIDER, (LLM_MODEL or LLM_DEFAULTS[LLM_PROVIDER])
    if GEMINI_API_KEY:
        return "gemini", (LLM_MODEL or LLM_DEFAULTS["gemini"])
    if OPENAI_API_KEY:
        return "openai", (LLM_MODEL or LLM_DEFAULTS["openai"])
    return None

# Bounded independent execution lanes, per Worker process.
WORKER_GENERATION_CONCURRENCY = max(1, min(8, int(os.getenv('WORKER_GENERATION_CONCURRENCY', '2'))))
WORKER_QC_CONCURRENCY = max(1, min(8, int(os.getenv('WORKER_QC_CONCURRENCY', '1'))))
WORKER_PLANNING_CONCURRENCY = max(1, min(8, int(os.getenv('WORKER_PLANNING_CONCURRENCY', '1'))))
WORKER_GENERAL_CONCURRENCY = max(1, min(8, int(os.getenv('WORKER_GENERAL_CONCURRENCY', '1'))))
