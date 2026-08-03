from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from flyboard.errors import ServiceError
from flyboard.model_calls import (
    FailureCategory,
    ModelCallFailed,
    ModelCallNotStarted,
    ModelCallRequest,
    ModelCallSucceeded,
    ModelMessage,
    TextContentPart,
)
from flyboard.models import (
    AccessLevel,
    ArtifactConsumer,
    ArtifactIndexEntry,
    BranchCode,
    BranchField,
    BranchResult,
    BranchResultStatus,
    CallPurpose,
    ConflictCandidate,
    ErrorRecord,
    FieldDefinition,
    FieldState,
    IntegrationResult,
    PhaseCode,
    ProjectDataListArtifact,
    ProjectDataRow,
    RunInputSnapshot,
    ToolFinding,
    TrajectoryEvent,
    TrajectoryEventType,
)
from flyboard.services._support import utc_now

if TYPE_CHECKING:
    from flyboard.workflow import AnalysisContext

from .models import (
    CandidateInput,
    DiagnosticInput,
    DiagnosticsInput,
    ErrorSummaryInput,
    EvidenceSummaryInput,
    FieldTask,
    ModelParsedArtifact,
    ModelRequestArtifact,
    ModelResponseArtifact,
    ModelUsageArtifact,
    SubmittedItem,
    ToolFindingSummaryInput,
)
from .parser import IntegrationResponseParser, ParsedBatch

SYSTEM_PROMPT = """你是 BoardFacts Agent 的多来源字段整合 Agent。

每个任务都给出一个正式字段、去标识化的可读资料或证据信息，以及当前字段必须遵守的 decision_rule。程序已经完成候选资格、稳定顺序、主显示候选和 canonical 引用关联；你的职责是根据完整字段表、提供的信息和 decision_rule 形成字段值、状态建议、本节点追加说明与当前完整结论。

必须遵守：
1. 以完整字段表中的物理语义和字段边界为准，不把不同物理语义字段互相合并或改写。
2. 严格执行每个任务的 decision_rule；不得重新选择来源、候选顺序或主显示候选。
3. compare 多候选应区分语义等价、确认真实差异和无法可靠确认等价，不得只按字符串判断；单候选按事实与规则形成结果。
4. summarize 按字段表口径保留不同来源的有效信息，不使用通用相等规则强行制造冲突。
5. diagnostic 只能说明已有诊断造成的缺值或可靠性限制，不生成工程值。
6. 已有 branch_state=conflict 时必须保留真实矛盾；已有 branch_state=unknown 时，单候选或摘要任务不得升级为 confirmed。只有多候选明确等价且至少一个候选为 clear_now 时，才允许结合完整上下文消除该未知。
7. 当前固定单位换算为 1 mil = 0.0254 mm；其他复合尺寸、列表、布尔、枚举和自由文本按字段语义判断。
8. 关系与状态必须严格配对：different 只能对应 conflict，uncertain 只能对应 unknown，diagnostic 只能对应 missing；单候选 compare 和 summarize 的 candidate_relation 必须为 null。不能准确判断时建议 unknown，只有明确确认真实值或事实互不相容时才建议 conflict。
9. 不执行工厂能力、报价或规则库判断，不输出完整推理过程。除 diagnostic 外，field_value 必须是非空字符串；different/conflict 也必须保留固定主候选表达作为主显示值。
10. append_check_note 必须使用简短中文，说明哪份可读资料或证据提供了什么事实、根据什么规则形成什么判断，以及该判断对字段结果的影响。不得回显 field_code、operation、候选序号、状态枚举、资料/证据/finding/error id、source_ref、路由、路径或其他协议字段。
11. check_conclusion 必须是可直接展示的中文完整判断，不得只返回状态词。append_check_note 和 check_conclusion 都不得出现 clear_now、confirmed、unknown、conflict、missing 等英文状态枚举，也不得描述“字段状态建议”为某个协议值；请改用自然中文说明业务判断。
12. 响应必须是首尾为大括号的单个裸 JSON 对象，不得使用 Markdown code fence 或附加说明。

按调用方附带的唯一响应协议提交结果。"""
OUTPUT_PROTOCOL = {
    "batch_id": "请求 batch_id",
    "items": [
        {
            "field_code": "请求字段编码",
            "field_value": "非空字符串；diagnostic 使用 null",
            "candidate_relation": "equivalent | different | uncertain | null",
            "suggested_field_state": "confirmed | unknown | conflict | missing",
            "append_check_note": "根据可读信息和 decision_rule 形成的简短中文追加说明",
            "check_conclusion": "当前完整中文结论",
        }
    ],
}


@dataclass(slots=True)
class _CallControl:
    context: AnalysisContext
    request_path: str
    batch_id: str
    source_ids: list[str]

    def allow_attempt(self, attempt_number: Literal[1, 2]) -> bool:
        if attempt_number == 2:
            return not self.context.stop_signal.is_requested()
        self.context.stop_signal.check_boundary()
        self.context.trajectory_writer.append(
            TrajectoryEvent(
                schema_version="1.0",
                run_id=self.context.run_id,
                timestamp=utc_now(),
                phase=PhaseCode.INTEGRATION,
                branch=None,
                event_type=TrajectoryEventType.MODEL_REQUEST_STARTED,
                summary={"call_purpose": CallPurpose.INTEGRATION.value, "batch_id": self.batch_id},
                source_material_ids=self.source_ids,
                artifact_paths=[self.request_path],
                error_ids=[],
            )
        )
        return True


@dataclass(frozen=True, slots=True)
class _BatchOutcome:
    accepted: dict[str, SubmittedItem]
    fallback_codes: tuple[str, ...]
    field_error_ids: dict[str, str]
    errors: tuple[ErrorRecord, ...]


@dataclass(frozen=True, slots=True)
class _FieldInput:
    branch: BranchCode
    field_index: int
    field: BranchField


