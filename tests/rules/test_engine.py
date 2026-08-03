from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from flyboard.errors import ServiceError
from flyboard.model_calls import (
    ENABLE_THINKING,
    MODEL_ID,
    POLICY_VERSION,
    TEMPERATURE,
    CapacityDecision,
    ModelCallPolicy,
    ModelCallSucceeded,
    ModelUsage,
)
from flyboard.models import (
    ErrorRecord,
    IntegrationResult,
    ProjectDataRow,
    Rule,
    RuleSetSnapshot,
    RunInputGroups,
    RunInputSnapshot,
    SelectedRuleSet,
)
from flyboard.models.field_table import build_current_field_table
from flyboard.rules import RuleEngine

NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)
SHA = "a" * 64


class StopSignal:
    def is_requested(self):
        return False

    def check_boundary(self):
        return None


class Diagnostics:
    def __init__(self):
        self.errors = {}

    def record_error(self, **values):
        error = ErrorRecord(
            error_id=f"err_00000000-0000-4000-8000-{len(self.errors) + 1:012d}",
            **values,
        )
        self.errors[error.error_id] = error
        return error

    def resolve_error(self, error_id):
        return self.errors[error_id]


class ArtifactWriter:
    def __init__(self, diagnostics, *, fail_trace=False):
        self.diagnostics = diagnostics
        self.fail_trace = fail_trace
        self.json = {}
        self.content = {}

    def write_json(self, artifact, entry, *, target_failure_error_id=None):
        self.json[entry.path] = artifact
        return entry.path

    def write_content(self, content, media_type, entry, *, optional_failure_code=None):
        if self.fail_trace and entry.path == "rules/rule_trace.jsonl":
            error = self.diagnostics.record_error(
                code="artifact_write_failed",
                severity="error",
                phase="rule_check",
                branch=None,
                message="synthetic trace failure",
                source_material_ids=[],
                artifact_paths=[],
                suggested_action="retry",
            )
            raise ServiceError("artifact_write_failed", error_id=error.error_id)
        self.content[entry.path] = content
        return entry.path


class Model:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def policy_for(self, purpose):
        return ModelCallPolicy(
            policy_version=POLICY_VERSION,
            call_purpose=purpose,
            model_id=MODEL_ID,
            temperature=TEMPERATURE,
            enable_thinking=ENABLE_THINKING,
            timeout_seconds=10.0,
            max_output_tokens=1024,
        )

    def call(self, request, *, control=None):
        self.requests.append(request)
        if control is not None:
            assert control.allow_attempt(1)
        return ModelCallSucceeded(
            call_purpose=request.call_purpose,
            policy_version=POLICY_VERSION,
            request_model_id=MODEL_ID,
            response_model_id=MODEL_ID,
            capacity_decision=CapacityDecision.UNKNOWN,
            transport_attempt_count=1,
            transport_retried=False,
            transport_retry_reason=None,
            duration_ms=3,
            provider_request_id="req_rule",
            finish_reason="stop",
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            response_text=self.responses.pop(0),
        )


class Recorder:
    def __init__(self):
        self.calls = []

    def record(self, outcome, **values):
        self.calls.append((outcome, values))


class Trajectory:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


