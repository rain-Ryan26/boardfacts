from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from flyboard.errors import ServiceError
from flyboard.model_calls import (
    FailureCategory,
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
    BranchField,
    BranchFieldState,
    BranchResult,
    BranchResultStatus,
    CallPurpose,
    ErrorRecord,
    FieldCategory,
    PhaseCode,
    RunInputSnapshot,
    ToolFinding,
    TrajectoryEvent,
    TrajectoryEventType,
)
from flyboard.repositories import ProjectRepository
from flyboard.services._support import utc_now

if TYPE_CHECKING:
    from flyboard.workflow import AnalysisContext

from .models import (
    XlsxManifestArtifact,
    XlsxModelInput,
    XlsxModelParsedArtifact,
    XlsxModelRequestArtifact,
    XlsxModelResponseArtifact,
    XlsxModelUsage,
    XlsxRawCellsArtifact,
    XlsxSubmittedField,
    XlsxSubmittedUnknownNote,
)
from .parser import ParsedSubmission, XlsxSubmissionParser
from .reader import XlsxReader

SYSTEM_PROMPT = """你是 BoardFacts 的 XLSX 工程资料分析节点。只依据给定字段表和 canonical XLSX 内容判断；不得猜测缺失值，不得生成 Evidence、artifact 路径或最终项目字段状态。

完整字段表同时包含两类生产范围：producer_scope=branch_or_integration 表示分析分支或后续 Integration 可以生成；producer_scope=integration_only 表示只能由后续 Integration 根据各分支结果和运行事实生成，XLSX 分支不能提交。fields[] 只能选择 producer_scope=branch_or_integration 的字段。用户消息会在最终输出协议前直接列出本次可提交和只归 Integration 所有的两组字段编码；这些名单由本次完整字段表生成，必须按名单执行。unknown_notes[] 仍按未知说明合同提交，不把最终未知说明编码写入 fields[]。

不得把“当前输入是 XLSX”、workbook 或 sheet 能被程序读取、分支处理成功等运行事实，当作表格中记载的工程事实来生成资料存在性字段。

字段候选和未知说明必须分别返回非空中文 check_note。备注只使用面向工程师的业务表达，说明当前可读表格事实、据此形成的字段或未知判断，以及该判断对本节点结果的影响；不得只复读 field_value，不得回显字段编码、资料或诊断 id、source_ref、路径、内部状态枚举或协议字段。"""
ANALYSIS_INSTRUCTION = """分析本次全部 workbook 和 sheet。先按完整字段表逐项判断字段资格：只有 producer_scope=branch_or_integration 的字段才能进入 fields[]；producer_scope=integration_only 表示该字段归后续 Integration 所有，本节点即使看见该字段定义也不得提交。
提取能可靠对应可提交字段的工程事实；只存在名称、占位值或无法对应可提交字段但有生产意义的内容写入 unknown_notes。不得根据文件类型、workbook/sheet 可读性或本节点执行结果推导任何资料存在性字段。
每个候选只引用同一 workbook、同一 sheet 中真实存在的 source_cells；公式无缓存值不可作为工程值；引用 value_origin=formula_cache 的单元格时，对应提交项的 check_note 字符串字面内容必须包含“公式缓存”四个字（只写在 field_value 中不合格）。
每个 check_note 必须是非空中文业务说明，说明可读单元格提供了什么事实、为何形成当前字段候选或未知说明及其影响；不得只复制 field_value，不得写入 field_code、source_material_id、source_ref、finding/error id、路径、branch_state 值、SUBMIT_XLSX 或其他协议字段。
同字段的不同来源保持独立，不跨 workbook 合并。每个提交项的 unit 属性必须与完整字段表中该 field_code 的 unit 完全相同。字段表 unit 为 null 只表示提交对象的 unit 属性必须为 null，不表示删除或省略资料中的物理单位、范围、上下限或限定词；field_value 必须保留资料中的完整工程表达。
branch_state 只允许 clear_now、unknown、conflict。响应不得包含协议外文本。"""
OUTPUT_PROTOCOL = """提交前再次检查 fields[] 中每个 field_code：它必须存在于完整字段表，并且对应定义的 producer_scope 必须等于 branch_or_integration。integration_only 不是可选字段，而是只供理解后续 Integration 输出的完整字段语义；严格使用紧邻本协议之前、由本次完整字段表生成的两组字段编码。

最后检查 unit 语义：字段表 unit 为 null 只表示提交对象的 unit 属性必须为 null，不表示删除资料中的物理单位、范围、上下限或限定词；field_value 仍必须保留完整工程表达。

下面只展示合法输出结构；board_thickness_mm 展示字段表 unit 非空的提交，ul_max_operating_temperature 展示字段表 unit 为 null 但 field_value 保留资料物理单位的提交。下列 material id、sheet、单元格位置和值均为人工构造的协议示例，不对应仓库中的任何样例；实际值、来源和单元格必须替换为本次 canonical XLSX 中真实存在的内容，不得照抄示例：
SUBMIT_XLSX: {
  "fields": [{
    "field_code": "board_thickness_mm",
    "field_value": "1.60",
    "unit": "mm",
    "branch_state": "clear_now",
    "source_material_id": "mat_example_thickness",
    "sheet_index": 0,
    "source_cells": ["B7", "D7"],
    "check_note": "技术表给出成品板厚为 1.60 mm，当前可形成板厚候选。"
  }, {
    "field_code": "ul_max_operating_temperature",
    "field_value": "135 °C",
    "unit": null,
    "branch_state": "clear_now",
    "source_material_id": "mat_example_temperature",
    "sheet_index": 0,
    "source_cells": ["B9", "D9"],
    "check_note": "技术表给出 UL 最高工作温度为 135 °C，当前可形成温度候选。"
  }],
  "unknown_notes": [{
    "field_value": "Special process: N/A",
    "source_material_id": "mat_example_process",
    "sheet_index": 0,
    "source_cells": ["B11", "D11"],
    "check_note": "原表只有特殊工艺名称和占位表达，当前无法形成标准工程字段。"
  }]
}"""