@dataclass(frozen=True, slots=True)
class _TaskBinding:
    definition: FieldDefinition
    candidate_fields: tuple[BranchField, ...] = ()
    diagnostic_fields: tuple[BranchField, ...] = ()
    source_material_ids: tuple[str, ...] = ()
    source_override: str | None = None
    extra_error_ids: tuple[str, ...] = ()
    preserve_value: bool = False
    fixed_value: str | None = None
    fixed_state: FieldState | None = None
    field_name_override: str | None = None


class SingleXlsxIntegrator:
    """Production integrator for the currently implemented v1 source branches."""

    def __init__(self, *, parser: IntegrationResponseParser | None = None) -> None:
        self._parser = parser or IntegrationResponseParser()

    def merge(
        self,
        snapshot: RunInputSnapshot,
        branch_results: list[BranchResult],
        tool_findings: list[ToolFinding],
        context: AnalysisContext,
    ) -> IntegrationResult:
        validation_error, refs = self._validate_inputs(
            snapshot, branch_results, tool_findings, context
        )
        if validation_error is not None:
            return IntegrationResult(
                draft_project_data_list=[],
                project_data_list=None,
                tool_findings=(
                    tool_findings
                    if len({item.finding_id for item in tool_findings}) == len(tool_findings)
                    else []
                ),
                errors=[validation_error],
                evidence_index=None,
            )
        evidence_lookup, finding_lookup, error_lookup = refs
        deliverable = [
            result
            for result in branch_results
            if result.status
            in {BranchResultStatus.COMPLETED, BranchResultStatus.COMPLETED_WITH_WARNINGS}
        ]
        definitions = {item.field_code: item for item in snapshot.field_table.fields}
        grouped: dict[str, list[_FieldInput]] = defaultdict(list)
        for field_input in _ordered_field_inputs(snapshot, deliverable):
            grouped[field_input.field.field_code].append(field_input)

        tasks: list[FieldTask] = []
        bindings: dict[str, _TaskBinding] = {}
        for definition in snapshot.field_table.fields:
            built = _task(
                definition,
                grouped.get(definition.field_code, []),
                snapshot,
                evidence_lookup,
                finding_lookup,
                error_lookup,
            )
            if built is not None:
                task, binding = built
                tasks.append(task)
                bindings[task.field_code] = binding
        for task, binding in _system_tasks(snapshot, branch_results, definitions, error_lookup):
            tasks.append(task)
            bindings[task.field_code] = binding
        for task, binding in _core_missing_tasks(snapshot, definitions, set(bindings)):
            tasks.append(task)
            bindings[task.field_code] = binding
        for task, binding in _unknown_tasks(
            snapshot,
            definitions["unknown_note_###"],
            _ordered_unknown_notes(snapshot, deliverable),
            evidence_lookup,
        ):
            tasks.append(task)
            bindings[task.field_code] = binding

        accepted: dict[str, SubmittedItem] = {}
        fallback_codes: set[str] = set()
        field_error_ids: dict[str, str] = {}
        integration_errors: list[ErrorRecord] = []
        batch_number = 0
        for operation in ("compare", "summarize", "diagnostic"):
            operation_tasks = [item for item in tasks if item.operation == operation]
            for start in range(0, len(operation_tasks), 8):
                context.stop_signal.check_boundary()
                batch_number += 1
                outcome = self._run_batch(
                    snapshot,
                    operation_tasks[start : start + 8],
                    f"integration_{batch_number:04d}",
                    f"integration/model/batch_{batch_number:04d}",
                    context,
                    bindings,
                )
                accepted.update(outcome.accepted)
                fallback_codes.update(outcome.fallback_codes)
                field_error_ids.update(outcome.field_error_ids)
                integration_errors = _merge_errors(integration_errors, outcome.errors)
        try:
            rows = []
            for task in tasks:
                binding = bindings[task.field_code]
                item = accepted.get(task.field_code)
                error_id = field_error_ids.get(task.field_code)
                rows.append(
                    _fallback_row(binding, task, error_id)
                    if item is None or task.field_code in fallback_codes
                    else _accepted_row(binding, task, item, error_id)
                )
            rows = _final_order(rows, snapshot)
            _validate_draft(rows, snapshot)
            draft = ProjectDataListArtifact(schema_version="1.0", run_id=context.run_id, rows=rows)
        except (KeyError, TypeError, ValueError):
            error = context.diagnostics.record_error(
                code="integration_draft_invalid",
                severity="error",
                phase="integration",
                branch=None,
                message="规则前项目数据草稿未通过完整结构、引用或顺序校验。",
                source_material_ids=[],
                artifact_paths=[],
                suggested_action="检查字段表、分支引用和整合行构造合同。",
            )
            integration_errors = _merge_errors(integration_errors, (error,))
            return IntegrationResult(
                draft_project_data_list=[],
                project_data_list=None,
                tool_findings=tool_findings,
                errors=integration_errors,
                evidence_index=None,
            )
        draft_path, draft_error, draft_fatal = _write(
            context,
            draft,
            _entry(
                "integration/draft_project_data_list.json",
                "draft_project_data_list",
                PhaseCode.INTEGRATION,
                [],
            ),
        )
        if draft_error is not None:
            integration_errors = _merge_errors(integration_errors, (draft_error,))
        if draft_fatal or draft_path is None:
            return IntegrationResult(
                draft_project_data_list=[],
                project_data_list=None,
                tool_findings=tool_findings,
                errors=integration_errors,
                evidence_index=None,
            )
        return IntegrationResult(
            draft_project_data_list=rows,
            project_data_list=None,
            tool_findings=tool_findings,
            errors=integration_errors,
            evidence_index=None,
        )

    @staticmethod
    def _validate_inputs(snapshot, branch_results, tool_findings, context):
        expected = [
            (BranchCode.PDF_IMAGE, _branch_sources(snapshot, BranchCode.PDF_IMAGE)),
            (BranchCode.XLSX, _branch_sources(snapshot, BranchCode.XLSX)),
            (BranchCode.GERBER, _branch_sources(snapshot, BranchCode.GERBER)),
        ]
        expected = [(branch, sources) for branch, sources in expected if sources]
        findings = [item for result in branch_results for item in result.tool_findings]
        errors = [item for result in branch_results for item in result.errors]
        definitions = {item.field_code: item for item in snapshot.field_table.fields}
        required = {
            "project_analysis_findings",
            "pdf_presence",
            "spreadsheet_presence",
            "gerber_job_presence",
            "drill_file_presence",
        }
        valid = (
            snapshot.run_id == context.run_id
            and len(branch_results) == len(expected)
            and any(
                result.status
                in {BranchResultStatus.COMPLETED, BranchResultStatus.COMPLETED_WITH_WARNINGS}
                for result in branch_results
            )
            and all(
                result.run_id == context.run_id
                and result.branch == branch
                and result.source_material_ids == sources
                for result, (branch, sources) in zip(branch_results, expected, strict=True)
            )
            and findings == tool_findings
            and len({item.finding_id for item in findings}) == len(findings)
            and len({item.error_id for item in errors}) == len(errors)
            and required.issubset(definitions)
        )
        if valid:
            for result in branch_results:
                for field in result.fields:
                    definition = definitions.get(field.field_code)
                    if (
                        definition is None
                        or definition.producer_scope.value != "branch_or_integration"
                        or field.field_name != definition.field_name
                        or field.category != definition.category
                        or field.unit != definition.unit
                    ):
                        valid = False
                        break
                if not valid:
                    break
        evidence_lookup = {}
        finding_lookup = {}
        error_lookup = {}
        if valid:
            try:
                evidence_lookup = _evidence_lookup(branch_results)
                finding_lookup = {item.finding_id: item for item in findings}
                error_lookup = {item.error_id: item for item in errors}
                for error_id, error in error_lookup.items():
                    if context.diagnostics.resolve_error(error_id) is not error:
                        raise ValueError("error identity mismatch")
            except (KeyError, ValueError, RuntimeError):
                valid = False
        if valid:
            return None, (evidence_lookup, finding_lookup, error_lookup)
        error = context.diagnostics.record_error(
            code="integration_input_contract_invalid",
            severity="error",
            phase="integration",
            branch=None,
            message="整合输入的分支身份、稳定顺序或业务引用集合不一致。",
            source_material_ids=[],
            artifact_paths=[],
            suggested_action="检查分支 barrier、字段表、诊断身份和 EvidenceSeed 合同。",
        )
        return error, ({}, {}, {})

    def _run_batch(
        self, snapshot, tasks, batch_id, path_prefix, context, bindings
    ) -> _BatchOutcome:
        first = self._call_once(snapshot, tasks, batch_id, path_prefix, context, bindings, None)
        if first[0] is None:
            error = first[2]
            if error is None:
                raise RuntimeError("failed integration call must return an error")
            return _fallback_outcome(tasks, error, first[3])

        parsed, error, recorded_errors = first[0], first[2], first[3]
        accepted = {item.field_code: item for item in parsed.accepted}
        if not parsed.rejections:
            return _BatchOutcome(accepted, (), {}, tuple(recorded_errors))
        if error is None:
            raise RuntimeError("rejected integration response must return an error")
        if not parsed.envelope_locatable:
            return _fallback_outcome(tasks, error, recorded_errors, accepted)

        field_errors = {code: error.error_id for code in parsed.affected_field_codes}
        fallback: list[str] = []
        errors = list(recorded_errors)
        task_by_code = {task.field_code: task for task in tasks}
        for field_code in parsed.affected_field_codes:
            retry_task = task_by_code[field_code]
            retry_id = f"{batch_id}_retry_{field_code}"
            retry_prefix = f"{path_prefix}/retry/{field_code}"
            retry = self._call_once(
                snapshot,
                [retry_task],
                retry_id,
                retry_prefix,
                context,
                bindings,
                error,
            )
            errors = _merge_errors(errors, retry[3])
            retry_parsed = retry[0]
            if retry_parsed is not None and len(retry_parsed.accepted) == 1:
                accepted[field_code] = retry_parsed.accepted[0]
            else:
                fallback.append(field_code)
        return _BatchOutcome(accepted, tuple(fallback), field_errors, tuple(errors))

    def _call_once(
        self,
        snapshot,
        tasks,
        batch_id,
        path_prefix,
        context,
        bindings,
        shared_error: ErrorRecord | None,
    ):
        source_ids = _source_ids(tasks, bindings)
        policy = context.model_client.policy_for(CallPurpose.INTEGRATION)
        request_artifact = ModelRequestArtifact(
            schema_version="1.0",
            run_id=context.run_id,
            model=policy.model_id,
            timeout_seconds=policy.timeout_seconds,
            batch_id=batch_id,
            operation=tasks[0].operation,
            system_prompt=SYSTEM_PROMPT,
            field_table=snapshot.field_table,
            tasks=tasks,
            output_schema_version="1.0",
        )
        request_path, write_error, fatal = _write(
            context,
            request_artifact,
            _entry(
                f"{path_prefix}/request.json", "model_request", PhaseCode.INTEGRATION, source_ids
            ),
            shared_error,
        )
        errors = [write_error] if write_error is not None else []
        effective_error = shared_error or write_error
        if fatal or request_path is None:
            return None, None, effective_error, errors

        prompt = _prompt(batch_id, snapshot, tasks)
        request = ModelCallRequest(
            call_purpose=CallPurpose.INTEGRATION,
            messages=(
                ModelMessage(role="system", content=(TextContentPart(SYSTEM_PROMPT),)),
                ModelMessage(role="user", content=(TextContentPart(prompt),)),
            ),
        )
        try:
            outcome = context.model_client.call(
                request,
                control=_CallControl(context, request_path, batch_id, source_ids),
            )
        except ModelCallNotStarted:
            raise
        if isinstance(outcome, ModelCallFailed):
            if effective_error is None:
                effective_error = context.diagnostics.record_error(
                    code=(
                        "integration_model_input_too_large"
                        if outcome.failure_category == FailureCategory.INPUT_TOO_LARGE
                        else "integration_model_call_failed"
                    ),
                    severity="error",
                    phase="integration",
                    branch=None,
                    message="整合模型调用未形成可提交响应，字段使用保守降级。",
                    source_material_ids=source_ids,
                    artifact_paths=[request_path],
                    suggested_action="复核降级字段并检查模型服务。",
                )
                errors.append(effective_error)
            context.model_calls.record(
                outcome,
                phase=PhaseCode.INTEGRATION,
                branch=None,
                error_id=effective_error.error_id,
                artifact_paths=[request_path],
            )
            return None, outcome, effective_error, errors
        if not isinstance(outcome, ModelCallSucceeded):
            raise TypeError("model client returned an unsupported integration outcome")

        response_artifact = ModelResponseArtifact(
            schema_version="1.0",
            run_id=context.run_id,
            model=outcome.response_model_id,
            batch_id=batch_id,
            content=outcome.response_text,
            usage=(
                ModelUsageArtifact(
                    input_tokens=outcome.usage.input_tokens,
                    output_tokens=outcome.usage.output_tokens,
                )
                if outcome.usage is not None
                else None
            ),
        )
        response_path, response_error, fatal = _write(
            context,
            response_artifact,
            _entry(
                f"{path_prefix}/response.json",
                "model_response",
                PhaseCode.INTEGRATION,
                source_ids,
            ),
            effective_error,
        )
        if response_error is not None:
            errors = _merge_errors(errors, (response_error,))
            effective_error = effective_error or response_error
        if fatal or response_path is None:
            context.model_calls.record(
                outcome,
                phase=PhaseCode.INTEGRATION,
                branch=None,
                error_id=effective_error.error_id if effective_error is not None else None,
                artifact_paths=[request_path],
            )
            return None, outcome, effective_error, errors

        parsed = self._parser.parse(outcome.response_text, batch_id=batch_id, tasks=tasks)
        if parsed.rejections and effective_error is None:
            effective_error = context.diagnostics.record_error(
                code="integration_model_protocol_invalid",
                severity="error",
                phase="integration",
                branch=None,
                message="整合模型响应包含无效 envelope 或字段项。",
                source_material_ids=source_ids,
                artifact_paths=[request_path, response_path],
                suggested_action="复核降级字段并检查整合响应协议。",
            )
            errors.append(effective_error)
        parsed_artifact = ModelParsedArtifact(
            schema_version="1.0",
            run_id=context.run_id,
            batch_id=batch_id,
            status=_parsed_status(parsed),
            accepted_items=list(parsed.accepted),
            rejections=list(parsed.rejections),
            fallback_field_codes=(
                list(parsed.affected_field_codes) if not parsed.envelope_locatable else []
            ),
            error_ids=[effective_error.error_id] if effective_error is not None else [],
        )
        parsed_path, parsed_error, fatal = _write(
            context,
            parsed_artifact,
            _entry(
                f"{path_prefix}/parsed.json",
                "model_parsed",
                PhaseCode.INTEGRATION,
                source_ids,
            ),
            effective_error,
        )
        if parsed_error is not None:
            errors = _merge_errors(errors, (parsed_error,))
            effective_error = effective_error or parsed_error
        paths = [request_path, response_path]
        if parsed_path is not None:
            paths.append(parsed_path)
        context.model_calls.record(
            outcome,
            phase=PhaseCode.INTEGRATION,
            branch=None,
            error_id=effective_error.error_id if effective_error is not None else None,
            artifact_paths=paths,
        )
        if fatal or parsed_path is None:
            return None, outcome, effective_error, errors
        return parsed, outcome, effective_error, errors


