from __future__ import annotations

import json
import time
import zipfile
from dataclasses import replace
from datetime import datetime, timezone

from flyboard.model_calls import (
    ENABLE_THINKING,
    MODEL_ID,
    POLICY_VERSION,
    TEMPERATURE,
    CapacityDecision,
    FailureCategory,
    ModelCallFailed,
    ModelCallPolicy,
    ModelCallSucceeded,
    ModelUsage,
)
from flyboard.models import CallPurpose, ProjectMaterial, ProjectRecord, RunStatus
from flyboard.models.field_table import build_current_field_table
from flyboard.repositories import ArtifactRepository, ProjectRepository
from flyboard.services import RuleSetService
from flyboard.workflow import RunService

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def make_workbook(path) -> None:
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <workbookPr date1904="0"/>
  <sheets><sheet name="技术要求" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    sheet = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:B1"/>
  <sheetData><row r="1">
    <c r="A1" t="inlineStr"><is><t>成品板厚</t></is></c>
    <c r="B1" t="inlineStr"><is><t>1.60 mm</t></is></c>
  </row></sheetData>
</worksheet>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def integration_item(task, note):
    candidates = task["ordered_candidates"]
    if task["operation"] == "diagnostic":
        value, relation, state = None, None, "missing"
    else:
        value = candidates[0]["field_value"]
        states = {candidate["branch_state"] for candidate in candidates}
        relation = "different" if task["operation"] == "compare" and len(candidates) > 1 else None
        if relation == "different" or "conflict" in states:
            state = "conflict"
        elif "unknown" in states:
            state = "unknown"
        else:
            state = "confirmed"
    return {
        "field_code": task["field_code"],
        "field_value": value,
        "candidate_relation": relation,
        "suggested_field_state": state,
        "append_check_note": note,
        "check_conclusion": "当前字段已形成整合结论。",
    }


class SuccessfulS3Model:
    def __init__(self) -> None:
        self.calls = []

    def policy_for(self, purpose):
        return ModelCallPolicy(
            policy_version=POLICY_VERSION,
            call_purpose=purpose,
            model_id=MODEL_ID,
            temperature=TEMPERATURE,
            enable_thinking=ENABLE_THINKING,
            timeout_seconds=10.0,
            max_output_tokens=2048,
        )

    def call(self, request, *, control=None):
        self.calls.append(request)
        if control is not None:
            assert control.allow_attempt(1)
        if request.call_purpose == CallPurpose.XLSX:
            response = (
                'SUBMIT_XLSX: {"fields":[{"field_code":"board_thickness_mm",'
                '"field_value":"1.60 mm","unit":"mm","branch_state":"clear_now",'
                '"source_material_id":"mat_1","sheet_index":1,'
                '"source_cells":["A1","B1"],"check_note":"技术要求给出成品板厚。"}],'
                '"unknown_notes":[]}'
            )
        else:
            payload = json.loads(request.messages[1].content[0].text)
            response = json.dumps(
                {
                    "batch_id": payload["batch"]["batch_id"],
                    "items": [
                        integration_item(task, "当前任务已形成整合结果。")
                        for task in payload["field_tasks"]
                    ],
                },
                ensure_ascii=False,
            )
        return ModelCallSucceeded(
            call_purpose=request.call_purpose,
            policy_version=POLICY_VERSION,
            request_model_id=MODEL_ID,
            response_model_id=MODEL_ID,
            capacity_decision=CapacityDecision.UNKNOWN,
            transport_attempt_count=1,
            transport_retried=False,
            transport_retry_reason=None,
            duration_ms=5,
            provider_request_id="req_test",
            finish_reason="stop",
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            response_text=response,
        )


class MismatchedXlsxResponseModel(SuccessfulS3Model):
    def call(self, request, *, control=None):
        outcome = super().call(request, control=control)
        if request.call_purpose == CallPurpose.XLSX:
            return replace(outcome, response_model_id="unexpected-model")
        return outcome


class OpaqueNoteXlsxModel(SuccessfulS3Model):
    def call(self, request, *, control=None):
        outcome = super().call(request, control=control)
        if request.call_purpose == CallPurpose.XLSX:
            return replace(
                outcome,
                response_text=outcome.response_text.replace(
                    "技术要求给出成品板厚。",
                    "技术表对应 mat_1，当前状态为 clear_now。",
                ),
            )
        return outcome


