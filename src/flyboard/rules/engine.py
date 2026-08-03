from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from flyboard.errors import ServiceError
from flyboard.model_calls import (
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
    CallPurpose,
    ErrorRecord,
    FieldState,
    IntegrationResult,
    PhaseCode,
    ProjectDataListArtifact,
    ProjectDataRow,
    Rule,
    RuleCheckIdentity,
    RuleCheckResult,
    RuleCheckResultStatus,
    RuleDecision,
    RuleSetSnapshot,
    RuleTraceArtifacts,
    RuleTraceEntry,
    RunInputSnapshot,
    TrajectoryEvent,
    TrajectoryEventType,
)
from flyboard.services._support import utc_now

from .models import (
    RuleModelParsedArtifact,
    RuleModelRequestArtifact,
    RuleModelResponseArtifact,
    RuleModelUsageArtifact,
    RuleOutputProtocol,
)
from .parser import RuleResponseParser

if TYPE_CHECKING:
    from flyboard.workflow import AnalysisContext


SYSTEM_PROMPT = """你是 BoardFacts Agent 的规则检查 Agent。

输入包含一条规则、当前完整项目数据列表和完整字段表。你只能判断当前规则并更新其目标字段的检查语义，不能创建、删除、重排或修改其他字段。

必须遵守：
1. 先判断规则是否适用，再依据当前完整字段事实判断 violation、no_violation 或 insufficient_information。
2. 只有规则适用但信息不足、矛盾或无法可靠理解且存在潜在风险时，才使用 insufficient_information。
3. append_check_note 必须使用输入 Rule.name 的业务名称，以简短中文说明实际使用的字段事实或适用条件和本条判断；不能只写“已检查”“检查完成”等占位语句。
4. rule_id、target_field_code、字段编码、资料/证据/finding/error id、路由名、状态枚举、artifact 路径和输出协议字段只用于输入关联与响应校验，不得复制进 append_check_note 或 check_conclusion。
5. violation 和 insufficient_information 必须返回可直接展示的中文完整 check_conclusion，不能只返回 Confirmed、Conflict、Violation、已确认、冲突、违反等状态词；no_violation 必须返回 null。
6. 不输出推理过程、Markdown、代码围栏或协议外字段，只返回输出协议规定的 JSON。"""

PROGRAM_FIELD_CODES = {
    "project_analysis_findings",
    "pdf_presence",
    "spreadsheet_presence",
    "gerber_job_presence",
    "drill_file_presence",
}

OUTPUT_PROTOCOL = RuleOutputProtocol(
    decision="violation | no_violation | insufficient_information",
    append_check_note="non-empty string",
    check_conclusion="non-empty string for violation/insufficient_information; null otherwise",
)


@dataclass(slots=True)
class _CallControl:
    context: AnalysisContext
    request_path: str
    sequence: int
    rule_id: str

    def allow_attempt(self, attempt_number: Literal[1, 2]) -> bool:
        if attempt_number == 2:
            return not self.context.stop_signal.is_requested()
        self.context.stop_signal.check_boundary()
        self.context.trajectory_writer.append(
            TrajectoryEvent(
                schema_version="1.0",
                run_id=self.context.run_id,
                timestamp=utc_now(),
                phase=PhaseCode.RULE_CHECK,
                branch=None,
                event_type=TrajectoryEventType.MODEL_REQUEST_STARTED,
                summary={
                    "call_purpose": CallPurpose.RULE_CHECK.value,
                    "rule_id": self.rule_id,
                    "sequence": self.sequence,
                },
                source_material_ids=[],
                artifact_paths=[self.request_path],
                error_ids=[],
            )
        )
        return True