def _ordered_field_inputs(snapshot, branch_results):
    material_order = {item.material_id: item.material_order for item in snapshot.materials}
    values = [
        _FieldInput(result.branch, field_index, field)
        for result in branch_results
        for field_index, field in enumerate(result.fields)
    ]
    values.sort(key=lambda item: (material_order[item.field.source_material_id], item.field_index))
    return values


def _ordered_unknown_notes(snapshot, branch_results):
    material_order = {item.material_id: item.material_order for item in snapshot.materials}
    values = [
        (material_order[note.source_material_id], note_index, note)
        for result in branch_results
        for note_index, note in enumerate(result.unknown_notes)
    ]
    values.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in values]


def _task(definition, inputs, snapshot, evidence_lookup, finding_lookup, error_lookup):
    value_inputs = [item for item in inputs if item.field.field_value is not None]
    diagnostic_inputs = [
        item
        for item in inputs
        if item.branch == BranchCode.GERBER
        and item.field.field_value is None
        and item.field.branch_state.value == "missing"
        and (item.field.tool_finding_ids or item.field.error_ids)
    ]
    if value_inputs:
        operation = (
            "summarize"
            if definition.field_code in {"project_instructions", "panelization_summary"}
            else "compare"
        )
        primary = 0
    elif diagnostic_inputs:
        operation = "diagnostic"
        primary = None
    else:
        return None
    candidate_fields = tuple(item.field for item in value_inputs)
    diagnostic_fields = tuple(item.field for item in diagnostic_inputs)
    candidates = [
        CandidateInput(
            candidate_index=index,
            field_value=item.field.field_value,
            unit=item.field.unit,
            branch_state=item.field.branch_state.value,
            source_label=_field_source_label(snapshot, item.field),
            check_note=item.field.check_note,
            evidence_summaries=_evidence_summaries(item.field, evidence_lookup, snapshot),
        )
        for index, item in enumerate(value_inputs)
    ]
    diagnostics = [
        DiagnosticInput(
            diagnostic_index=index,
            source_label=_field_source_label(snapshot, item.field),
            branch_state="missing",
            check_note=item.field.check_note,
            evidence_summaries=_evidence_summaries(item.field, evidence_lookup, snapshot),
        )
        for index, item in enumerate(diagnostic_inputs)
    ]
    referenced = [*candidate_fields, *diagnostic_fields]
    finding_ids = _stable_union(item.tool_finding_ids for item in referenced)
    error_ids = _stable_union(item.error_ids for item in referenced)
    task = FieldTask(
        field_code=definition.field_code,
        operation=operation,
        decision_rule=_standard_decision_rule(definition, operation, candidates),
        fixed_primary_candidate=primary,
        ordered_candidates=candidates,
        diagnostics=DiagnosticsInput(
            ordered_inputs=diagnostics,
            tool_findings=[
                ToolFindingSummaryInput(message=finding_lookup[item].message)
                for item in finding_ids
            ],
            errors=[
                ErrorSummaryInput(
                    message=error_lookup[item].message,
                    suggested_action=error_lookup[item].suggested_action,
                )
                for item in error_ids
            ],
        ),
    )
    binding = _TaskBinding(
        definition=definition,
        candidate_fields=candidate_fields,
        diagnostic_fields=diagnostic_fields,
        source_material_ids=tuple(_stable_union(item.source_material_id for item in referenced)),
    )
    return task, binding