class IntegrationItemRetryModel(SuccessfulS3Model):
    def __init__(self) -> None:
        super().__init__()
        self.integration_call_count = 0

    def call(self, request, *, control=None):
        if request.call_purpose == CallPurpose.XLSX:
            return super().call(request, control=control)
        self.calls.append(request)
        self.integration_call_count += 1
        if control is not None:
            assert control.allow_attempt(1)
        payload = json.loads(request.messages[1].content[0].text)
        if self.integration_call_count == 1:
            response = json.dumps(
                {"batch_id": payload["batch"]["batch_id"], "items": []},
                ensure_ascii=False,
            )
        else:
            response = json.dumps(
                {
                    "batch_id": payload["batch"]["batch_id"],
                    "items": [
                        integration_item(task, "单字段重试或后续批次形成合法结果。")
                        for task in payload["field_tasks"]
                    ],
                },
                ensure_ascii=False,
            )
        return ModelCallSucceeded(
            call_purpose=request.call_purpose,
            policy_version=POLICY_VERSION,
            request_model_id=MODEL_ID,
            response_model_id=MODEL_ID,
            capacity_decision=CapacityDecision.UNKNOWN,
            transport_attempt_count=1,
            transport_retried=False,
            transport_retry_reason=None,
            duration_ms=5,
            provider_request_id="req_retry",
            finish_reason="stop",
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            response_text=response,
        )


class SelectedRuleModel(SuccessfulS3Model):
    def call(self, request, *, control=None):
        if request.call_purpose != CallPurpose.RULE_CHECK:
            return super().call(request, control=control)
        self.calls.append(request)
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
            duration_ms=5,
            provider_request_id="req_rule",
            finish_reason="stop",
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            response_text=json.dumps(
                {
                    "decision": "violation",
                    "append_check_note": "成品板厚规则确认当前板厚不符合要求。",
                    "check_conclusion": "成品板厚违反所选规则。",
                },
                ensure_ascii=False,
            ),
        )


class IntegrationFailureModel(SuccessfulS3Model):
    def call(self, request, *, control=None):
        if request.call_purpose == CallPurpose.XLSX:
            return super().call(request, control=control)
        self.calls.append(request)
        if control is not None:
            assert control.allow_attempt(1)
        return ModelCallFailed(
            call_purpose=request.call_purpose,
            policy_version=POLICY_VERSION,
            request_model_id=MODEL_ID,
            response_model_id=None,
            capacity_decision=CapacityDecision.UNKNOWN,
            transport_attempt_count=1,
            transport_retried=False,
            transport_retry_reason=None,
            duration_ms=7,
            provider_request_id=None,
            finish_reason=None,
            usage=None,
            failure_category=FailureCategory.CALL_FAILED,
            provider_code=None,
            failure_summary="synthetic integration failure",
        )


