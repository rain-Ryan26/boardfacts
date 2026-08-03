from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from flyboard.errors import ServiceError
from flyboard.model_calls import (
    ModelCallFailed,
    ModelCallNotStarted,
    ModelCallRequest,
    ModelCallSucceeded,
    ModelCallUnavailable,
    ModelMessage,
    TextContentPart,
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
    GerberDiagnosticRequestArtifact,
    GerberDiagnosticResponseArtifact,
    GerberDiagnosticSubmission,
)

_SYSTEM_PROMPT = (
    "你是 BoardFacts GerberScout 的单字段诊断备注 Agent。程序已经确定当前字段、字段值、诊断关联和处理状态。"
    "你只根据字段语义、现有字段值以及相关工具发现和错误，生成一条可直接展示给工程师的简短中文检查备注。"
    "说明诊断事实怎样影响当前字段以及工程师需要复核什么；可以合并重复诊断，不要逐条机械复述。"
    "不得修改或补造字段值，不得重新判断字段状态，不得执行生产规则或生成最终检查结论。"
    "不得输出内部标识、字段编码、状态或模式枚举、路径、诊断编号和输入输出协议词。"
    "响应必须是首字符为 {、尾字符为 } 的单个裸 JSON 对象，且只含 response_schema 规定的键，不得返回纯文本、Markdown code fence 或附加说明。"
)


@dataclass(frozen=True, slots=True)
class DiagnosticNoteResult:
    note: str
    error: ErrorRecord | None


class _Control:
    def __init__(self, context, source_material_id: str, request_path: str) -> None:
        self.context = context
        self.source_material_id = source_material_id
        self.request_path = request_path

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
                branch=BranchCode.GERBER,
                event_type=TrajectoryEventType.MODEL_REQUEST_STARTED,
                summary={"call_purpose": CallPurpose.GERBER_DIAGNOSTIC.value},
                source_material_ids=[self.source_material_id],
                artifact_paths=[self.request_path],
                error_ids=[],
            )
        )
        return True