def _standard_decision_rule(definition, operation, candidates):
    unit_rule = (
        f"最终值使用字段定义单位 {definition.unit}。"
        if definition.unit is not None
        else "字段定义没有独立单位，保留完整工程表达。"
    )
    if operation == "diagnostic":
        return (
            f"这是“{definition.field_name}”缺值诊断：不得生成工程值，状态必须为 missing；"
            "根据可读诊断说明缺失原因、结果影响和工程师需要补充或复核的内容。"
        )
    if operation == "summarize":
        return (
            f"按“{definition.field_name}”的字段含义汇总全部候选，保留有效信息且不强行制造冲突。"
            + unit_rule
        )
    if len(candidates) == 1 and candidates[0].branch_state == "unknown":
        return (
            f"“{definition.field_name}”已有一个非空但可靠性不足的候选：必须保留固定主候选表达，"
            "状态保持 unknown，不得返回 null 或 missing；同一任务中的工具诊断只用于说明该值为何"
            "暂不可靠以及需要复核什么，不能把有值候选改成缺值诊断。" + unit_rule
        )
    return (
        f"按“{definition.field_name}”的物理含义比较全部候选；语义等价时采用固定主候选表达，"
        "明确不相容时标记冲突，无法可靠判断时保持未知。" + unit_rule
    )