@dataclass(slots=True)
class _ModelControl:
    context: AnalysisContext
    source_material_ids: list[str]
    request_path: str
    clock: Callable

    def allow_attempt(self, attempt_number: Literal[1, 2]) -> bool:
        if attempt_number == 2:
            return not self.context.stop_signal.is_requested()
        self.context.stop_signal.check_boundary()
        self.context.trajectory_writer.append(
            TrajectoryEvent(
                schema_version="1.0",
                run_id=self.context.run_id,
                timestamp=self.clock(),
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=BranchCode.XLSX,
                event_type=TrajectoryEventType.MODEL_REQUEST_STARTED,
                summary={"call_purpose": CallPurpose.XLSX.value},
                source_material_ids=self.source_material_ids,
                artifact_paths=[self.request_path],
                error_ids=[],
            )
        )
        return True


class XlsxAnalyzer:
    """First-version XLSX owner: one deterministic read and one model submission."""

    def __init__(
        self,
        projects: ProjectRepository,
        *,
        reader: XlsxReader | None = None,
        parser: XlsxSubmissionParser | None = None,
        clock: Callable = utc_now,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._projects = projects
        self._reader = reader or XlsxReader()
        self._parser = parser or XlsxSubmissionParser()
        self._clock = clock
        self._clock_ns = clock_ns

    def analyze(self, snapshot: RunInputSnapshot, context: AnalysisContext) -> BranchResult:
        source_ids = list(snapshot.groups.spreadsheet_inputs)
        materials = {item.material_id: item for item in snapshot.materials}
        workbooks = []
        cells = []
        errors: list[ErrorRecord] = []
        findings: list[ToolFinding] = []

        started_ns = self._clock_ns()
        context.trajectory_writer.append(
            TrajectoryEvent(
                schema_version="1.0",
                run_id=context.run_id,
                timestamp=self._clock(),
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=BranchCode.XLSX,
                event_type=TrajectoryEventType.TOOL_CALL_STARTED,
                summary={"tool": "xlsx_reader", "workbook_count": len(source_ids)},
                source_material_ids=source_ids,
                artifact_paths=[],
                error_ids=[],
            )
        )
        for material_id in source_ids:
            context.stop_signal.check_boundary()
            material = materials[material_id]
            try:
                path = self._projects.resolve_material_source(snapshot.project_id, material)
            except ServiceError:
                error = context.diagnostics.record_error(
                    code="xlsx_workbook_read_failed",
                    severity="error",
                    phase="branch_analysis",
                    branch="xlsx",
                    message="XLSX workbook 的受管来源无法解析。",
                    source_material_ids=[material_id],
                    artifact_paths=[],
                    suggested_action="重新登记 workbook 后创建新的运行。",
                )
                from .models import XlsxWorkbookRecord

                workbooks.append(
                    XlsxWorkbookRecord(
                        source_material_id=material_id,
                        source_ref=material.source_ref,
                        outcome="failed",
                        date_system=None,
                        sheets=[],
                        error_ids=[error.error_id],
                    )
                )
                errors.append(error)
                continue
            read_result = self._reader.read(path, material, context)
            workbooks.append(read_result.workbook)
            cells.extend(read_result.cells)
            errors.extend(read_result.errors)
            findings.extend(read_result.findings)

        manifest = XlsxManifestArtifact(
            schema_version="1.0", run_id=context.run_id, workbooks=workbooks
        )
        raw_cells = XlsxRawCellsArtifact(schema_version="1.0", run_id=context.run_id, cells=cells)
        tool_paths: list[str] = []
        for artifact, entry in (
            (
                manifest,
                self._entry("xlsx/manifest.json", "xlsx_manifest", source_ids),
            ),
            (
                raw_cells,
                self._entry("xlsx/raw_cells.json", "xlsx_raw_cells", source_ids),
            ),
        ):
            path, write_error, fatal = self._write_artifact(artifact, entry, context)
            if path is not None:
                tool_paths.append(path)
            if write_error is not None:
                errors = _append_error(errors, write_error)
            if fatal:
                return self._failed(context.run_id, source_ids, errors, findings)

        context.trajectory_writer.append(
            TrajectoryEvent(
                schema_version="1.0",
                run_id=context.run_id,
                timestamp=self._clock(),
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=BranchCode.XLSX,
                event_type=TrajectoryEventType.TOOL_CALL_FINISHED,
                summary={
                    "tool": "xlsx_reader",
                    "workbook_count": len(workbooks),
                    "cell_count": len(cells),
                },
                duration_ms=_elapsed_ms(started_ns, self._clock_ns()),
                source_material_ids=source_ids,
                artifact_paths=tool_paths,
                error_ids=[item.error_id for item in errors],
            )
        )
        if not cells:
            error = context.diagnostics.record_error(
                code="xlsx_no_adoptable_result",
                severity="error",
                phase="branch_analysis",
                branch="xlsx",
                message="XLSX 读取没有形成可提交给模型的 canonical 单元格内容。",
                source_material_ids=source_ids,
                artifact_paths=tool_paths,
                suggested_action="检查 workbook 是否包含可读取的 sheet 和单元格。",
            )
            return self._failed(context.run_id, source_ids, _append_error(errors, error), findings)

        try:
            policy = context.model_client.policy_for(CallPurpose.XLSX)
        except (ModelCallUnavailable, KeyError, ValueError):
            error = context.diagnostics.record_error(
                code="xlsx_model_call_failed",
                severity="error",
                phase="branch_analysis",
                branch="xlsx",
                message="XLSX 模型调用策略或生产客户端未完成装配。",
                source_material_ids=source_ids,
                artifact_paths=tool_paths,
                suggested_action="完成已验证的模型策略与 endpoint 装配后创建新的运行。",
            )
            return self._failed(context.run_id, source_ids, _append_error(errors, error), findings)

        model_input = XlsxModelInput(workbooks=workbooks, cells=cells)
        request_artifact = XlsxModelRequestArtifact(
            schema_version="1.0",
            run_id=context.run_id,
            model=policy.model_id,
            timeout_seconds=policy.timeout_seconds,
            system_prompt=SYSTEM_PROMPT,
            analysis_instruction=ANALYSIS_INSTRUCTION,
            field_table=snapshot.field_table,
            xlsx_input=model_input,
            output_schema_version="1.0",
        )
        request_entry = self._entry("xlsx/model/request.json", "model_request", source_ids)
        request_path, write_error, fatal = self._write_artifact(
            request_artifact, request_entry, context
        )
        if write_error is not None:
            errors = _append_error(errors, write_error)
        if fatal or request_path is None:
            return self._failed(context.run_id, source_ids, errors, findings)

        request = ModelCallRequest(
            call_purpose=CallPurpose.XLSX,
            messages=(
                ModelMessage(role="system", content=(TextContentPart(SYSTEM_PROMPT),)),
                ModelMessage(
                    role="user",
                    content=(
                        TextContentPart(
                            _user_prompt(
                                snapshot.field_table.model_dump(mode="json"),
                                model_input.model_dump(mode="json"),
                            )
                        ),
                    ),
                ),
            ),
        )
        try:
            outcome = context.model_client.call(
                request,
                control=_ModelControl(context, source_ids, request_path, self._clock),
            )
        except ModelCallNotStarted:
            raise
        except ModelCallUnavailable:
            error = context.diagnostics.record_error(
                code="xlsx_model_call_failed",
                severity="error",
                phase="branch_analysis",
                branch="xlsx",
                message="XLSX 模型客户端在调用前不可用。",
                source_material_ids=source_ids,
                artifact_paths=[request_path],
                suggested_action="检查生产模型客户端装配后创建新的运行。",
            )
            return self._failed(context.run_id, source_ids, _append_error(errors, error), findings)

        if isinstance(outcome, ModelCallFailed):
            code = (
                "xlsx_model_input_too_large"
                if outcome.failure_category == FailureCategory.INPUT_TOO_LARGE
                else "xlsx_model_call_failed"
            )
            error = context.diagnostics.record_error(
                code=code,
                severity="error",
                phase="branch_analysis",
                branch="xlsx",
                message=(
                    "XLSX 模型输入超过已验证容量。"
                    if code == "xlsx_model_input_too_large"
                    else "XLSX 模型调用没有取得可解析响应。"
                ),
                source_material_ids=source_ids,
                artifact_paths=[request_path],
                suggested_action="检查输入规模或模型服务后创建新的运行。",
            )
            errors = _append_error(errors, error)
            context.model_calls.record(
                outcome,
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=BranchCode.XLSX,
                error_id=error.error_id,
                artifact_paths=[request_path],
            )
            return self._failed(context.run_id, source_ids, errors, findings)
        if not isinstance(outcome, ModelCallSucceeded):
            raise TypeError("model client returned an unsupported outcome")
        if (
            outcome.request_model_id != request_artifact.model
            or outcome.response_model_id != request_artifact.model
        ):
            error = context.diagnostics.record_error(
                code="xlsx_model_call_failed",
                severity="error",
                phase="branch_analysis",
                branch="xlsx",
                message="XLSX 模型响应身份与配对请求不一致。",
                source_material_ids=source_ids,
                artifact_paths=[request_path],
                suggested_action="检查模型 endpoint 与响应 model 配置后创建新的运行。",
            )
            errors = _append_error(errors, error)
            context.model_calls.record(
                outcome,
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=BranchCode.XLSX,
                error_id=error.error_id,
                artifact_paths=[request_path],
            )
            return self._failed(context.run_id, source_ids, errors, findings)

        response_artifact = XlsxModelResponseArtifact(
            schema_version="1.0",
            run_id=context.run_id,
            model=outcome.response_model_id,
            content=outcome.response_text,
            usage=(
                XlsxModelUsage(
                    input_tokens=outcome.usage.input_tokens,
                    output_tokens=outcome.usage.output_tokens,
                )
                if outcome.usage is not None
                else None
            ),
        )
        response_path, write_error, fatal = self._write_artifact(
            response_artifact,
            self._entry("xlsx/model/response.json", "model_response", source_ids),
            context,
        )
        if write_error is not None:
            errors = _append_error(errors, write_error)
        if fatal or response_path is None:
            context.model_calls.record(
                outcome,
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=BranchCode.XLSX,
                error_id=write_error.error_id if write_error is not None else None,
                artifact_paths=[request_path],
            )
            return self._failed(context.run_id, source_ids, errors, findings)

        parsed = self._parser.parse(
            outcome.response_text,
            field_table=snapshot.field_table,
            cells=tuple(cells),
        )
        protocol_error = None
        if parsed.rejections:
            protocol_error = context.diagnostics.record_error(
                code="xlsx_model_protocol_invalid",
                severity="error",
                phase="branch_analysis",
                branch="xlsx",
                message="XLSX 模型提交包含不符合协议的 envelope 或条目。",
                source_material_ids=source_ids,
                artifact_paths=[request_path, response_path],
                suggested_action="检查模型输出与 SUBMIT_XLSX 合同。",
            )
            errors = _append_error(errors, protocol_error)

        parsed_artifact = XlsxModelParsedArtifact(
            schema_version="1.0",
            run_id=context.run_id,
            status=_parsed_status(parsed),
            accepted_fields=list(parsed.fields),
            accepted_unknown_notes=list(parsed.unknown_notes),
            rejections=list(parsed.rejections),
            error_ids=[protocol_error.error_id] if protocol_error is not None else [],
        )
        parsed_path, write_error, fatal = self._write_artifact(
            parsed_artifact,
            self._entry("xlsx/model/parsed.json", "model_parsed", source_ids),
            context,
        )
        if write_error is not None:
            errors = _append_error(errors, write_error)
        model_paths = [request_path, response_path]
        if parsed_path is not None:
            model_paths.append(parsed_path)
        record_error = write_error or protocol_error
        context.model_calls.record(
            outcome,
            phase=PhaseCode.BRANCH_ANALYSIS,
            branch=BranchCode.XLSX,
            error_id=record_error.error_id if record_error is not None else None,
            artifact_paths=model_paths,
        )
        if fatal or parsed_path is None:
            return self._failed(context.run_id, source_ids, errors, findings)

        fields, unknown_notes = _to_branch_fields(parsed, snapshot)
        if not fields and not unknown_notes:
            if protocol_error is None:
                no_result = context.diagnostics.record_error(
                    code="xlsx_no_adoptable_result",
                    severity="error",
                    phase="branch_analysis",
                    branch="xlsx",
                    message="XLSX 模型提交没有形成可采纳的字段或未知说明。",
                    source_material_ids=source_ids,
                    artifact_paths=model_paths,
                    suggested_action="检查 workbook 内容和模型提交结果。",
                )
                errors = _append_error(errors, no_result)
            return self._failed(context.run_id, source_ids, errors, findings)

        status = (
            BranchResultStatus.COMPLETED_WITH_WARNINGS
            if errors or findings
            else BranchResultStatus.COMPLETED
        )
        return BranchResult(
            schema_version="1.0",
            run_id=context.run_id,
            branch=BranchCode.XLSX,
            status=status,
            source_material_ids=source_ids,
            fields=fields,
            unknown_notes=unknown_notes,
            tool_findings=findings,
            evidence=[],
            errors=errors,
        )

    @staticmethod
    def _entry(path: str, kind: str, source_ids: list[str]) -> ArtifactIndexEntry:
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
            source_material_ids=source_ids,
        )

    @staticmethod
    def _write_artifact(artifact, entry, context):
        try:
            return context.artifact_writer.write_json(artifact, entry), None, False
        except ServiceError as exc:
            if exc.error_id is None:
                raise
            error = context.diagnostics.resolve_error(exc.error_id)
            if exc.code == "artifact_index_update_failed":
                return entry.path, error, False
            return None, error, True

    @staticmethod
    def _failed(
        run_id: str,
        source_ids: list[str],
        errors: list[ErrorRecord],
        findings: list[ToolFinding],
    ) -> BranchResult:
        return BranchResult(
            schema_version="1.0",
            run_id=run_id,
            branch=BranchCode.XLSX,
            status=BranchResultStatus.FAILED,
            source_material_ids=source_ids,
            fields=[],
            unknown_notes=[],
            tool_findings=findings,
            evidence=[],
            errors=errors,
        )


