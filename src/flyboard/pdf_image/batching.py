from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from flyboard.errors import ServiceError
from flyboard.model_calls import (
    FailureCategory,
    ModelCallFailed,
    ModelCallNotStarted,
    ModelCallSucceeded,
    ModelCallUnavailable,
)
from flyboard.models import (
    AccessLevel,
    ArtifactConsumer,
    ArtifactIndexEntry,
    BranchCode,
    CallPurpose,
    ErrorRecord,
    PhaseCode,
    TrajectoryEvent,
    TrajectoryEventType,
)
from flyboard.services._support import utc_now

from .models import (
    PdfAcceptedAsset,
    PdfModelParsedArtifact,
    PdfModelResponseArtifact,
    PdfModelUsage,
)
from .parser import PdfSubmissionParser
from .preparation import PreparedPdfAsset
from .requests import build_model_request, build_request_artifact
from .trace import PdfBatchTrace, estimated_units

if TYPE_CHECKING:
    from flyboard.models import RunInputSnapshot
    from flyboard.workflow import AnalysisContext


@dataclass(frozen=True, slots=True)
class PdfImageSettings:
    max_image_count: int = 8
    request_budget_units: int = 40_000_000
    page_text_char_limit: int = 12_000

    def __post_init__(self) -> None:
        if self.max_image_count < 1 or self.request_budget_units < 1:
            raise ValueError("PDF batch limits must be positive")
        if self.page_text_char_limit < 0:
            raise ValueError("page text limit must be non-negative")


@dataclass(frozen=True, slots=True)
class PdfBatch:
    batch_id: str
    parent_batch_id: str | None
    source_material_id: str
    assets: tuple[PreparedPdfAsset, ...]


@dataclass(frozen=True, slots=True)
class PdfBatchAnalysis:
    accepted_assets: tuple[PdfAcceptedAsset, ...]
    errors: tuple[ErrorRecord, ...]
    trace: PdfBatchTrace


@dataclass(frozen=True, slots=True)
class _LeafResult:
    outcome: Literal["completed", "limit", "failed"]
    accepted_assets: tuple[PdfAcceptedAsset, ...]
    errors: tuple[ErrorRecord, ...]


@dataclass(slots=True)
class _ModelControl:
    context: AnalysisContext
    batch: PdfBatch
    request_path: str

    def allow_attempt(self, attempt_number: Literal[1, 2]) -> bool:
        if attempt_number == 2:
            return not self.context.stop_signal.is_requested()
        self.context.stop_signal.check_boundary()
        self.context.trajectory_writer.append(
            TrajectoryEvent(
                schema_version="1.0",
                run_id=self.context.run_id,
                timestamp=utc_now(),
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=BranchCode.PDF_IMAGE,
                event_type=TrajectoryEventType.MODEL_REQUEST_STARTED,
                summary={
                    "call_purpose": CallPurpose.PDF_IMAGE.value,
                    "batch_id": self.batch.batch_id,
                },
                source_material_ids=[self.batch.source_material_id],
                artifact_paths=[self.request_path],
                error_ids=[],
            )
        )
        return True