def _field_source_label(snapshot, field):
    material = next(
        item for item in snapshot.materials if item.material_id == field.source_material_id
    )
    kind_label = {
        "pdf": "PDF 制造图",
        "image": "工程图片",
        "spreadsheet": "技术表",
        "gerber_job": "Gerber/Drill 资料",
        "unsupported": "未支持资料",
    }[material.kind.value]
    if material.kind.value == "gerber_job":
        return f"第 {material.material_order} 组{kind_label}"
    display_name = material.source_ref.rsplit("/", 1)[-1]
    return f"第 {material.material_order} 份{kind_label}“{display_name}”"


def _evidence_summaries(field, evidence_lookup, snapshot):
    values = []
    for evidence_id in field.evidence_ids:
        seed = evidence_lookup[evidence_id]
        source_label = _field_source_label(snapshot, field)
        if seed.type == "pdf_page":
            description = f"{source_label}第 {seed.page_index} 页的可复核证据"
        elif seed.type == "image":
            description = f"{source_label}第 {seed.source_image_index} 张图的可复核证据"
        else:
            description = f"{source_label}的分析结果证据"
        values.append(EvidenceSummaryInput(description=description, note=seed.note))
    return values


def _prompt(batch_id, snapshot, tasks) -> str:
    data = {
        "batch": {"batch_id": batch_id, "operation": tasks[0].operation},
        "field_table": snapshot.field_table.model_dump(mode="json"),
        "field_tasks": [item.model_dump(mode="json") for item in tasks],
        "output_protocol": OUTPUT_PROTOCOL,
    }
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _accepted_row(binding, task, item, error_id):
    candidates = task.ordered_candidates
    if binding.fixed_state is not None:
        state = binding.fixed_state
    elif task.operation == "diagnostic":
        state = FieldState.MISSING
    else:
        states = {candidate.branch_state for candidate in candidates}
        relation = item.candidate_relation
        if task.operation == "compare" and len(candidates) > 1 and relation == "different":
            state = FieldState.CONFLICT
        elif "conflict" in states or item.suggested_field_state == "conflict":
            state = FieldState.CONFLICT
        elif relation == "uncertain":
            state = FieldState.UNKNOWN
        elif task.operation == "summarize" and "unknown" in states:
            state = FieldState.UNKNOWN
        elif len(candidates) == 1 and candidates[0].branch_state == "unknown":
            state = FieldState.UNKNOWN
        else:
            state = FieldState(item.suggested_field_state)
    conflict_candidates = (
        [
            ConflictCandidate(
                field_value=candidate.field_value,
                source_material_id=candidate.source_material_id,
                source_ref=candidate.source_ref,
                evidence_ids=list(candidate.evidence_ids),
            )
            for candidate in binding.candidate_fields
        ]
        if item.candidate_relation == "different" and len(binding.candidate_fields) > 1
        else []
    )
    evidence = (
        _stable_union(candidate.evidence_ids for candidate in conflict_candidates)
        if conflict_candidates
        else _task_evidence_ids(binding)
    )
    notes = [*_upstream_notes(binding), item.append_check_note.strip()]
    return ProjectDataRow(
        index=1,
        field_code=task.field_code,
        field_name=binding.field_name_override or binding.definition.field_name,
        field_value=binding.fixed_value if binding.preserve_value else item.field_value,
        source=_task_source(binding),
        evidence_ids=evidence,
        tool_finding_ids=_task_tool_finding_ids(binding),
        error_ids=_task_error_ids(binding, error_id),
        conflict_candidates=conflict_candidates,
        category=binding.definition.category,
        field_state=state,
        check_note="\n".join(note for note in notes if note),
        check_conclusion=item.check_conclusion,
    )


