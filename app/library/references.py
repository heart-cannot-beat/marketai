"""参考素材上传门禁：quarantine → 检查 → 受控派生（契约 §7.2）。

不变量：
- 上传先进入 quarantine，未通过全部检查前不产生 ReferenceAssetVersion；
- 原件保持私有，只在授权审计场景访问；
- Provider 只能读取去 EXIF、符合能力限制的受控派生版本；
- 内容安全检查是可替换 adapter；受控 Mock 必须显式，并写入证据（§18）；
- 内容安全的 execution mode 与 Mock 场景由部署配置决定，永远不由请求方选择。

两条物理路径共用同一套纯函数：

- ``run_gate``：本地同步上传路径（``quarantine_upload`` 写入
  ``LIBRARY_OBJECT_DIR``），原件与派生都是本地绝对路径；
- Worker 的 ``library.reference_gate``：生产路径，原件是 Upload Intent 提交到
  ``BlobStore`` 的 committed 对象，派生通过 ``BlobStore.commit_generated_artifact``
  写回同一对象存储。Worker 只负责字节的进出，检查、派生编码与落库都在这里。
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageFile, UnidentifiedImageError

from .. import config
from ..adapters.persistence.models import ReferenceObjectRecord, new_id, utcnow
from app.platform.blob_store import validate_object_key
from app.platform.tenancy import TenantContext

from .contracts import EXECUTION_MODES, LibraryContractError

# 解压炸弹护栏：超出像素上限时 Pillow 直接报错，而不是先吃满内存。
Image.MAX_IMAGE_PIXELS = config.LIBRARY_MAX_REFERENCE_PIXELS
ImageFile.LOAD_TRUNCATED_IMAGES = False

STATE_QUARANTINED = "quarantined"
STATE_REJECTED = "rejected"
STATE_RELEASED = "released"

#: 稳定失败码：Operation reason、Job error_code、审计与
#: ``GET /library/reference-objects/{id}`` 的 ``failure_code`` 都用它们，
#: 前端只按码选文案，不解析中文 detail（集成契约 §9）。
FAILURE_GATE_REJECTED = "reference_gate_rejected"
FAILURE_CONTENT_SAFETY_UNAVAILABLE = "content_safety_unavailable"
FAILURE_SOURCE_UNAVAILABLE = "reference_source_unavailable"
FAILURE_STORAGE_UNAVAILABLE = "reference_storage_unavailable"

# EICAR 标准反病毒测试串：安全 fixture，不是真实恶意代码。
EICAR_SIGNATURE = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

_PIL_FORMAT_TO_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
# GPS 是 EXIF 主 IFD 的 0x8825 指针。
_EXIF_GPS_IFD = 0x8825

#: 受控派生在 BlobStore 里的 purpose 段：与 ``staging/committed`` 的
#: ``{workspace_id}/{purpose}/{YYYY}/{MM}/{DD}/{leaf}`` 布局一致（spec §7.13）。
DERIVATIVE_PURPOSE = "reference_derivative"


class ReferenceGateError(RuntimeError):
    """上传门禁拒绝。

    ``code`` is the stable response code (#253); ``failure_code`` stays the
    object's recorded failure family.
    """

    failure_code = FAILURE_GATE_REJECTED
    default_code = "reference_gate_failed"

    def __init__(self, message: str = "", *, code: str | None = None, params: dict | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code
        self.params = params or {}


class ContentSafetyUnavailable(ReferenceGateError):
    """内容安全服务未配置：部署选择了 ``real`` 但真实 adapter 尚未接入。

    这不是对内容的判定——对象停在 quarantine，不进入 rejected，也不静默回落
    受控 Mock；运维接入/切换 adapter 后可以对同一对象重跑门禁。
    """

    failure_code = FAILURE_CONTENT_SAFETY_UNAVAILABLE
    default_code = FAILURE_CONTENT_SAFETY_UNAVAILABLE

    def __init__(
        self, message: str, *, checks: list["GateCheck"] | None = None, code: str | None = None,
        params: dict | None = None,
    ) -> None:
        super().__init__(message, code=code, params=params)
        self.checks = list(checks or [])


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def _workspace_dir(workspace_id: str, bucket: str) -> Path:
    # Workspace 目录隔离：跨 Workspace 不共享对象路径，也就无法通过路径观察对方内容。
    safe = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:32]
    path = config.LIBRARY_OBJECT_DIR / safe / bucket
    path.mkdir(parents=True, exist_ok=True)
    return path


def _object_key(workspace_id: str, bucket: str, digest: str, suffix: str) -> str:
    return f"{_workspace_dir(workspace_id, bucket) / (digest + suffix)}"


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def is_blob_object_key(key: str) -> bool:
    """``committed/...`` 形状的 BlobStore key，而不是本地目录的绝对路径。"""
    if not key or Path(key).is_absolute():
        return False
    try:
        validate_object_key(key)
    except ValueError:
        return False
    return key.split("/", 1)[0] == "committed"


def derivative_object_key(workspace_id: str, digest: str) -> str:
    """受控派生在 BlobStore 中的 key；按原件 digest 寻址，重跑门禁得到同一个 key。"""
    now = utcnow()
    key = (
        f"committed/{workspace_id}/{DERIVATIVE_PURPOSE}/"
        f"{now:%Y}/{now:%m}/{now:%d}/{digest}.jpg"
    )
    validate_object_key(key)
    return key


def resolve_safety_mode(execution_mode: str = "", safety_scenario: str = "") -> tuple[str, str]:
    """内容安全 execution mode 与 Mock 场景：部署配置决定，服务端/测试可显式固定。

    HTTP 层永远不要把这两个参数接到请求体上——否则请求方等于可以自己关闭
    内容安全检查（契约 §18、CLAUDE.md 不变量）。
    """
    mode = (execution_mode or config.LIBRARY_CONTENT_SAFETY_MODE).strip().lower()
    scenario = (safety_scenario or config.LIBRARY_CONTENT_SAFETY_MOCK_SCENARIO).strip().lower()
    if mode not in EXECUTION_MODES:
        raise LibraryContractError(f"未知 execution mode: {mode}", code="library_execution_mode_unknown")
    return mode, scenario


def quarantine_upload(
    session,
    context: TenantContext,
    *,
    declared_mime: str,
    raw: bytes,
    filename: str = "",
) -> tuple[ReferenceObjectRecord, bool]:
    """把上传写入 quarantine，返回 (对象, 是否复用了已有对象)。

    同一 Workspace 内相同文件复用对象存储（§7.1），但复用不等于业务身份相同：
    Binding 仍然各自独立。
    """
    if not raw:
        raise ReferenceGateError("上传内容为空", code="reference_upload_empty")
    if len(raw) > config.LIBRARY_MAX_REFERENCE_BYTES:
        raise ReferenceGateError(
            f"文件超过上限 {config.LIBRARY_MAX_REFERENCE_BYTES} 字节",
            code="file_too_large",
            params={"limit_mb": max(1, round(config.LIBRARY_MAX_REFERENCE_BYTES / (1024 * 1024)))},
        )
    declared = (declared_mime or "").strip().lower()
    if declared not in config.LIBRARY_ALLOWED_REFERENCE_MIME:
        raise ReferenceGateError(f"不接受的声明 MIME: {declared_mime!r}", code="reference_mime_not_allowed")

    digest = sha256_bytes(raw)
    existing = (
        session.query(ReferenceObjectRecord)
        .filter(
            ReferenceObjectRecord.workspace_id == context.workspace_id,
            ReferenceObjectRecord.content_sha256 == digest,
        )
        .one_or_none()
    )
    if existing is not None:
        if existing.state == STATE_REJECTED:
            # 同一 Workspace 内这份内容已经被门禁拒绝且原件已清除。
            # 重新写盘等于把已知不合规的文件再存一次，直接拒绝。
            raise ReferenceGateError(
                "该文件此前已被上传门禁拒绝，不接受重复上传", code="reference_previously_rejected",
            )
        return existing, True

    key = _object_key(context.workspace_id, "originals", digest, ".bin")
    Path(key).write_bytes(raw)
    record = ReferenceObjectRecord(
        id=new_id("refobj"),
        workspace_id=context.workspace_id,
        content_sha256=digest,
        original_object_key=key,
        declared_mime=declared,
        mime_type="",
        byte_size=len(raw),
        state=STATE_QUARANTINED,
        checks_json={"filename": Path(filename or "").name, "checks": []},
    )
    session.add(record)
    session.flush()
    return record, False


def _scan_malware(raw: bytes) -> GateCheck:
    if EICAR_SIGNATURE in raw:
        return GateCheck("malware_scan", False, "命中 EICAR 测试签名")
    # 图片对象里不应该出现可执行文件魔数。
    for magic, label in ((b"MZ", "PE"), (b"\x7fELF", "ELF"), (b"#!", "script")):
        if raw.startswith(magic):
            return GateCheck("malware_scan", False, f"文件头是 {label}，不是图片")
    return GateCheck("malware_scan", True, "")


def _decode(raw: bytes) -> tuple[Image.Image, str]:
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ReferenceGateError(
            f"畸形或不可解码的图片: {type(exc).__name__}", code="reference_image_undecodable",
        ) from exc
    return image, (image.format or "").upper()


def _exif_check(image: Image.Image) -> tuple[GateCheck, bool]:
    """检测 EXIF/GPS 隐私元数据。存在不阻断，但必须由派生版本剥离。"""
    try:
        exif = image.getexif()
    except Exception:  # pragma: no cover - 解码器差异
        exif = None
    if not exif:
        return GateCheck("exif_privacy", True, "无 EXIF"), False
    has_gps = _EXIF_GPS_IFD in exif
    detail = f"发现 {len(exif)} 个 EXIF 标签" + ("，含 GPS" if has_gps else "")
    return GateCheck("exif_privacy", True, detail + "；受控派生将剥离"), True


def content_safety_check(raw: bytes, *, mode: str, scenario: str = "clean") -> GateCheck:
    """内容安全 adapter。

    受控 Mock 只替换这一层外部能力；门禁、派生、权利与审计仍走真实路径（§18）。
    """
    if mode not in EXECUTION_MODES:
        raise LibraryContractError(f"未知 execution mode: {mode}", code="library_execution_mode_unknown")
    if mode == "real":
        # 真实内容安全服务属于 #169 之后的外部能力接入；这里既不静默回落 Mock，
        # 也不把"服务未配置"伪装成对内容的拒绝。
        raise ContentSafetyUnavailable(
            "内容安全服务未配置：LIBRARY_CONTENT_SAFETY_MODE=real 但真实 adapter 尚未接入，"
            "不允许静默回落受控 Mock"
        )
    if scenario == "flagged":
        return GateCheck("content_safety", False, "受控 Mock 场景 flagged")
    return GateCheck("content_safety", True, "受控 Mock 场景 clean")


@dataclass
class GateEvaluation:
    """一次门禁评估的纯结果：还没有碰数据库，也没有写任何对象。"""

    mode: str
    scenario: str
    checks: list[GateCheck] = field(default_factory=list)
    mime: str = ""
    width: int = 0
    height: int = 0
    had_exif: bool = False
    image: Image.Image | None = None

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def failed_checks(self) -> list[str]:
        return [check.name for check in self.checks if not check.passed]


def evaluate_gate(
    raw: bytes,
    *,
    expected_sha256: str,
    declared_mime: str,
    mode: str,
    scenario: str,
) -> GateEvaluation:
    """对原件字节执行全部门禁检查（§7.2），不做任何持久化。

    只依赖字节与对象记录上的两个事实（内容 hash、声明 MIME），Worker 在事务
    之外读完字节后就能调用。``mode == "real"`` 且前置检查全部通过时抛
    :class:`ContentSafetyUnavailable`，异常上携带已完成的检查，调用方负责把它们
    记进证据。
    """
    evaluation = GateEvaluation(mode=mode, scenario=scenario)
    checks = evaluation.checks

    checks.append(
        GateCheck(
            "byte_size",
            0 < len(raw) <= config.LIBRARY_MAX_REFERENCE_BYTES,
            f"{len(raw)} 字节",
        )
    )
    digest = sha256_bytes(raw)
    checks.append(
        GateCheck("object_integrity", digest == expected_sha256, f"sha256={digest[:16]}…")
    )
    checks.append(_scan_malware(raw))

    if all(check.passed for check in checks):
        try:
            image, pil_format = _decode(raw)
        except ReferenceGateError as exc:
            checks.append(GateCheck("decode", False, str(exc)))
        else:
            mime = _PIL_FORMAT_TO_MIME.get(pil_format, "")
            width, height = image.size
            evaluation.image = image
            evaluation.mime = mime
            evaluation.width, evaluation.height = width, height
            checks.append(GateCheck("decode", True, pil_format))
            checks.append(
                GateCheck(
                    "declared_mime_matches_decoded",
                    bool(mime) and mime == declared_mime,
                    f"声明 {declared_mime} / 实际 {mime or pil_format}",
                )
            )
            within = (
                width * height <= config.LIBRARY_MAX_REFERENCE_PIXELS
                and max(width, height) <= config.LIBRARY_MAX_REFERENCE_SIDE
            )
            checks.append(GateCheck("pixel_limits", within, f"{width}x{height}"))
            exif_check, evaluation.had_exif = _exif_check(image)
            checks.append(exif_check)
            try:
                checks.append(content_safety_check(raw, mode=mode, scenario=scenario))
            except ContentSafetyUnavailable as exc:
                exc.checks = list(checks)
                raise
    return evaluation


@dataclass(frozen=True)
class ControlledDerivative:
    """受控派生的内存表示：去 EXIF、按 Provider 能力限制缩放、统一 JPEG 编码。"""

    raw: bytes
    sha256: str
    byte_size: int
    mime: str
    width: int
    height: int


def build_controlled_derivative(image: Image.Image) -> ControlledDerivative:
    working = image.convert("RGB")
    max_side = config.LIBRARY_DERIVATIVE_MAX_SIDE
    if max(working.size) > max_side:
        working.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    # 不传 exif 参数：Pillow 不会自行携带原件元数据。
    working.save(buffer, format="JPEG", quality=90, optimize=True)
    raw = buffer.getvalue()
    return ControlledDerivative(
        raw=raw,
        sha256=sha256_bytes(raw),
        byte_size=len(raw),
        mime="image/jpeg",
        width=working.size[0],
        height=working.size[1],
    )


def _write_evidence(record: ReferenceObjectRecord, evaluation: GateEvaluation) -> None:
    payload = dict(record.checks_json or {})
    payload["checks"] = [check.as_dict() for check in evaluation.checks]
    payload["execution_mode"] = evaluation.mode
    payload["safety_scenario"] = evaluation.scenario if evaluation.mode == "mock" else ""
    payload.pop("failure", None)
    record.checks_json = payload
    record.execution_mode = evaluation.mode
    record.mime_type = evaluation.mime
    record.width = evaluation.width
    record.height = evaluation.height
    record.had_exif = evaluation.had_exif
    record.updated_at = utcnow()


def record_gate_unavailable(record: ReferenceObjectRecord, error: ContentSafetyUnavailable, *, mode: str) -> None:
    """内容安全服务不可用：对象留在 quarantine，证据与稳定失败码照常落库。"""
    payload = dict(record.checks_json or {})
    payload["checks"] = [check.as_dict() for check in error.checks]
    payload["execution_mode"] = mode
    payload["safety_scenario"] = ""
    payload["failure"] = {"code": error.failure_code, "detail": str(error)}
    record.checks_json = payload
    record.execution_mode = mode
    record.state = STATE_QUARANTINED
    record.updated_at = utcnow()


def record_gate_rejection(record: ReferenceObjectRecord, evaluation: GateEvaluation) -> str:
    """任一检查失败：对象进入 ``rejected``，不产生派生，也就不可能发布版本。

    返回人读原因；调用方负责清除原件（本地 unlink 或 BlobStore delete）。
    """
    _write_evidence(record, evaluation)
    reason = f"上传门禁未通过: {', '.join(evaluation.failed_checks)}"
    payload = dict(record.checks_json or {})
    payload["failure"] = {"code": FAILURE_GATE_REJECTED, "detail": reason}
    record.checks_json = payload
    record.state = STATE_REJECTED
    record.derivative_object_key = ""
    record.derivative_storage_version_id = ""
    return reason


def record_gate_release(
    record: ReferenceObjectRecord,
    evaluation: GateEvaluation,
    derivative: ControlledDerivative,
    *,
    object_key: str,
    storage_version_id: str = "",
) -> None:
    """全部检查通过且派生已经写好：冻结派生事实并放行。"""
    _write_evidence(record, evaluation)
    record.derivative_object_key = object_key
    record.derivative_storage_version_id = storage_version_id
    record.derivative_sha256 = derivative.sha256
    record.derivative_byte_size = derivative.byte_size
    record.derivative_mime = derivative.mime
    record.derivative_width = derivative.width
    record.derivative_height = derivative.height
    record.state = STATE_RELEASED


def load_gate_target(session, context: TenantContext, object_id: str) -> ReferenceObjectRecord:
    """Workspace 作用域地取出待门禁对象；跨 Workspace 猜测 ID 不泄露存在性。"""
    record = session.get(ReferenceObjectRecord, object_id)
    if record is None or record.workspace_id != context.workspace_id:
        raise ReferenceGateError("参考对象不存在", code="reference_object_not_found")
    return record


def run_gate(
    session,
    context: TenantContext,
    object_id: str,
    *,
    execution_mode: str = "",
    safety_scenario: str = "",
) -> ReferenceObjectRecord:
    """本地对象目录路径：执行全部上传门禁并在通过时生成受控派生。

    ``execution_mode`` 与 ``safety_scenario`` 是服务端/测试参数，默认取部署配置。
    调用方（尤其是任何未来的 HTTP 层）不得把它们暴露给客户端选择，否则等于让
    请求方自己关闭内容安全检查。生产 Worker 走 ``evaluate_gate`` +
    ``build_controlled_derivative`` + ``record_gate_*``，字节由 BlobStore 进出。
    """
    mode, scenario = resolve_safety_mode(execution_mode, safety_scenario)
    record = load_gate_target(session, context, object_id)
    if record.state == STATE_RELEASED:
        return record
    if not record.original_object_key or record.original_purged:
        raise ReferenceGateError(
            "原件已清除，无法重跑门禁；需要重新上传一个未被拒绝的文件", code="reference_original_purged",
        )
    if is_blob_object_key(record.original_object_key):
        raise ReferenceGateError(
            "该对象的原件在 BlobStore 中，门禁必须由 Worker 执行", code="reference_gate_worker_only",
        )

    raw = Path(record.original_object_key).read_bytes()
    try:
        evaluation = evaluate_gate(
            raw,
            expected_sha256=record.content_sha256,
            declared_mime=record.declared_mime,
            mode=mode,
            scenario=scenario,
        )
    except ContentSafetyUnavailable as exc:
        record_gate_unavailable(record, exc, mode=mode)
        session.flush()
        raise

    if not evaluation.passed:
        reason = record_gate_rejection(record, evaluation)
        # 被拒绝的原件立刻从对象存储清除：恶意或不合规的文件没有理由继续留存，
        # 内容 hash 与检查结果已经足够审计。清理失败不吞掉——记录下来，
        # 由 retry_rejected_original_purge 重试，state='rejected' 且
        # original_purged=False 就是可查询、可告警的滞留条件。
        purge_original(record)
        session.flush()
        raise ReferenceGateError(reason, code=FAILURE_GATE_REJECTED)

    assert evaluation.image is not None
    derivative = build_controlled_derivative(evaluation.image)
    key = _object_key(context.workspace_id, "derivatives", record.content_sha256, ".jpg")
    Path(key).write_bytes(derivative.raw)
    record_gate_release(record, evaluation, derivative, object_key=key)
    session.flush()
    return record


def mark_original_purged(record: ReferenceObjectRecord, error: str = "") -> tuple[bool, str]:
    """记录一次原件清除尝试的结果（本地与 BlobStore 两条路径共用）。"""
    payload = dict(record.checks_json or {})
    purge = dict(payload.get("purge") or {})
    purge["attempts"] = int(purge.get("attempts") or 0) + 1
    if error:
        purge["last_error"] = error
        payload["purge"] = purge
        record.checks_json = payload
        record.original_purged = False
        record.updated_at = utcnow()
        return False, error
    record.original_object_key = ""
    record.original_storage_version_id = ""
    record.original_purged = True
    purge["last_error"] = ""
    payload["purge"] = purge
    record.checks_json = payload
    record.updated_at = utcnow()
    return True, ""


def purge_original(record: ReferenceObjectRecord) -> tuple[bool, str]:
    """删除被拒绝对象的本地原件文件。

    返回 ``(是否已清除, 错误说明)``，并把尝试次数与最后一次错误写进 checks_json。
    失败不抛异常也不静默：记录保持 ``original_purged=False``，
    ``state='rejected' AND original_purged=0`` 就是运维可查询的滞留告警条件。

    BlobStore 中的原件（``committed/...`` key）不能从这里删除——那是 Worker 持有
    adapter 的事（``app.worker_runtime``）；这里如实返回失败而不是假装已清除。
    """
    key = record.original_object_key
    if not key:
        # 已经没有原件路径：要么早已清除，要么从来没落盘。
        return mark_original_purged(record)
    if is_blob_object_key(key):
        return mark_original_purged(record, "原件在 BlobStore 中，需由 Worker 通过 adapter 删除")

    error = ""
    try:
        Path(key).unlink()
    except FileNotFoundError:
        # 文件已不在，目标状态达成。
        pass
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
    return mark_original_purged(record, error)


def derivative_has_exif(record: ReferenceObjectRecord) -> bool:
    """审计辅助：确认本地受控派生确实不含 EXIF。"""
    if not record.derivative_object_key or is_blob_object_key(record.derivative_object_key):
        return False
    with Image.open(record.derivative_object_key) as image:
        return bool(image.getexif())


def provider_payload_path(record: ReferenceObjectRecord) -> str:
    """Provider 只能拿到受控派生；原件与 quarantine 对象永不外发。"""
    if record.state != STATE_RELEASED or not record.derivative_object_key:
        raise ReferenceGateError("对象未放行，不能提供给 Provider", code="reference_object_not_released")
    return record.derivative_object_key


def gate_failure(record: ReferenceObjectRecord) -> tuple[str | None, str | None]:
    """对象上记录的稳定失败码与人读说明（没有失败时为 ``(None, None)``）。"""
    failure = (record.checks_json or {}).get("failure")
    if not isinstance(failure, dict):
        return None, None
    code = failure.get("code")
    return (str(code) if code else None), (str(failure.get("detail") or "") or None)


def object_facts(record: ReferenceObjectRecord) -> dict:
    """冻结进 ReferenceAssetVersion 的文件事实。"""
    return {
        "object_id": record.id,
        "content_sha256": record.content_sha256,
        "mime_type": record.mime_type,
        "byte_size": record.byte_size,
        "width": record.width,
        "height": record.height,
        "execution_mode": record.execution_mode,
        "derivative": {
            "sha256": record.derivative_sha256,
            "mime_type": record.derivative_mime,
            "byte_size": record.derivative_byte_size,
            "width": record.derivative_width,
            "height": record.derivative_height,
        },
        "checks": (record.checks_json or {}).get("checks", []),
    }