class PdfBatchRunner:
    def __init__(
        self,
        *,
        parser: PdfSubmissionParser | None = None,
        settings: PdfImageSettings | None = None,
    ) -> None:
        self._parser = parser or PdfSubmissionParser()
        self.settings = settings or PdfImageSettings()

    def analyze(
        self,
        snapshot: RunInputSnapshot,
        assets: tuple[PreparedPdfAsset, ...],
        context: AnalysisContext,
    ) -> PdfBatchAnalysis:
        selected = set(snapshot.groups.pdf_inputs + snapshot.groups.image_inputs)
        source_ids = [
            item.material_id for item in snapshot.materials if item.material_id in selected
        ]
        trace = PdfBatchTrace(context.run_id, self.settings, source_ids, context)
        accepted: list[PdfAcceptedAsset] = []
        errors: list[ErrorRecord] = []
        for batch in self._plan(snapshot, assets):
            context.stop_signal.check_boundary()
            trace.add(batch, "batch_planned", outcome="planned")
            batch_accepted, batch_errors = self._process(batch, snapshot, context, trace)
            accepted.extend(batch_accepted)
            errors = _merge_errors(errors, batch_errors)
        return PdfBatchAnalysis(tuple(accepted), tuple(errors), trace)

    def _plan(self, snapshot, assets):
        by_source: dict[str, list[PreparedPdfAsset]] = {}
        for asset in assets:
            by_source.setdefault(asset.record.source_material_id, []).append(asset)
        material_order = {item.material_id: item.material_order for item in snapshot.materials}
        batches: list[PdfBatch] = []
        for source_id in sorted(by_source, key=material_order.__getitem__):
            source_assets = by_source[source_id]
            if source_assets[0].record.source_kind == "image":
                batches.append(
                    PdfBatch(
                        f"image_{material_order[source_id]:04d}_0001",
                        None,
                        source_id,
                        (source_assets[0],),
                    )
                )
                continue
            current: list[PreparedPdfAsset] = []
            number = 0
            for asset in source_assets:
                proposed = [*current, asset]
                if current and (
                    len(proposed) > self.settings.max_image_count
                    or estimated_units(proposed) > self.settings.request_budget_units
                ):
                    number += 1
                    batches.append(
                        PdfBatch(
                            f"pdf_{material_order[source_id]:04d}_{number:04d}",
                            None,
                            source_id,
                            tuple(current),
                        )
                    )
                    current = [asset]
                else:
                    current = proposed
            if current:
                number += 1
                batches.append(
                    PdfBatch(
                        f"pdf_{material_order[source_id]:04d}_{number:04d}",
                        None,
                        source_id,
                        tuple(current),
                    )
                )
        return batches

    def _process(self, batch, snapshot, context, trace):
        leaf = self._run_leaf(batch, snapshot, context, trace)
        if leaf.outcome != "limit":
            return leaf.accepted_assets, list(leaf.errors)
        if len(batch.assets) == 1:
            error = context.diagnostics.record_error(
                code="pdf_image_model_limit_exceeded",
                severity="error",
                phase="branch_analysis",
                branch="pdf_image",
                message="单个 PDF/图片 asset 仍超过模型容量。",
                source_material_ids=[batch.source_material_id],
                artifact_paths=[],
                suggested_action="检查输入图片尺寸或已验证的模型容量。",
            )
            trace.add(
                batch,
                "batch_failed",
                outcome="failed",
                error_ids=[error.error_id],
            )
            return (), _merge_errors(list(leaf.errors), [error])
        middle = len(batch.assets) // 2
        children = (
            PdfBatch(
                f"{batch.batch_id}_a",
                batch.batch_id,
                batch.source_material_id,
                batch.assets[:middle],
            ),
            PdfBatch(
                f"{batch.batch_id}_b",
                batch.batch_id,
                batch.source_material_id,
                batch.assets[middle:],
            ),
        )
        trace.add(
            batch,
            "batch_split",
            outcome="split",
            child_batch_ids=[item.batch_id for item in children],
        )
        accepted: list[PdfAcceptedAsset] = []
        errors = list(leaf.errors)
        for child in children:
            trace.add(child, "batch_planned", outcome="planned")
            child_accepted, child_errors = self._process(child, snapshot, context, trace)
            accepted.extend(child_accepted)
            errors = _merge_errors(errors, child_errors)
        return tuple(accepted), errors

    def _run_leaf(self, batch, snapshot, context, trace):
        try:
            policy = context.model_client.policy_for(CallPurpose.PDF_IMAGE)
        except (ModelCallUnavailable, KeyError, ValueError):
            error = _model_error(
                context,
                batch,
                "PDF/图片模型策略或生产客户端未完成装配。",
            )
            return _LeafResult("failed", (), (error,))

        errors: list[ErrorRecord] = []
        protocol_error: ErrorRecord | None = None
        for attempt in (1, 2):
            if attempt == 2:
                context.stop_signal.check_boundary()
                trace.add(
                    batch,
                    "retry_started",
                    attempt=attempt,
                    outcome="retrying",
                    error_ids=_error_ids(errors),
                )
            request_artifact = build_request_artifact(
                batch,
                snapshot,
                policy,
                context,
                attempt,
                self.settings.page_text_char_limit,
            )
            prefix = f"pdf/model/{batch.batch_id}/attempt_{attempt}"
            request_path, write_error, exists = _write_json(
                context,
                request_artifact,
                _entry(
                    f"{prefix}_request.json",
                    "model_request",
                    batch.source_material_id,
                ),
            )
            errors = _merge_errors(errors, [write_error])
            if not exists or request_path is None:
                if attempt == 1:
                    continue
                trace.add(
                    batch,
                    "batch_failed",
                    attempt=attempt,
                    outcome="failed",
                    error_ids=_error_ids(errors),
                )
                return _LeafResult("failed", (), tuple(errors))

            trace.add(
                batch,
                "request_started",
                attempt=attempt,
                outcome="running",
                artifact_paths=[request_path],
                error_ids=_error_ids(errors),
            )
            request = build_model_request(batch, snapshot, request_artifact)
            try:
                outcome = context.model_client.call(
                    request,
                    control=_ModelControl(context, batch, request_path),
                )
            except ModelCallNotStarted:
                raise
            except ModelCallUnavailable:
                error = _model_error(
                    context,
                    batch,
                    "PDF/图片模型客户端在调用前不可用。",
                    [request_path],
                )
                return _LeafResult("failed", (), tuple(_merge_errors(errors, [error])))
            if isinstance(outcome, ModelCallFailed):
                if outcome.failure_category == FailureCategory.INPUT_TOO_LARGE:
                    context.model_calls.record(
                        outcome,
                        phase=PhaseCode.BRANCH_ANALYSIS,
                        branch=BranchCode.PDF_IMAGE,
                        error_id=None,
                        artifact_paths=[request_path],
                    )
                    trace.add(
                        batch,
                        "provider_limit_rejected",
                        attempt=attempt,
                        outcome="split",
                        artifact_paths=[request_path],
                    )
                    return _LeafResult("limit", (), tuple(errors))
                error = _model_error(
                    context,
                    batch,
                    "PDF/图片模型调用没有取得可解析响应。",
                    [request_path],
                )
                errors = _merge_errors(errors, [error])
                context.model_calls.record(
                    outcome,
                    phase=PhaseCode.BRANCH_ANALYSIS,
                    branch=BranchCode.PDF_IMAGE,
                    error_id=error.error_id,
                    artifact_paths=[request_path],
                )
                trace.add(
                    batch,
                    "batch_failed",
                    attempt=attempt,
                    outcome="failed",
                    artifact_paths=[request_path],
                    error_ids=[error.error_id],
                )
                return _LeafResult("failed", (), tuple(errors))
            if not isinstance(outcome, ModelCallSucceeded):
                raise TypeError("model client returned an unsupported outcome")
            if (
                outcome.request_model_id != policy.model_id
                or outcome.response_model_id != policy.model_id
            ):
                error = _model_error(
                    context,
                    batch,
                    "PDF/图片模型响应身份与请求不一致。",
                    [request_path],
                )
                errors = _merge_errors(errors, [error])
                context.model_calls.record(
                    outcome,
                    phase=PhaseCode.BRANCH_ANALYSIS,
                    branch=BranchCode.PDF_IMAGE,
                    error_id=error.error_id,
                    artifact_paths=[request_path],
                )
                return _LeafResult("failed", (), tuple(errors))

            response = PdfModelResponseArtifact(
                schema_version="1.0",
                run_id=context.run_id,
                batch_id=batch.batch_id,
                attempt=attempt,
                model=outcome.response_model_id,
                content=outcome.response_text,
                usage=(
                    PdfModelUsage(
                        input_tokens=outcome.usage.input_tokens,
                        output_tokens=outcome.usage.output_tokens,
                    )
                    if outcome.usage is not None
                    else None
                ),
            )
            response_path, response_error, response_exists = _write_json(
                context,
                response,
                _entry(
                    f"{prefix}_response.json",
                    "model_response",
                    batch.source_material_id,
                ),
            )
            errors = _merge_errors(errors, [response_error])
            paths = [request_path] + ([response_path] if response_path else [])
            if not response_exists or response_path is None:
                context.model_calls.record(
                    outcome,
                    phase=PhaseCode.BRANCH_ANALYSIS,
                    branch=BranchCode.PDF_IMAGE,
                    error_id=response_error.error_id if response_error else None,
                    artifact_paths=paths,
                )
                if attempt == 1:
                    continue
                return _LeafResult("failed", (), tuple(errors))
            trace.add(
                batch,
                "response_received",
                attempt=attempt,
                outcome="received",
                artifact_paths=paths,
                error_ids=_error_ids(errors),
            )

            dimensions = {}
            for asset in batch.assets:
                width = asset.record.width_px
                height = asset.record.height_px
                if width is None or height is None or asset.record.asset_id is None:
                    raise ValueError("prepared asset dimensions are incomplete")
                dimensions[asset.record.asset_id] = (width, height)
            parsed = self._parser.parse(
                outcome.response_text,
                batch_id=batch.batch_id,
                asset_dimensions=dimensions,
                field_table=snapshot.field_table,
            )
            if parsed.rejections and protocol_error is None:
                protocol_error = context.diagnostics.record_error(
                    code="pdf_image_model_protocol_invalid",
                    severity="error",
                    phase="branch_analysis",
                    branch="pdf_image",
                    message=("PDF/图片模型提交包含不符合协议的 envelope、asset 或候选。"),
                    source_material_ids=[batch.source_material_id],
                    artifact_paths=paths,
                    suggested_action="检查 PDF/图片模型输出协议。",
                )
                errors = _merge_errors(errors, [protocol_error])
            region_error = None
            if parsed.region_degradations:
                region_error = context.diagnostics.record_error(
                    code="pdf_image_region_degraded",
                    severity="warning",
                    phase="branch_analysis",
                    branch="pdf_image",
                    message="PDF/图片模型区域已裁剪或降级为页级证据。",
                    source_material_ids=[batch.source_material_id],
                    artifact_paths=paths,
                    suggested_action="在证据下钻中复核页图或整图。",
                )
                errors = _merge_errors(errors, [region_error])
            parsed_artifact = PdfModelParsedArtifact(
                schema_version="1.0",
                run_id=context.run_id,
                batch_id=batch.batch_id,
                attempt=attempt,
                status=(
                    "accepted_with_rejections"
                    if parsed.accepted_assets and parsed.rejections
                    else "accepted"
                    if parsed.accepted_assets
                    else "rejected"
                ),
                accepted_assets=list(parsed.accepted_assets),
                rejections=list(parsed.rejections),
                region_degradations=list(parsed.region_degradations),
                error_ids=_stable_ids(
                    [
                        protocol_error.error_id if protocol_error else None,
                        region_error.error_id if region_error else None,
                    ]
                ),
            )
            parsed_path, parsed_error, parsed_exists = _write_json(
                context,
                parsed_artifact,
                _entry(
                    f"{prefix}_parsed.json",
                    "model_parsed",
                    batch.source_material_id,
                ),
            )
            errors = _merge_errors(errors, [parsed_error])
            if parsed_path:
                paths.append(parsed_path)
            record_error = parsed_error or protocol_error or region_error
            context.model_calls.record(
                outcome,
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=BranchCode.PDF_IMAGE,
                error_id=record_error.error_id if record_error else None,
                artifact_paths=paths,
            )
            if not parsed_exists or parsed_path is None:
                if attempt == 1:
                    continue
                return _LeafResult("failed", (), tuple(errors))
            if parsed.envelope_rejected:
                trace.add(
                    batch,
                    "protocol_failed",
                    attempt=attempt,
                    outcome="retrying" if attempt == 1 else "failed",
                    artifact_paths=paths,
                    error_ids=_error_ids(errors),
                )
                if attempt == 1:
                    continue
                return _LeafResult("failed", (), tuple(errors))
            trace.add(
                batch,
                "batch_completed",
                attempt=attempt,
                outcome="completed",
                artifact_paths=paths,
                error_ids=_error_ids(errors),
            )
            return _LeafResult("completed", parsed.accepted_assets, tuple(errors))
        return _LeafResult("failed", (), tuple(errors))