def _fallback_row(binding, task, error_id):
    candidates = task.ordered_candidates
    if binding.fixed_state is not None:
        state = binding.fixed_state
    elif task.operation == "diagnostic":
        state = FieldState.MISSING
    else:
        state = (
            FieldState.CONFLICT
            if any(item.branch_state == "conflict" for item in candidates)
            else FieldState.UNKNOWN
        )
    if binding.preserve_value:
        value = binding.fixed_value
    elif task.operation == "diagnostic":
        value = None
    elif task.operation == "summarize":
        value = "；".join(item.field_value for item in candidates)
    else:
        value = candidates[0].field_value
    return ProjectDataRow(
        index=1,
        field_code=task.field_code,
        field_name=binding.field_name_override or binding.definition.field_name,
        field_value=value,
        source=_task_source(binding),
        evidence_ids=_task_evidence_ids(binding),
        tool_finding_ids=_task_tool_finding_ids(binding),
        error_ids=_task_error_ids(binding, error_id),
        conflict_candidates=[],
        category=binding.definition.category,
        field_state=state,
        check_note="\n".join(_upstream_notes(binding)),
        check_conclusion="整合模型未形成合法判断，需要工程师结合运行问题复核。",
    )


def _binding_fields(binding):
    return [*binding.candidate_fields, *binding.diagnostic_fields]


def _upstream_notes(binding):
    return [item.check_note.strip() for item in _binding_fields(binding) if item.check_note.strip()]


def _task_source(binding):
    if binding.source_override is not None:
        return binding.source_override
    fields = _binding_fields(binding)
    if not fields:
        raise ValueError("task binding requires a canonical source")
    return fields[0].source_ref


def _task_evidence_ids(binding):
    return _stable_union(item.evidence_ids for item in _binding_fields(binding))


def _task_tool_finding_ids(binding):
    return _stable_union(item.tool_finding_ids for item in _binding_fields(binding))


def _task_error_ids(binding, error_id):
    values = _stable_union(
        [item.error_ids for item in _binding_fields(binding)] + [list(binding.extra_error_ids)]
    )
    if error_id is not None and error_id not in values:
        values.append(error_id)
    return values


def _system_tasks(snapshot, branch_results, definitions, error_lookup):
    by_branch = {result.branch: result for result in branch_results}
    all_error_ids = tuple(
        _stable_union(error.error_id for result in branch_results for error in result.errors)
    )
    analysis_ok = (
        all(result.status == BranchResultStatus.COMPLETED for result in branch_results)
        and not snapshot.groups.unsupported_materials
    )
    processable_count = (
        len(snapshot.groups.pdf_inputs)
        + len(snapshot.groups.image_inputs)
        + len(snapshot.groups.spreadsheet_inputs)
        + len(snapshot.groups.gerber_jobs)
    )
    counts = {
        status: sum(result.status == status for result in branch_results)
        for status in BranchResultStatus
    }
    value = (
        f"本次选择 {len(snapshot.materials)} 项资料，其中 {processable_count} 项进入分析；"
        f"未支持或无法识别资料 {len(snapshot.groups.unsupported_materials)} 项；"
        f"参与的分析分支中，{counts[BranchResultStatus.COMPLETED]} 个正常完成，"
        f"{counts[BranchResultStatus.COMPLETED_WITH_WARNINGS]} 个部分完成，"
        f"{counts[BranchResultStatus.FAILED]} 个分析失败。"
    )
    definition = definitions["project_analysis_findings"]
    analysis_state = FieldState.CONFIRMED if analysis_ok else FieldState.CONFLICT
    task = _single_candidate_task(
        definition,
        value=value,
        branch_state="clear_now" if analysis_ok else "conflict",
        source_label="；".join(
            _material_analysis_fact(item, snapshot, by_branch) for item in snapshot.materials
        ),
        decision_rule=(
            "汇总本次资料处理事实：全部可处理资料正常完成且没有未支持资料时确认；"
            "存在未支持、部分完成或失败时标记冲突，并说明这些问题对其余可用结果的影响。"
        ),
        errors=[
            ErrorSummaryInput(
                message=error_lookup[item].message,
                suggested_action=error_lookup[item].suggested_action,
            )
            for item in all_error_ids
        ],
    )
    values = [
        (
            task,
            _TaskBinding(
                definition=definition,
                source_material_ids=tuple(item.material_id for item in snapshot.materials),
                source_override="integration",
                extra_error_ids=all_error_ids,
                preserve_value=True,
                fixed_value=value,
                fixed_state=analysis_state,
            ),
        )
    ]
    presence = (
        ("pdf_presence", _branch_sources(snapshot, BranchCode.PDF_IMAGE), BranchCode.PDF_IMAGE),
        ("spreadsheet_presence", _branch_sources(snapshot, BranchCode.XLSX), BranchCode.XLSX),
        ("gerber_job_presence", _branch_sources(snapshot, BranchCode.GERBER), BranchCode.GERBER),
        (
            "drill_file_presence",
            [
                item.material_id
                for item in snapshot.materials
                if item.material_id in snapshot.groups.gerber_jobs
                and item.member_kinds
                and "drill" in item.member_kinds
            ],
            BranchCode.GERBER,
        ),
    )
    for code, source_ids, branch_code in presence:
        result = by_branch.get(branch_code)
        if not source_ids:
            item_value, state, error_ids, status_label = (
                "未提供/未参与本次分析",
                FieldState.CONFIRMED,
                (),
                "本次未提供该类资料",
            )
        elif result is not None and result.status == BranchResultStatus.COMPLETED:
            item_value, state, error_ids, status_label = (
                "存在，分析完成",
                FieldState.CONFIRMED,
                (),
                "该类资料已正常完成分析",
            )
        elif result is not None and result.status == BranchResultStatus.COMPLETED_WITH_WARNINGS:
            item_value, state, error_ids, status_label = (
                "存在，部分分析完成",
                FieldState.CONFLICT,
                tuple(item.error_id for item in result.errors),
                "该类资料仅部分完成分析",
            )
        else:
            item_value, state, error_ids, status_label = (
                "存在，分析失败",
                FieldState.CONFLICT,
                tuple(item.error_id for item in result.errors) if result is not None else (),
                "该类资料分析失败",
            )
        definition = definitions[code]
        labels = [
            _readable_material_label(item)
            for item in snapshot.materials
            if item.material_id in source_ids
        ]
        source_label = f"{'、'.join(labels)}：{status_label}" if labels else status_label
        presence_task = _single_candidate_task(
            definition,
            value=item_value,
            branch_state="clear_now" if state == FieldState.CONFIRMED else "conflict",
            source_label=source_label,
            decision_rule=(
                "根据是否提供及实际分析结果形成资料存在性判断：未提供或正常完成时确认；"
                "部分完成或失败时标记冲突，并说明原因和对本次结果完整性的影响。"
            ),
            errors=[
                ErrorSummaryInput(
                    message=error_lookup[item].message,
                    suggested_action=error_lookup[item].suggested_action,
                )
                for item in error_ids
            ],
        )
        values.append(
            (
                presence_task,
                _TaskBinding(
                    definition=definition,
                    source_material_ids=tuple(source_ids),
                    source_override="integration",
                    extra_error_ids=error_ids,
                    preserve_value=True,
                    fixed_value=item_value,
                    fixed_state=state,
                ),
            )
        )
    return values