def make_runtime(tmp_path, model, *, rule_set_id=None):
    projects = ProjectRepository(tmp_path)
    material = ProjectMaterial(
        material_id="mat_1",
        kind="spreadsheet",
        source_ref="sources/mat_1/input.xlsx",
        display_name="input.xlsx",
        registered_order=1,
        registered_at=NOW,
    )
    with projects.mutate("prj_test") as mutation:
        mutation.create(
            ProjectRecord(
                project_id="prj_test",
                project_name="S3 测试",
                created_at=NOW,
                updated_at=NOW,
                materials=[material],
                latest_run_id=None,
            )
        )
    source = tmp_path / "projects" / "prj_test" / "sources" / "mat_1" / "input.xlsx"
    source.parent.mkdir(parents=True)
    make_workbook(source)
    rule_sets = RuleSetService(tmp_path, clock=lambda: NOW)
    if rule_set_id is not None:
        rule_source = tmp_path / "selected_rules.json"
        rule_source.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "rule_set_id": rule_set_id,
                    "rule_set_name": "板厚规则",
                    "rule_set_version": "2026.07",
                    "description": "S6 选中规则链回归",
                    "rules": [
                        {
                            "rule_id": "rule_thickness",
                            "name": "成品板厚规则",
                            "target_field_code": "board_thickness_mm",
                            "instruction": "成品板厚 1.60 mm 不符合本测试规则。",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        assert (
            rule_sets.register_file(str(rule_source), build_current_field_table()).validation_status
            == "valid"
        )
    service = RunService(
        projects,
        ArtifactRepository(tmp_path),
        rule_sets,
        model_client=model,
        clock=lambda: NOW,
        id_factory=lambda prefix: f"{prefix}_1",
        recover_on_startup=False,
    )
    service.create_run("prj_test", ["mat_1"], rule_set_id)
    return service


def wait_terminal(service, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = service.get_summary("prj_test", "run_1")
        if summary.status in {
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_ERRORS,
            RunStatus.FAILED,
        }:
            return summary
        time.sleep(0.01)
    raise AssertionError("S3 run did not reach a terminal state")


def test_single_xlsx_reaches_formal_data_list_and_empty_evidence_index(tmp_path) -> None:
    model = SuccessfulS3Model()
    service = make_runtime(tmp_path, model)

    summary = wait_terminal(service)

    assert summary.status == RunStatus.COMPLETED
    assert summary.rule_check_summary.status == "skipped"
    assert [item.call_purpose for item in summary.model_call_summaries] == [
        CallPurpose.XLSX,
        CallPurpose.INTEGRATION,
    ]
    integration_summary = summary.model_call_summaries[1]
    assert integration_summary.logical_call_count == sum(
        request.call_purpose == CallPurpose.INTEGRATION for request in model.calls
    )
    rows = service.get_data_list_view("prj_test", "run_1")
    thickness = next(item for item in rows if item.field_code == "board_thickness_mm")
    assert thickness.field_value == "1.60 mm"
    assert thickness.field_state == "confirmed"
    assert thickness.source == "XLSX_1_sheet1"
    assert thickness.evidence_ids == []

    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    branch = json.loads((run_root / "branches/xlsx_result.json").read_text(encoding="utf-8"))
    assert branch["evidence"] == []
    request = json.loads(
        (run_root / "integration/model/batch_0001/request.json").read_text(encoding="utf-8")
    )
    task = request["tasks"][0]
    assert set(task) == {
        "field_code",
        "operation",
        "decision_rule",
        "fixed_primary_candidate",
        "ordered_candidates",
        "diagnostics",
    }
    assert task["fixed_primary_candidate"] == 0
    assert task["diagnostics"] == {"ordered_inputs": [], "tool_findings": [], "errors": []}
    candidate = task["ordered_candidates"][0]
    assert set(candidate) == {
        "candidate_index",
        "field_value",
        "unit",
        "branch_state",
        "source_label",
        "check_note",
        "evidence_summaries",
    }
    assert candidate["candidate_index"] == 0
    assert candidate["unit"] == "mm"
    assert candidate["evidence_summaries"] == []
    assert json.loads((run_root / "evidence_index.json").read_text(encoding="utf-8")) == {
        "schema_version": "1.0",
        "run_id": "run_1",
        "evidence": {},
        "locators": {},
    }
    assert (
        json.loads((run_root / "rules/rule_check_result.json").read_text(encoding="utf-8"))[
            "reason"
        ]
        == "rule_set_not_selected"
    )
    service.shutdown(0)


def test_selected_rule_set_uses_production_engine_and_commits_trace(tmp_path) -> None:
    service = make_runtime(tmp_path, SelectedRuleModel(), rule_set_id="factory_rules")

    summary = wait_terminal(service)

    assert summary.status == RunStatus.COMPLETED
    assert summary.rule_check_summary.status == "completed"
    assert summary.rule_check_summary.rule_set_id == "factory_rules"
    assert summary.rule_check_summary.attempted_rule_count == 1
    assert [item.call_purpose for item in summary.model_call_summaries] == [
        CallPurpose.XLSX,
        CallPurpose.INTEGRATION,
        CallPurpose.RULE_CHECK,
    ]
    row = next(
        item
        for item in service.get_data_list_view("prj_test", "run_1")
        if item.field_code == "board_thickness_mm"
    )
    assert row.field_state == "conflict"
    assert row.check_note.endswith("成品板厚规则确认当前板厚不符合要求。")
    assert row.check_conclusion == "成品板厚违反所选规则。"
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    trace = json.loads((run_root / "rules/rule_trace.jsonl").read_text(encoding="utf-8"))
    assert trace["rule_id"] == "rule_thickness"
    assert trace["model_called"] is True
    assert trace["protocol_valid"] is True
    assert trace["applied"] is True
    service.shutdown(0)


def test_xlsx_response_model_mismatch_fails_before_response_artifact(tmp_path) -> None:
    service = make_runtime(tmp_path, MismatchedXlsxResponseModel())
    summary = wait_terminal(service)

    assert summary.status == RunStatus.FAILED
    assert "xlsx_model_call_failed" in {item.code for item in summary.error_summary}
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    assert (run_root / "xlsx/model/request.json").is_file()
    assert not (run_root / "xlsx/model/response.json").exists()
    assert not (run_root / "xlsx/model/parsed.json").exists()
    service.shutdown(0)


def test_xlsx_non_empty_note_is_not_rejected_by_text_matching(tmp_path) -> None:
    model = OpaqueNoteXlsxModel()
    service = make_runtime(tmp_path, model)
    summary = wait_terminal(service)

    assert summary.status == RunStatus.COMPLETED
    assert model.calls[0].call_purpose == CallPurpose.XLSX
    assert model.calls[1:]
    assert all(item.call_purpose == CallPurpose.INTEGRATION for item in model.calls[1:])
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    parsed = json.loads((run_root / "xlsx/model/parsed.json").read_text(encoding="utf-8"))
    assert parsed["status"] == "accepted"
    assert parsed["accepted_fields"][0]["check_note"] == (
        "技术表对应 mat_1，当前状态为 clear_now。"
    )
    thickness = next(
        item
        for item in service.get_data_list_view("prj_test", "run_1")
        if item.field_code == "board_thickness_mm"
    )
    assert thickness.field_state == "confirmed"
    assert thickness.check_note.endswith("当前任务已形成整合结果。")
    service.shutdown(0)


def test_locatable_missing_item_retries_once_and_keeps_protocol_error(tmp_path) -> None:
    model = IntegrationItemRetryModel()
    service = make_runtime(tmp_path, model)
    summary = wait_terminal(service)

    assert summary.status == RunStatus.COMPLETED_WITH_ERRORS
    assert model.integration_call_count >= 2
    assert [item.code for item in summary.error_summary] == ["integration_model_protocol_invalid"]
    integration_summary = next(
        item
        for item in summary.model_call_summaries
        if item.call_purpose == CallPurpose.INTEGRATION
    )
    assert integration_summary.logical_call_count == model.integration_call_count
    thickness = next(
        item
        for item in service.get_data_list_view("prj_test", "run_1")
        if item.field_code == "board_thickness_mm"
    )
    assert thickness.field_state == "confirmed"
    assert thickness.error_ids == [summary.error_summary[0].error_id]
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    retry_root = run_root / "integration/model/batch_0001/retry/board_thickness_mm"
    assert (retry_root / "request.json").is_file()
    assert (retry_root / "response.json").is_file()
    assert (retry_root / "parsed.json").is_file()
    service.shutdown(0)


def test_integration_call_failure_uses_visible_fallback_and_keeps_product(tmp_path) -> None:
    service = make_runtime(tmp_path, IntegrationFailureModel())
    summary = wait_terminal(service)

    assert summary.status == RunStatus.COMPLETED_WITH_ERRORS
    assert summary.error_summary
    assert {item.code for item in summary.error_summary} == {"integration_model_call_failed"}
    assert len({item.error_id for item in summary.error_summary}) == len(summary.error_summary)
    rows = service.get_data_list_view("prj_test", "run_1")
    thickness = next(item for item in rows if item.field_code == "board_thickness_mm")
    assert thickness.field_value == "1.60 mm"
    assert thickness.field_state == "unknown"
    assert thickness.check_note == "技术要求给出成品板厚。"
    assert thickness.check_conclusion == "整合模型未形成合法判断，需要工程师结合运行问题复核。"
    assert len(thickness.error_ids) == 1
    assert thickness.error_ids[0] in {item.error_id for item in summary.error_summary}
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    assert (run_root / "integration/model/batch_0001/request.json").is_file()
    assert not (run_root / "integration/model/batch_0001/response.json").exists()
    assert (run_root / "project_data_list.json").is_file()
    assert (run_root / "evidence_index.json").is_file()
    service.shutdown(0)