def _user_prompt(field_table: dict, xlsx_input: dict) -> str:
    return "\n\n".join(
        (
            "XLSX 分析指示：\n" + ANALYSIS_INSTRUCTION,
            "完整字段表：\n" + json.dumps(field_table, ensure_ascii=False, separators=(",", ":")),
            "XLSX 解析内容：\n" + json.dumps(xlsx_input, ensure_ascii=False, separators=(",", ":")),
            "本次字段生产范围（直接派生自上述完整字段表）：\n"
            + _field_scope_instruction(field_table),
            "最终输出协议：\n" + OUTPUT_PROTOCOL,
        )
    )


def _field_scope_instruction(field_table: dict) -> str:
    definitions = field_table.get("fields", [])
    branch_codes = [
        item["field_code"]
        for item in definitions
        if item.get("producer_scope") == "branch_or_integration"
    ]
    integration_codes = [
        item["field_code"]
        for item in definitions
        if item.get("producer_scope") == "integration_only"
    ]
    return "\n".join(
        (
            "fields[] 可以提交（producer_scope=branch_or_integration）：" + "、".join(branch_codes),
            "fields[] 不得提交，只能由后续 Integration 生成（producer_scope=integration_only）："
            + "、".join(integration_codes),
        )
    )


def _parsed_status(parsed: ParsedSubmission):
    accepted = bool(parsed.fields or parsed.unknown_notes)
    if accepted and parsed.rejections:
        return "accepted_with_rejections"
    if accepted:
        return "accepted"
    return "rejected"