def _single_candidate_task(
    definition,
    *,
    value,
    branch_state,
    source_label,
    decision_rule,
    errors=None,
):
    return FieldTask(
        field_code=definition.field_code,
        operation="compare",
        decision_rule=decision_rule,
        fixed_primary_candidate=0,
        ordered_candidates=[
            CandidateInput(
                candidate_index=0,
                field_value=value,
                unit=definition.unit,
                branch_state=branch_state,
                source_label=source_label,
                check_note="",
                evidence_summaries=[],
            )
        ],
        diagnostics=DiagnosticsInput(ordered_inputs=[], tool_findings=[], errors=errors or []),
    )


def _material_analysis_fact(material, snapshot, by_branch):
    label = _readable_material_label(material)
    if material.material_id in snapshot.groups.unsupported_materials:
        reason = {
            "unsupported_file_format": "文件格式暂不支持",
            "image_format_mismatch": "图片格式与内容不一致",
            "image_decode_failed": "图片无法读取",
            "pdf_invalid": "PDF 内容无效",
            "xlsx_invalid": "技术表内容无效",
            "design_file_unrecognized": "设计文件无法识别",
            "gerber_job_ambiguous": "Gerber 作业边界无法确认",
        }.get(material.unsupported_reason_code.value, "当前无法参与分析")
        return f"{label}未进入分析，原因是{reason}"
    branch = next(
        item
        for item in (BranchCode.PDF_IMAGE, BranchCode.XLSX, BranchCode.GERBER)
        if material.material_id in _branch_sources(snapshot, item)
    )
    result = by_branch.get(branch)
    status = {
        BranchResultStatus.COMPLETED: "已正常完成分析",
        BranchResultStatus.COMPLETED_WITH_WARNINGS: "已部分完成分析",
        BranchResultStatus.FAILED: "分析失败",
    }.get(result.status if result is not None else BranchResultStatus.FAILED, "分析失败")
    return f"{label}{status}"


def _readable_material_label(material):
    kind_label = {
        "pdf": "PDF 制造图",
        "image": "工程图片",
        "spreadsheet": "技术表",
        "gerber_job": "Gerber/Drill 资料",
        "unsupported": "未支持资料",
    }[material.kind.value]
    if material.kind.value == "gerber_job":
        return f"第 {material.material_order} 组{kind_label}"
    return f"第 {material.material_order} 份{kind_label}“{material.source_ref.rsplit('/', 1)[-1]}”"


def _core_missing_tasks(snapshot, definitions, existing):
    values = []
    for definition in snapshot.field_table.fields:
        if not definition.is_core or definition.field_code in existing:
            continue
        task = FieldTask(
            field_code=definition.field_code,
            operation="diagnostic",
            decision_rule=(
                f"“{definition.field_name}”没有任何可采纳真实值或正式诊断结果；不得生成工程值，"
                "状态必须为 missing，并说明缺失对检查的影响和工程师需要补充的内容。"
            ),
            fixed_primary_candidate=None,
            ordered_candidates=[],
            diagnostics=DiagnosticsInput(
                ordered_inputs=[
                    DiagnosticInput(
                        diagnostic_index=0,
                        source_label="本次全部可采纳工程资料",
                        branch_state="missing",
                        check_note="",
                        evidence_summaries=[],
                    )
                ],
                tool_findings=[],
                errors=[],
            ),
        )
        values.append(
            (
                task,
                _TaskBinding(
                    definition=definition,
                    source_override="integration",
                    preserve_value=True,
                    fixed_value=None,
                    fixed_state=FieldState.MISSING,
                ),
            )
        )
    return values