class RuleEngine:
    """Production v1 owner for selected and unselected rule-check paths."""

    def __init__(self, *, parser: RuleResponseParser | None = None) -> None:
        self._parser = parser or RuleResponseParser()

    def check(
        self,
        snapshot: RunInputSnapshot,
        integration_result: IntegrationResult,
        rule_set_snapshot: RuleSetSnapshot | None,
        context: AnalysisContext,
    ) -> RuleCheckResult:
        if snapshot.rule_set.mode == "not_selected":
            return self._commit_skipped(snapshot, integration_result, rule_set_snapshot, context)

        identity = RuleCheckIdentity(
            rule_set_id=snapshot.rule_set.rule_set_id,
            rule_set_version=snapshot.rule_set.rule_set_version,
            content_sha256=snapshot.rule_set.content_sha256,
        )
        rows = [item.model_copy(deep=True) for item in integration_result.draft_project_data_list]
        input_error = self._validate_draft(snapshot, rows, context)
        if input_error is not None:
            result = RuleCheckResult(
                schema_version="1.0",
                run_id=context.run_id,
                status=RuleCheckResultStatus.INTERRUPTED,
                reason="rule_check_failed",
                rule_set=identity,
                executed_rule_ids=[],
                attempted_rule_count=None,
                errors=[input_error],
            )
            return self._commit_result(result, context)

        snapshot_error = self._validate_rule_snapshot(snapshot, rule_set_snapshot, context)
        if snapshot_error is not None:
            final_error = self._commit_final_rows(rows, integration_result, context)
            errors = _merge_errors([snapshot_error], [final_error] if final_error else [])
            result = RuleCheckResult(
                schema_version="1.0",
                run_id=context.run_id,
                status=RuleCheckResultStatus.INTERRUPTED,
                reason="rule_check_failed",
                rule_set=identity,
                executed_rule_ids=[],
                attempted_rule_count=None,
                errors=errors,
            )
            return self._commit_result(result, context)

        if rule_set_snapshot is None:
            raise RuntimeError("validated selected rule snapshot is missing")

        executed: list[str] = []
        attempted = 0
        errors: list[ErrorRecord] = []
        trace_entries: list[RuleTraceEntry] = []
        trace_incomplete = False

        for sequence, rule in enumerate(rule_set_snapshot.rules, start=1):
            context.stop_signal.check_boundary()
            executed.append(rule.rule_id)
            matches = [
                index for index, row in enumerate(rows) if row.field_code == rule.target_field_code
            ]
            if not matches:
                trace = RuleTraceEntry(
                    schema_version="1.0",
                    run_id=context.run_id,
                    sequence=sequence,
                    rule_id=rule.rule_id,
                    target_field_code=rule.target_field_code,
                    model_called=False,
                    protocol_valid=None,
                    applied=False,
                    decision=None,
                    skip_reason="target_row_not_present",
                    error_id=None,
                    artifacts=RuleTraceArtifacts(request=None, response=None, parsed=None),
                )
                trace_error = self._append_trace(trace_entries, trace, context)
                if trace_error is not None:
                    errors = _merge_errors(errors, [trace_error])
                    trace_incomplete = True
                    break
                trace_entries.append(trace)
                continue

            row_index = matches[0]
            attempt, attempted_delta = self._run_rule(
                snapshot,
                rows,
                row_index,
                rule,
                sequence,
                context,
            )
            attempted += attempted_delta
            errors = _merge_errors(errors, [attempt.error] if attempt.error else [])
            trace_error = self._append_trace(trace_entries, attempt.trace, context)
            if trace_error is not None:
                errors = _merge_errors(errors, [trace_error])
                trace_incomplete = True
                break
            trace_entries.append(attempt.trace)
            if attempt.updated_row is not None:
                rows[row_index] = attempt.updated_row

        final_error = self._commit_final_rows(rows, integration_result, context)
        errors = _merge_errors(errors, [final_error] if final_error else [])
        final_exists = integration_result.project_data_list is not None
        if trace_incomplete:
            status = RuleCheckResultStatus.INTERRUPTED
            reason = "rule_trace_incomplete"
            attempted_count = None
        elif not final_exists:
            status = RuleCheckResultStatus.INTERRUPTED
            reason = "rule_check_failed"
            attempted_count = None
        else:
            status = (
                RuleCheckResultStatus.COMPLETED_WITH_ERRORS
                if errors
                else RuleCheckResultStatus.COMPLETED
            )
            reason = None
            attempted_count = attempted
        result = RuleCheckResult(
            schema_version="1.0",
            run_id=context.run_id,
            status=status,
            reason=reason,
            rule_set=identity,
            executed_rule_ids=executed,
            attempted_rule_count=attempted_count,
            errors=errors,
        )
        return self._commit_result(result, context)

    def _run_rule(self, snapshot, rows, row_index, rule, sequence, context):
        path_prefix = f"rules/model/rule_{sequence:04d}"
        policy = context.model_client.policy_for(CallPurpose.RULE_CHECK)
        request_artifact = RuleModelRequestArtifact(
            schema_version="1.0",
            run_id=context.run_id,
            model=policy.model_id,
            timeout_seconds=policy.timeout_seconds,
            sequence=sequence,
            rule=rule,
            system_prompt=SYSTEM_PROMPT,
            field_table=snapshot.field_table,
            project_data_list=rows,
            output_protocol=OUTPUT_PROTOCOL,
        )
        request_path, request_error = _write_json(
            context,
            request_artifact,
            _model_entry(f"{path_prefix}/request.json", "model_request"),
        )
        if request_path is None:
            trace = _trace(
                context.run_id,
                sequence,
                rule,
                model_called=False,
                protocol_valid=None,
                applied=False,
                decision=None,
                error=request_error,
                request=None,
                response=None,
                parsed=None,
            )
            return _RuleAttempt(trace, None, request_error), 0

        prompt = _prompt(rule, snapshot, rows)
        request = ModelCallRequest(
            call_purpose=CallPurpose.RULE_CHECK,
            messages=(
                ModelMessage(role="system", content=(TextContentPart(SYSTEM_PROMPT),)),
                ModelMessage(role="user", content=(TextContentPart(prompt),)),
            ),
        )
        context.stop_signal.check_boundary()
        attempted_delta = 1
        try:
            outcome = context.model_client.call(
                request,
                control=_CallControl(context, request_path, sequence, rule.rule_id),
            )
        except ModelCallNotStarted:
            attempted_delta = 0
            context.stop_signal.check_boundary()
            raise
        except Exception:
            outcome = None

        if not isinstance(outcome, (ModelCallSucceeded, ModelCallFailed)):
            error = _record_rule_error(
                context,
                "rule_model_call_failed",
                "规则模型调用未形成统一 outcome。",
                [request_path],
            )
            trace = _trace(
                context.run_id,
                sequence,
                rule,
                model_called=True,
                protocol_valid=None,
                applied=False,
                decision=None,
                error=error,
                request=request_path,
                response=None,
                parsed=None,
            )
            return _RuleAttempt(trace, None, error), attempted_delta

        if isinstance(outcome, ModelCallFailed):
            error = _record_rule_error(
                context,
                "rule_model_call_failed",
                "规则模型调用未形成可提交响应。",
                [request_path],
            )
            context.model_calls.record(
                outcome,
                phase=PhaseCode.RULE_CHECK,
                branch=None,
                error_id=error.error_id,
                artifact_paths=[request_path],
            )
            trace = _trace(
                context.run_id,
                sequence,
                rule,
                model_called=True,
                protocol_valid=None,
                applied=False,
                decision=None,
                error=error,
                request=request_path,
                response=None,
                parsed=None,
            )
            return _RuleAttempt(trace, None, error), attempted_delta

        response_artifact = RuleModelResponseArtifact(
            schema_version="1.0",
            run_id=context.run_id,
            model=outcome.response_model_id,
            sequence=sequence,
            rule_id=rule.rule_id,
            content=outcome.response_text,
            usage=(
                RuleModelUsageArtifact(
                    input_tokens=outcome.usage.input_tokens,
                    output_tokens=outcome.usage.output_tokens,
                )
                if outcome.usage is not None
                else None
            ),
        )
        response_path, response_error = _write_json(
            context,
            response_artifact,
            _model_entry(f"{path_prefix}/response.json", "model_response"),
        )
        if response_path is None:
            context.model_calls.record(
                outcome,
                phase=PhaseCode.RULE_CHECK,
                branch=None,
                error_id=response_error.error_id if response_error else None,
                artifact_paths=[request_path],
            )
            trace = _trace(
                context.run_id,
                sequence,
                rule,
                model_called=True,
                protocol_valid=None,
                applied=False,
                decision=None,
                error=response_error,
                request=request_path,
                response=None,
                parsed=None,
            )
            return _RuleAttempt(trace, None, response_error), attempted_delta

        parsed = self._parser.parse(outcome.response_text)
        protocol_error = None
        if parsed.instruction is None:
            protocol_error = _record_rule_error(
                context,
                "rule_model_protocol_invalid",
                "规则模型响应未通过唯一输出合同。",
                [request_path, response_path],
            )
        parsed_artifact = RuleModelParsedArtifact(
            schema_version="1.0",
            run_id=context.run_id,
            sequence=sequence,
            rule_id=rule.rule_id,
            status="accepted" if parsed.instruction is not None else "rejected",
            instruction=parsed.instruction,
            rejection_code=parsed.rejection_code,
            rejection_message=parsed.rejection_message,
            error_ids=[protocol_error.error_id] if protocol_error else [],
        )
        parsed_path, parsed_write_error = _write_json(
            context,
            parsed_artifact,
            _model_entry(f"{path_prefix}/parsed.json", "model_parsed"),
            protocol_error,
        )
        effective_error = protocol_error or parsed_write_error
        paths = [request_path, response_path]
        if parsed_path is not None:
            paths.append(parsed_path)
        context.model_calls.record(
            outcome,
            phase=PhaseCode.RULE_CHECK,
            branch=None,
            error_id=effective_error.error_id if effective_error else None,
            artifact_paths=paths,
        )
        if parsed.instruction is None or parsed_path is None:
            trace = _trace(
                context.run_id,
                sequence,
                rule,
                model_called=True,
                protocol_valid=False if parsed.instruction is None else None,
                applied=False,
                decision=None,
                error=effective_error,
                request=request_path,
                response=response_path,
                parsed=parsed_path,
            )
            return _RuleAttempt(trace, None, effective_error), attempted_delta

        try:
            updated = _updated_row(rows[row_index], parsed.instruction)
            candidate_rows = [item.model_copy(deep=True) for item in rows]
            candidate_rows[row_index] = updated
            ProjectDataListArtifact(
                schema_version="1.0", run_id=context.run_id, rows=candidate_rows
            )
        except (ValidationError, ValueError, TypeError):
            error = _record_rule_error(
                context,
                "rule_update_failed",
                "规则响应未能形成合法的目标行更新。",
                paths,
            )
            trace = _trace(
                context.run_id,
                sequence,
                rule,
                model_called=True,
                protocol_valid=True,
                applied=False,
                decision=parsed.instruction.decision,
                error=error,
                request=request_path,
                response=response_path,
                parsed=parsed_path,
            )
            return _RuleAttempt(trace, None, error), attempted_delta

        trace = _trace(
            context.run_id,
            sequence,
            rule,
            model_called=True,
            protocol_valid=True,
            applied=True,
            decision=parsed.instruction.decision,
            error=None,
            request=request_path,
            response=response_path,
            parsed=parsed_path,
        )
        return _RuleAttempt(trace, updated, None), attempted_delta

    @staticmethod
    def _validate_draft(snapshot, rows, context):
        definitions = {item.field_code: item for item in snapshot.field_table.fields}
        valid_codes = set(definitions)
        try:
            ProjectDataListArtifact(schema_version="1.0", run_id=context.run_id, rows=rows)
            actual_codes = {row.field_code for row in rows}
            required_codes = PROGRAM_FIELD_CODES | {
                item.field_code for item in snapshot.field_table.fields if item.is_core
            }
            if not required_codes.issubset(actual_codes):
                raise ValueError("draft is missing a required program or core row")
            if any(
                row.field_code not in valid_codes and not row.field_code.startswith("unknown_note_")
                for row in rows
            ):
                raise ValueError("draft contains a field outside the field table")
            fixed_codes = [
                row.field_code for row in rows if not row.field_code.startswith("unknown_note_")
            ]
            field_order = {
                item.field_code: index for index, item in enumerate(snapshot.field_table.fields)
            }
            if fixed_codes != sorted(fixed_codes, key=field_order.__getitem__):
                raise ValueError("draft fixed rows do not follow field-table order")
            unknown_codes = [
                row.field_code for row in rows if row.field_code.startswith("unknown_note_")
            ]
            if unknown_codes != [
                f"unknown_note_{index:03d}" for index in range(1, len(unknown_codes) + 1)
            ]:
                raise ValueError("draft unknown-note rows are not contiguous")
            if unknown_codes and rows[-len(unknown_codes) :][0].field_code != unknown_codes[0]:
                raise ValueError("draft unknown-note rows must follow fixed rows")
        except (ValidationError, ValueError, TypeError):
            return _record_rule_error(
                context,
                "rule_input_contract_invalid",
                "规则前项目数据列表违反正式结构或字段合同。",
                [],
            )
        return None

    @staticmethod
    def _validate_rule_snapshot(snapshot, rule_set_snapshot, context):
        selection = snapshot.rule_set
        field_codes = {item.field_code for item in snapshot.field_table.fields}
        valid = bool(
            rule_set_snapshot is not None
            and rule_set_snapshot.rule_set_id == selection.rule_set_id
            and rule_set_snapshot.rule_set_name == selection.rule_set_name
            and rule_set_snapshot.rule_set_version == selection.rule_set_version
            and rule_set_snapshot.schema_version == selection.schema_version
            and rule_set_snapshot.source_kind == selection.source_kind
            and rule_set_snapshot.source_path == selection.source_path
            and rule_set_snapshot.content_sha256 == selection.content_sha256
            and all(
                rule.target_field_code in field_codes
                and rule.target_field_code != "unknown_note_###"
                for rule in rule_set_snapshot.rules
            )
        )
        if valid:
            return None
        return _record_rule_error(
            context,
            "rule_snapshot_invalid",
            "运行规则快照身份、内容指纹或字段引用无效。",
            [],
        )

    @staticmethod
    def _commit_final_rows(rows, integration_result, context):
        artifact = ProjectDataListArtifact(schema_version="1.0", run_id=context.run_id, rows=rows)
        path, error = _write_json(context, artifact, _final_list_entry())
        if path is not None:
            integration_result.project_data_list = [item.model_copy(deep=True) for item in rows]
            return None
        return error

    @staticmethod
    def _commit_result(result, context):
        path, error = _write_json(context, result, _result_entry())
        if path is not None or error is None:
            return result
        errors = _merge_errors(list(result.errors), [error])
        return RuleCheckResult(
            schema_version="1.0",
            run_id=result.run_id,
            status=(
                result.status
                if result.status == RuleCheckResultStatus.INTERRUPTED
                else RuleCheckResultStatus.COMPLETED_WITH_ERRORS
            ),
            reason=result.reason,
            rule_set=result.rule_set,
            executed_rule_ids=result.executed_rule_ids,
            attempted_rule_count=result.attempted_rule_count,
            errors=errors,
        )

    @staticmethod
    def _append_trace(existing, trace, context):
        content = b"".join(
            (item.model_dump_json(exclude_none=False) + "\n").encode("utf-8")
            for item in [*existing, trace]
        )
        try:
            context.artifact_writer.write_content(
                content,
                "application/octet-stream",
                _trace_entry(),
            )
        except ServiceError as exc:
            if exc.code == "artifact_index_update_failed":
                return None
            if exc.error_id is not None:
                return context.diagnostics.resolve_error(exc.error_id)
            if context.stop_signal.is_requested():
                context.stop_signal.check_boundary()
            raise
        return None

    @staticmethod
    def _commit_skipped(snapshot, integration_result, rule_set_snapshot, context):
        if snapshot.rule_set.mode != "not_selected" or rule_set_snapshot is not None:
            raise ValueError("the skipped rule path requires no selected rule set")
        context.stop_signal.check_boundary()
        rows = [item.model_copy(deep=True) for item in integration_result.draft_project_data_list]
        final_path, _ = _write_json(
            context,
            ProjectDataListArtifact(schema_version="1.0", run_id=context.run_id, rows=rows),
            _final_list_entry(),
        )
        if final_path is None:
            raise ServiceError("artifact_write_failed")
        integration_result.project_data_list = rows
        result = RuleCheckResult(
            schema_version="1.0",
            run_id=context.run_id,
            status=RuleCheckResultStatus.SKIPPED,
            reason="rule_set_not_selected",
            rule_set=None,
            executed_rule_ids=[],
            attempted_rule_count=0,
            errors=[],
        )
        _write_json(context, result, _result_entry())
        return result