def make_case(rules, responses, *, target="board_thickness_mm", fail_trace=False):
    field_table = build_current_field_table()
    selection = SelectedRuleSet(
        mode="selected",
        rule_set_id="factory_rules",
        rule_set_name="测试规则库",
        rule_set_version="2026.07",
        schema_version="1.0",
        source_kind="system_rule_set_file",
        source_path="rule_sets/factory_rules.json",
        content_sha256=SHA,
        snapshot_artifact="rules/rule_set_snapshot.json",
    )
    snapshot = RunInputSnapshot(
        schema_version="1.0",
        project_id="prj_test",
        run_id="run_test",
        snapshot_id="snap_test",
        materials=[],
        groups=RunInputGroups(
            pdf_inputs=[],
            image_inputs=[],
            spreadsheet_inputs=[],
            gerber_jobs=[],
            unsupported_materials=[],
        ),
        field_table=field_table,
        rule_set=selection,
    )
    snapshot_rules = [
        Rule(
            rule_id=rule_id,
            name=("成品板厚上限检查" if index == 1 else "成品板厚复核"),
            target_field_code=rule_target,
            instruction="检查目标字段是否符合当前工程要求。",
        )
        for index, (rule_id, rule_target) in enumerate(rules, start=1)
    ]
    rule_snapshot = RuleSetSnapshot(
        rule_set_id=selection.rule_set_id,
        rule_set_name=selection.rule_set_name,
        rule_set_version=selection.rule_set_version,
        schema_version="1.0",
        source_kind=selection.source_kind,
        source_path=selection.source_path,
        content_sha256=SHA,
        snapshot_created_at=NOW,
        rules=snapshot_rules,
    )
    program_codes = {
        "project_analysis_findings",
        "pdf_presence",
        "spreadsheet_presence",
        "gerber_job_presence",
        "drill_file_presence",
    }
    required = (
        program_codes | {item.field_code for item in field_table.fields if item.is_core} | {target}
    )
    rows = []
    for definition in (item for item in field_table.fields if item.field_code in required):
        is_target = definition.field_code == target
        rows.append(
            ProjectDataRow(
                index=len(rows) + 1,
                field_code=definition.field_code,
                field_name=definition.field_name,
                field_value="1.60 mm" if is_target else "已确认",
                source="XLSX_1_sheet1" if is_target else "integration",
                evidence_ids=[],
                tool_finding_ids=[],
                error_ids=[],
                conflict_candidates=[],
                category=definition.category,
                field_state="confirmed",
                check_note="整合确认。",
                check_conclusion=("成品板厚已确认。" if is_target else "程序行已确认。"),
            )
        )
    integration = IntegrationResult(
        draft_project_data_list=rows,
        project_data_list=None,
        tool_findings=[],
        errors=[],
        evidence_index=None,
    )
    diagnostics = Diagnostics()
    writer = ArtifactWriter(diagnostics, fail_trace=fail_trace)
    model = Model(responses)
    recorder = Recorder()
    context = SimpleNamespace(
        run_id="run_test",
        stop_signal=StopSignal(),
        artifact_writer=writer,
        diagnostics=diagnostics,
        model_client=model,
        model_calls=recorder,
        trajectory_writer=Trajectory(),
    )
    return snapshot, rule_snapshot, integration, context, model, writer, recorder


def response(decision, note, conclusion):
    return json.dumps(
        {
            "decision": decision,
            "append_check_note": note,
            "check_conclusion": conclusion,
        },
        ensure_ascii=False,
    )


def test_selected_rules_apply_in_snapshot_order_and_keep_complete_conclusion():
    case = make_case(
        [("rule_1", "board_thickness_mm"), ("rule_2", "board_thickness_mm")],
        [
            response(
                "violation",
                "成品板厚上限检查发现当前板厚超过规则上限。",
                "当前板厚超过规则上限，需要工程师复核。",
            ),
            response(
                "no_violation",
                "成品板厚复核未发现新增问题。",
                None,
            ),
        ],
    )
    snapshot, rule_snapshot, integration, context, model, writer, recorder = case

    result = RuleEngine().check(snapshot, integration, rule_snapshot, context)

    assert result.status == "completed"
    assert result.executed_rule_ids == ["rule_1", "rule_2"]
    assert result.attempted_rule_count == 2
    row = next(
        item for item in integration.project_data_list if item.field_code == "board_thickness_mm"
    )
    assert row.field_state == "conflict"
    assert row.check_note == (
        "整合确认。\n成品板厚上限检查发现当前板厚超过规则上限。\n成品板厚复核未发现新增问题。"
    )
    assert row.check_conclusion == "当前板厚超过规则上限，需要工程师复核。"
    second_request = writer.json["rules/model/rule_0002/request.json"]
    second_row = next(
        item for item in second_request.project_data_list if item.field_code == "board_thickness_mm"
    )
    assert second_row.check_note.endswith("成品板厚上限检查发现当前板厚超过规则上限。")
    system_prompt = model.requests[0].messages[0].content[0].text
    assert "使用输入 Rule.name 的业务名称" in system_prompt
    assert "不得复制进 append_check_note 或 check_conclusion" in system_prompt
    trace = [json.loads(line) for line in writer.content["rules/rule_trace.jsonl"].splitlines()]
    assert [item["sequence"] for item in trace] == [1, 2]
    assert all(item["applied"] for item in trace)
    assert len(model.requests) == len(recorder.calls) == 2


def test_missing_target_row_writes_skip_trace_without_model_call_or_error():
    case = make_case(
        [("rule_missing", "panelization_summary")],
        [],
    )
    snapshot, rule_snapshot, integration, context, model, writer, _ = case

    result = RuleEngine().check(snapshot, integration, rule_snapshot, context)

    assert result.status == "completed"
    assert result.executed_rule_ids == ["rule_missing"]
    assert result.attempted_rule_count == 0
    assert result.errors == []
    assert model.requests == []
    trace = json.loads(writer.content["rules/rule_trace.jsonl"].decode("utf-8"))
    assert trace["skip_reason"] == "target_row_not_present"
    assert trace["model_called"] is False