def _entry(path, kind, source_material_id):
    return ArtifactIndexEntry(
        path=path,
        kind=kind,
        producer_phase=PhaseCode.BRANCH_ANALYSIS,
        consumers=[
            ArtifactConsumer.WORKFLOW,
            ArtifactConsumer.DEBUG,
            ArtifactConsumer.REGRESSION,
        ],
        access_level=AccessLevel.RESTRICTED_DEBUG,
        source_material_ids=[source_material_id],
    )


def _write_json(context, artifact, entry):
    try:
        return context.artifact_writer.write_json(artifact, entry), None, True
    except ServiceError as exc:
        if exc.error_id is None:
            raise
        error = context.diagnostics.resolve_error(exc.error_id)
        if exc.code == "artifact_index_update_failed":
            return entry.path, error, True
        return None, error, False


def _model_error(context, batch, message, paths=None):
    return context.diagnostics.record_error(
        code="pdf_image_model_call_failed",
        severity="error",
        phase="branch_analysis",
        branch="pdf_image",
        message=message,
        source_material_ids=[batch.source_material_id],
        artifact_paths=paths or [],
        suggested_action="检查模型 endpoint、策略和输入后创建新的运行。",
    )


def _merge_errors(existing, added):
    values = list(existing)
    seen = {item.error_id for item in values}
    for item in added:
        if item is not None and item.error_id not in seen:
            seen.add(item.error_id)
            values.append(item)
    return values


def _error_ids(errors):
    return [item.error_id for item in errors]


def _stable_ids(values):
    result = []
    for value in values:
        if value is not None and value not in result:
            result.append(value)
    return result