class GerberDiagnosticNoteWriter:
    def write(
        self,
        *,
        job_material_id: str,
        output_key: str,
        mode: Literal["missing_value", "unreliable_value", "finding_only"],
        field_context: dict[str, object],
        context,
    ) -> DiagnosticNoteResult:
        fallback = _fallback(mode)
        try:
            policy = context.model_client.policy_for(CallPurpose.GERBER_DIAGNOSTIC)
        except (ModelCallUnavailable, KeyError, ValueError):
            return DiagnosticNoteResult(
                fallback,
                _record_failure(
                    context,
                    job_material_id,
                    "Gerber 诊断备注模型策略或生产客户端不可用。",
                ),
            )

        safe_key = output_key.replace(".", "_")
        base = f"gerberscout/{job_material_id}/diagnostic/{safe_key}"
        request_artifact = GerberDiagnosticRequestArtifact(
            schema_version="1.0",
            run_id=context.run_id,
            job_material_id=job_material_id,
            output_key=output_key,
            mode=mode,
            model=policy.model_id,
            timeout_seconds=policy.timeout_seconds,
            system_prompt=_SYSTEM_PROMPT,
            field_context=field_context,
            output_schema_version="1.0",
        )
        request_path, request_error = _write_json(
            context,
            request_artifact,
            _entry(f"{base}/request.json", job_material_id),
        )
        if request_error is not None:
            return DiagnosticNoteResult(fallback, request_error)

        request = ModelCallRequest(
            call_purpose=CallPurpose.GERBER_DIAGNOSTIC,
            messages=(
                ModelMessage(role="system", content=(TextContentPart(_SYSTEM_PROMPT),)),
                ModelMessage(
                    role="user",
                    content=(
                        TextContentPart(
                            json.dumps(
                                {
                                    "task": _task_instruction(mode),
                                    "field_context": field_context,
                                    "response_schema": {
                                        "append_check_note": "不含内部标识、状态枚举或路径的非空中文业务说明"
                                    },
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        ),
                    ),
                ),
            ),
        )
        try:
            outcome = context.model_client.call(
                request,
                control=_Control(context, job_material_id, request_path),
            )
        except ModelCallNotStarted:
            raise
        except ModelCallUnavailable:
            return DiagnosticNoteResult(
                fallback,
                _record_failure(context, job_material_id, "Gerber 诊断备注模型调用不可用。"),
            )

        if isinstance(outcome, ModelCallFailed):
            error = _record_failure(
                context,
                job_material_id,
                "Gerber 诊断备注模型调用失败或输入超过容量。",
            )
            context.model_calls.record(
                outcome,
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=BranchCode.GERBER,
                error_id=error.error_id,
                artifact_paths=[request_path],
            )
            return DiagnosticNoteResult(fallback, error)

        assert isinstance(outcome, ModelCallSucceeded)
        if (
            outcome.request_model_id != policy.model_id
            or outcome.response_model_id != policy.model_id
        ):
            error = _record_failure(
                context,
                job_material_id,
                "Gerber 诊断备注模型响应身份与配对请求不一致。",
            )
            context.model_calls.record(
                outcome,
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=BranchCode.GERBER,
                error_id=error.error_id,
                artifact_paths=[request_path],
            )
            return DiagnosticNoteResult(fallback, error)

        response_artifact = GerberDiagnosticResponseArtifact(
            schema_version="1.0",
            run_id=context.run_id,
            job_material_id=job_material_id,
            output_key=output_key,
            model=outcome.response_model_id,
            content=outcome.response_text,
        )
        response_path, response_error = _write_json(
            context,
            response_artifact,
            _entry(f"{base}/response.json", job_material_id),
        )
        if response_error is not None:
            context.model_calls.record(
                outcome,
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=BranchCode.GERBER,
                error_id=response_error.error_id,
                artifact_paths=[request_path],
            )
            return DiagnosticNoteResult(fallback, response_error)
        context.model_calls.record(
            outcome,
            phase=PhaseCode.BRANCH_ANALYSIS,
            branch=BranchCode.GERBER,
            artifact_paths=[request_path, response_path],
        )
        try:
            payload = json.loads(outcome.response_text)
            submission = GerberDiagnosticSubmission.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, TypeError):
            return DiagnosticNoteResult(
                fallback,
                _record_failure(
                    context,
                    job_material_id,
                    "Gerber 诊断备注模型响应不符合封闭协议。",
                ),
            )
        return DiagnosticNoteResult(submission.append_check_note.strip(), None)


def _entry(path: str, job_material_id: str) -> ArtifactIndexEntry:
    return ArtifactIndexEntry(
        path=path,
        kind="model_debug",
        producer_phase=PhaseCode.BRANCH_ANALYSIS,
        consumers=[ArtifactConsumer.DEBUG, ArtifactConsumer.REGRESSION],
        access_level=AccessLevel.RESTRICTED_DEBUG,
        source_material_ids=[job_material_id],
    )


def _write_json(context, artifact, entry):
    try:
        return context.artifact_writer.write_json(artifact, entry), None
    except ServiceError as exc:
        if exc.error_id is None:
            raise
        if exc.code == "artifact_index_update_failed":
            raise
        return None, context.diagnostics.resolve_error(exc.error_id)


def _record_failure(context, job_material_id: str, message: str) -> ErrorRecord:
    return context.diagnostics.record_error(
        code="gerber_diagnostic_model_failed",
        severity="warning",
        phase="branch_analysis",
        branch="gerber",
        message=message,
        source_material_ids=[job_material_id],
        artifact_paths=[],
        suggested_action="查看关联工具诊断并人工复核该字段。",
    )


def _task_instruction(mode: str) -> str:
    if mode == "missing_value":
        return "说明已有工具发现或错误为什么使该字段没有形成可用值，以及工程师需要复核什么；不得生成字段值。"
    if mode == "unreliable_value":
        return "说明现有候选受到哪些解析范围、工具能力或错误限制，以及为什么需要工程师复核；不得改写候选值。"
    return "说明现有候选关联的工程告警及复核重点；不得把工具告警改写成生产规则违反。"


def _fallback(mode: str) -> str:
    if mode == "missing_value":
        return "GerberScout 未能生成该字段的可用值；请查看关联工具发现或运行错误。"
    if mode == "unreliable_value":
        return "GerberScout 已形成候选值，但工具覆盖不完整；请查看关联诊断后复核。"
    return "GerberScout 已形成可用候选，但存在工程 warning；请查看关联工具发现。"