def test_invalid_protocol_keeps_row_and_continues_with_visible_error():
    invalid = json.dumps(
        {
            "decision": "violation",
            "append_check_note": "不应提交。",
            "check_conclusion": "不应提交。",
            "extra": True,
        },
        ensure_ascii=False,
    )
    case = make_case([("rule_1", "board_thickness_mm")], [invalid])
    snapshot, rule_snapshot, integration, context, _, writer, recorder = case

    result = RuleEngine().check(snapshot, integration, rule_snapshot, context)

    assert result.status == "completed_with_errors"
    assert result.attempted_rule_count == 1
    assert [item.code for item in result.errors] == ["rule_model_protocol_invalid"]
    row = next(
        item for item in integration.project_data_list if item.field_code == "board_thickness_mm"
    )
    assert row.check_note == "整合确认。"
    trace = json.loads(writer.content["rules/rule_trace.jsonl"].decode("utf-8"))
    assert trace["protocol_valid"] is False
    assert trace["applied"] is False
    assert len(recorder.calls) == 1


@pytest.mark.parametrize(
    ("note", "conclusion"),
    [
        (
            "成品板厚上限检查发现 rule_1 对应的板厚风险。",
            "当前板厚超过规则上限，需要工程师复核。",
        ),
        (
            "成品板厚上限检查发现 board_thickness_mm 超过规则上限。",
            "当前板厚超过规则上限，需要工程师复核。",
        ),
        (
            "成品板厚上限检查记录 error_id=err_001。",
            "当前板厚超过规则上限，需要工程师复核。",
        ),
        (
            "成品板厚上限检查通过 append_check_note 记录了风险。",
            "当前板厚超过规则上限，需要工程师复核。",
        ),
        (
            "当前板厚超过规则上限，需要工程师复核。",
            "当前板厚超过规则上限，需要工程师复核。",
        ),
        (
            "成品板厚上限检查发现当前板厚超过规则上限。",
            "当前板厚超过规则上限，关联 err_001，需要工程师复核。",
        ),
    ],
)
def test_parser_does_not_scan_non_empty_note_or_conclusion_text(note, conclusion):
    case = make_case(
        [("rule_1", "board_thickness_mm")],
        [response("violation", note, conclusion)],
    )
    snapshot, rule_snapshot, integration, context, _, writer, _ = case

    result = RuleEngine().check(snapshot, integration, rule_snapshot, context)

    assert result.status == "completed"
    assert result.errors == []
    row = next(
        item for item in integration.project_data_list if item.field_code == "board_thickness_mm"
    )
    assert row.field_state == "conflict"
    assert row.check_note.endswith(note)
    assert row.check_conclusion == conclusion
    trace = json.loads(writer.content["rules/rule_trace.jsonl"].decode("utf-8"))
    assert trace["protocol_valid"] is True
    assert trace["applied"] is True


@pytest.mark.parametrize(
    ("note", "conclusion"),
    [
        ("成品板厚上限检查。", "处理完成。"),
        ("成品板厚上限检查已检查。", "冲突"),
    ],
)
def test_prompt_quality_is_not_rejected_by_natural_language_matching(note, conclusion):
    case = make_case(
        [("rule_1", "board_thickness_mm")],
        [response("violation", note, conclusion)],
    )
    snapshot, rule_snapshot, integration, context, _, _, _ = case

    result = RuleEngine().check(snapshot, integration, rule_snapshot, context)

    assert result.status == "completed"
    assert result.errors == []
    row = next(
        item for item in integration.project_data_list if item.field_code == "board_thickness_mm"
    )
    assert row.field_state == "conflict"
    assert row.check_note.endswith(note)
    assert row.check_conclusion == conclusion


def test_trace_failure_does_not_apply_current_rule_and_interrupts_audit():
    case = make_case(
        [("rule_1", "board_thickness_mm")],
        [
            response(
                "violation",
                "成品板厚上限检查发现当前板厚超过规则上限。",
                "当前板厚超过规则上限，需要工程师复核。",
            )
        ],
        fail_trace=True,
    )
    snapshot, rule_snapshot, integration, context, _, _, _ = case

    result = RuleEngine().check(snapshot, integration, rule_snapshot, context)

    assert result.status == "interrupted"
    assert result.reason == "rule_trace_incomplete"
    assert result.attempted_rule_count is None
    assert [item.code for item in result.errors] == ["artifact_write_failed"]
    row = next(
        item for item in integration.project_data_list if item.field_code == "board_thickness_mm"
    )
    assert row.check_note == "整合确认。"
    assert row.check_conclusion == "成品板厚已确认。"