def _unknown_tasks(snapshot, definition, notes, evidence_lookup):
    values = []
    for number, note in enumerate(notes, start=1):
        field_code = f"unknown_note_{number:03d}"
        task = FieldTask(
            field_code=field_code,
            operation="compare",
            decision_rule=(
                "保留候选中的原始工程表达，状态必须为 unknown；根据可读来源、上游说明和证据摘要，"
                "说明该内容具有何种生产意义、为何当前不能归入正式字段以及工程师需要确认什么。"
            ),
            fixed_primary_candidate=0,
            ordered_candidates=[
                CandidateInput(
                    candidate_index=0,
                    field_value=note.field_value,
                    unit=note.unit,
                    branch_state="unknown",
                    source_label=_field_source_label(snapshot, note),
                    check_note=note.check_note,
                    evidence_summaries=_evidence_summaries(note, evidence_lookup, snapshot),
                )
            ],
            diagnostics=DiagnosticsInput(ordered_inputs=[], tool_findings=[], errors=[]),
        )
        values.append(
            (
                task,
                _TaskBinding(
                    definition=definition,
                    candidate_fields=(note,),
                    source_material_ids=(note.source_material_id,),
                    preserve_value=True,
                    fixed_value=note.field_value,
                    fixed_state=FieldState.UNKNOWN,
                    field_name_override=f"未知说明_{number}",
                ),
            )
        )
    return values


def _final_order(rows, snapshot):
    order = {item.field_code: index for index, item in enumerate(snapshot.field_table.fields)}
    fixed = [item for item in rows if not item.field_code.startswith("unknown_note_")]
    unknown = [item for item in rows if item.field_code.startswith("unknown_note_")]
    fixed.sort(key=lambda item: order[item.field_code])
    ordered = [*fixed, *unknown]
    return [item.model_copy(update={"index": index}) for index, item in enumerate(ordered, start=1)]


def _validate_draft(rows, snapshot):
    order = {item.field_code: index for index, item in enumerate(snapshot.field_table.fields)}
    fixed_codes = [
        item.field_code for item in rows if not item.field_code.startswith("unknown_note_")
    ]
    unknown_codes = [
        item.field_code for item in rows if item.field_code.startswith("unknown_note_")
    ]
    if any(item not in order for item in fixed_codes):
        raise ValueError("draft contains a field outside the field table")
    if fixed_codes != sorted(fixed_codes, key=order.__getitem__):
        raise ValueError("fixed rows are not ordered by the field table")
    if unknown_codes != [f"unknown_note_{index:03d}" for index in range(1, len(unknown_codes) + 1)]:
        raise ValueError("unknown-note codes are not contiguous")
    ProjectDataListArtifact(schema_version="1.0", run_id=snapshot.run_id, rows=rows)


def _parsed_status(parsed: ParsedBatch):
    if parsed.accepted and parsed.rejections:
        return "accepted_with_rejections"
    if parsed.accepted:
        return "accepted"
    return "rejected"


def _fallback_outcome(tasks, error, errors, accepted=None):
    accepted_values = accepted or {}
    fallback = tuple(task.field_code for task in tasks if task.field_code not in accepted_values)
    return _BatchOutcome(
        accepted=accepted_values,
        fallback_codes=fallback,
        field_error_ids={code: error.error_id for code in fallback},
        errors=tuple(_merge_errors([], errors)),
    )


def _entry(path, kind, phase, source_ids):
    return ArtifactIndexEntry(
        path=path,
        kind=kind,
        producer_phase=phase,
        consumers=[ArtifactConsumer.WORKFLOW, ArtifactConsumer.DEBUG, ArtifactConsumer.REGRESSION],
        access_level=AccessLevel.RESTRICTED_DEBUG,
        source_material_ids=source_ids,
    )


def _write(context, artifact, entry, shared_error=None):
    try:
        path = context.artifact_writer.write_json(
            artifact,
            entry,
            target_failure_error_id=(shared_error.error_id if shared_error is not None else None),
        )
        return path, None, False
    except ServiceError as exc:
        if exc.error_id is None:
            raise
        error = context.diagnostics.resolve_error(exc.error_id)
        if exc.code == "artifact_index_update_failed":
            return entry.path, error, False
        return None, error, True


def _branch_sources(snapshot, branch):
    if branch == BranchCode.PDF_IMAGE:
        selected = set(snapshot.groups.pdf_inputs + snapshot.groups.image_inputs)
        return [
            material.material_id
            for material in snapshot.materials
            if material.material_id in selected
        ]
    if branch == BranchCode.XLSX:
        return list(snapshot.groups.spreadsheet_inputs)
    return list(snapshot.groups.gerber_jobs)


def _evidence_lookup(branch_results):
    by_id = {}
    keys = set()
    for result in branch_results:
        for seed in result.evidence:
            key = _seed_key(seed)
            if seed.evidence_id in by_id or key in keys:
                raise ValueError("duplicate EvidenceSeed id or key")
            by_id[seed.evidence_id] = seed
            keys.add(key)
    return by_id


def _seed_key(seed):
    region = (
        None
        if seed.region is None
        else (
            seed.region.x,
            seed.region.y,
            seed.region.width,
            seed.region.height,
            seed.region.unit,
        )
    )
    if seed.type == "pdf_page":
        return (seed.type, seed.source_material_id, seed.page_index, region)
    if seed.type == "image":
        return (
            seed.type,
            seed.source_material_id,
            seed.source_image_index,
            region,
        )
    return (
        seed.type,
        seed.source_material_id,
        seed.artifact_kind.value,
        seed.artifact_path,
    )


def _source_ids(tasks, bindings):
    return _stable_union(bindings[task.field_code].source_material_ids for task in tasks)


def _stable_union(groups):
    values = []
    seen = set()
    for group in groups:
        items = [group] if isinstance(group, str) else group
        for item in items:
            if item not in seen:
                seen.add(item)
                values.append(item)
    return values


def _merge_errors(existing, added):
    values = list(existing)
    known = {item.error_id for item in values}
    for item in added:
        if item is not None and item.error_id not in known:
            known.add(item.error_id)
            values.append(item)
    return values