def _to_branch_fields(
    parsed: ParsedSubmission,
    snapshot: RunInputSnapshot,
) -> tuple[list[BranchField], list[BranchField]]:
    definitions = {item.field_code: item for item in snapshot.field_table.fields}
    workbook_order = {
        material_id: index
        for index, material_id in enumerate(snapshot.groups.spreadsheet_inputs, start=1)
    }

    accepted: list[tuple[tuple[int, int, tuple[int, int], int], BranchField]] = []
    for item_index, item in enumerate(parsed.fields):
        definition = definitions[item.field_code]
        key = _source_sort_key(item, workbook_order, item_index)
        accepted.append(
            (
                key,
                BranchField(
                    field_code=item.field_code,
                    field_name=definition.field_name,
                    field_value=item.field_value,
                    unit=item.unit,
                    category=definition.category,
                    branch_state=BranchFieldState(item.branch_state),
                    source="xlsx_ai",
                    source_material_id=item.source_material_id,
                    source_ref=_source_ref(item, workbook_order),
                    evidence_ids=[],
                    tool_finding_ids=[],
                    error_ids=[],
                    check_note=item.check_note,
                ),
            )
        )

    unknowns: list[tuple[tuple[int, int, tuple[int, int], int], BranchField]] = []
    for item_index, item in enumerate(parsed.unknown_notes):
        key = _source_sort_key(item, workbook_order, item_index)
        unknowns.append(
            (
                key,
                BranchField(
                    field_code="unknown_note_candidate",
                    field_name="未知说明候选",
                    field_value=item.field_value,
                    unit=None,
                    category=FieldCategory.BOARD,
                    branch_state=BranchFieldState.UNKNOWN,
                    source="xlsx_ai",
                    source_material_id=item.source_material_id,
                    source_ref=_source_ref(item, workbook_order),
                    evidence_ids=[],
                    tool_finding_ids=[],
                    error_ids=[],
                    check_note=item.check_note,
                ),
            )
        )
    return (
        [item for _, item in sorted(accepted, key=lambda pair: pair[0])],
        [item for _, item in sorted(unknowns, key=lambda pair: pair[0])],
    )


def _source_ref(
    item: XlsxSubmittedField | XlsxSubmittedUnknownNote,
    workbook_order: dict[str, int],
) -> str:
    return f"XLSX_{workbook_order[item.source_material_id]}_sheet{item.sheet_index}"


def _source_sort_key(item, workbook_order, item_index):
    return (
        workbook_order[item.source_material_id],
        item.sheet_index,
        _cell_address_key(item.source_cells[0]),
        item_index,
    )


def _cell_address_key(address: str) -> tuple[int, int]:
    split = 0
    while split < len(address) and address[split].isalpha():
        split += 1
    column = 0
    for letter in address[:split]:
        column = column * 26 + ord(letter) - ord("A") + 1
    return int(address[split:]), column


def _append_error(errors: list[ErrorRecord], error: ErrorRecord) -> list[ErrorRecord]:
    if any(item.error_id == error.error_id for item in errors):
        return errors
    return [*errors, error]


def _elapsed_ms(start_ns: int, end_ns: int) -> int:
    return max(0, (end_ns - start_ns) // 1_000_000)