NoRuleRuleEngine = RuleEngine


@dataclass(frozen=True, slots=True)
class _RuleAttempt:
    trace: RuleTraceEntry
    updated_row: ProjectDataRow | None
    error: ErrorRecord | None


def _prompt(rule: Rule, snapshot: RunInputSnapshot, rows: list[ProjectDataRow]) -> str:
    return json.dumps(
        {
            "rule": rule.model_dump(mode="json"),
            "field_table": snapshot.field_table.model_dump(mode="json"),
            "project_data_list": [item.model_dump(mode="json") for item in rows],
            "output_protocol": OUTPUT_PROTOCOL.model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _updated_row(row: ProjectDataRow, instruction) -> ProjectDataRow:
    previous = row.check_note.strip()
    note = (
        instruction.append_check_note
        if not previous or previous.lower() == "null"
        else f"{row.check_note}\n{instruction.append_check_note}"
    )
    updates = {"check_note": note}
    if instruction.decision in {
        RuleDecision.VIOLATION,
        RuleDecision.INSUFFICIENT_INFORMATION,
    }:
        updates.update(
            field_state=FieldState.CONFLICT,
            check_conclusion=instruction.check_conclusion,
        )
    return row.model_copy(update=updates)


def _trace(
    run_id,
    sequence,
    rule,
    *,
    model_called,
    protocol_valid,
    applied,
    decision,
    error,
    request,
    response,
    parsed,
):
    return RuleTraceEntry(
        schema_version="1.0",
        run_id=run_id,
        sequence=sequence,
        rule_id=rule.rule_id,
        target_field_code=rule.target_field_code,
        model_called=model_called,
        protocol_valid=protocol_valid,
        applied=applied,
        decision=decision,
        skip_reason=None,
        error_id=error.error_id if error else None,
        artifacts=RuleTraceArtifacts(request=request, response=response, parsed=parsed),
    )


def _record_rule_error(context, code, message, artifact_paths):
    return context.diagnostics.record_error(
        code=code,
        severity="error",
        phase="rule_check",
        branch=None,
        message=message,
        source_material_ids=[],
        artifact_paths=artifact_paths,
        suggested_action="复核规则输入、模型响应和受管产物后创建新的运行。",
    )


def _write_json(context, artifact, entry, shared_error=None):
    try:
        path = context.artifact_writer.write_json(
            artifact,
            entry,
            target_failure_error_id=(shared_error.error_id if shared_error else None),
        )
        return path, None
    except ServiceError as exc:
        if exc.code == "artifact_index_update_failed":
            return entry.path, None
        if exc.error_id is not None:
            return None, context.diagnostics.resolve_error(exc.error_id)
        if context.stop_signal.is_requested():
            context.stop_signal.check_boundary()
        raise


def _model_entry(path, kind):
    return ArtifactIndexEntry(
        path=path,
        kind=kind,
        producer_phase=PhaseCode.RULE_CHECK,
        consumers=[ArtifactConsumer.WORKFLOW, ArtifactConsumer.DEBUG, ArtifactConsumer.REGRESSION],
        access_level=AccessLevel.RESTRICTED_DEBUG,
        source_material_ids=[],
    )


def _trace_entry():
    return ArtifactIndexEntry(
        path="rules/rule_trace.jsonl",
        kind="rule_trace",
        producer_phase=PhaseCode.RULE_CHECK,
        consumers=[ArtifactConsumer.WORKFLOW, ArtifactConsumer.DEBUG, ArtifactConsumer.REGRESSION],
        access_level=AccessLevel.PROJECT_SCOPED,
        source_material_ids=[],
    )


def _final_list_entry():
    return ArtifactIndexEntry(
        path="project_data_list.json",
        kind="project_data_list",
        producer_phase=PhaseCode.RULE_CHECK,
        consumers=[
            ArtifactConsumer.WORKFLOW,
            ArtifactConsumer.API,
            ArtifactConsumer.GUI,
            ArtifactConsumer.REPORT,
            ArtifactConsumer.REGRESSION,
        ],
        access_level=AccessLevel.PROJECT_SCOPED,
        source_material_ids=[],
    )


def _result_entry():
    return ArtifactIndexEntry(
        path="rules/rule_check_result.json",
        kind="rule_check_result",
        producer_phase=PhaseCode.RULE_CHECK,
        consumers=[ArtifactConsumer.WORKFLOW, ArtifactConsumer.REPORT, ArtifactConsumer.REGRESSION],
        access_level=AccessLevel.PROJECT_SCOPED,
        source_material_ids=[],
    )


def _merge_errors(existing, added):
    result = list(existing)
    seen = {item.error_id for item in result}
    for item in added:
        if item is not None and item.error_id not in seen:
            seen.add(item.error_id)
            result.append(item)
    return result
