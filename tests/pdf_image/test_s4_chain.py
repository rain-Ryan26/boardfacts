from __future__ import annotations

import io
import json
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone

import fitz
import pytest
from PIL import Image, ImageChops

from flyboard.errors import ServiceError
from flyboard.evidence import DeterministicEvidenceService
from flyboard.integration import SingleXlsxIntegrator
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
from flyboard.repositories import ArtifactRepository, ProjectRepository
from flyboard.rules import NoRuleRuleEngine
from flyboard.services import RuleSetService
from flyboard.workflow import RunService
from flyboard.workflow.evidence_ids import RunEvidenceIdHelper

NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)


def _make_workbook(path) -> None:
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


def _succeeded(request, response_text):
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
        provider_request_id="req_s4",
        finish_reason="stop",
        usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        response_text=response_text,
    )


class S4Model:
    def __init__(
        self,
        *,
        fail_pdf_calls=(),
        invalid_pdf_envelope_once=False,
        limit_multi_asset=False,
    ) -> None:
        self.calls = []
        self.pdf_call_count = 0
        self.fail_pdf_calls = set(fail_pdf_calls)
        self.invalid_pdf_envelope_once = invalid_pdf_envelope_once
        self.limit_multi_asset = limit_multi_asset

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
        if request.call_purpose == CallPurpose.PDF_IMAGE:
            self.pdf_call_count += 1
            if self.pdf_call_count in self.fail_pdf_calls:
                return ModelCallFailed(
                    call_purpose=request.call_purpose,
                    policy_version=POLICY_VERSION,
                    request_model_id=MODEL_ID,
                    response_model_id=None,
                    capacity_decision=CapacityDecision.UNKNOWN,
                    transport_attempt_count=1,
                    transport_retried=False,
                    transport_retry_reason=None,
                    duration_ms=5,
                    provider_request_id=None,
                    finish_reason=None,
                    usage=None,
                    failure_category=FailureCategory.CALL_FAILED,
                    provider_code=None,
                    failure_summary="synthetic PDF/image failure",
                )
            payload = json.loads(request.messages[1].content[0].text)
            if self.invalid_pdf_envelope_once and self.pdf_call_count == 1:
                return _succeeded(
                    request,
                    json.dumps({"schema_version": "1.0", "batch_id": "wrong", "assets": []}),
                )
            if self.limit_multi_asset and len(payload["assets"]) > 1:
                return ModelCallFailed(
                    call_purpose=request.call_purpose,
                    policy_version=POLICY_VERSION,
                    request_model_id=MODEL_ID,
                    response_model_id=None,
                    capacity_decision=CapacityDecision.UNKNOWN,
                    transport_attempt_count=0,
                    transport_retried=False,
                    transport_retry_reason=None,
                    duration_ms=0,
                    provider_request_id=None,
                    finish_reason=None,
                    usage=None,
                    failure_category=FailureCategory.INPUT_TOO_LARGE,
                    provider_code=None,
                    failure_summary="synthetic provider limit",
                )
            response = {
                "schema_version": "1.0",
                "batch_id": payload["batch_id"],
                "assets": [
                    {
                        "asset_id": asset["asset_id"],
                        "fields": [
                            {
                                "field_code": "board_thickness_mm",
                                "field_value": "1.60 mm",
                                "unit": "mm",
                                "branch_state": "clear_now",
                                "source_text": "成品板厚 1.60 mm",
                                "check_note": "图片中的板厚标注给出了明确数值，可作为成品板厚候选。",
                                "region": {"bbox_2d": [100, 100, 500, 350]},
                            }
                        ],
                        "unknown_notes": [],
                    }
                    for asset in payload["assets"]
                ],
            }
            return _succeeded(request, json.dumps(response, ensure_ascii=False))
        if request.call_purpose == CallPurpose.XLSX:
            response = (
                'SUBMIT_XLSX: {"fields":[{"field_code":"board_thickness_mm",'
                '"field_value":"1.60 mm","unit":"mm","branch_state":"clear_now",'
                '"source_material_id":"mat_xlsx","sheet_index":1,'
                '"source_cells":["A1","B1"],"check_note":"技术要求给出成品板厚。"}],'
                '"unknown_notes":[]}'
            )
            return _succeeded(request, response)

        payload = json.loads(request.messages[1].content[0].text)
        items = []
        for task in payload["field_tasks"]:
            candidates = task["ordered_candidates"]
            if task["operation"] == "diagnostic":
                value, relation, state = None, None, "missing"
            elif len(candidates) > 1:
                value = candidates[0]["field_value"]
                relation, state = "equivalent", "confirmed"
            else:
                value = candidates[0]["field_value"]
                states = {item["branch_state"] for item in candidates}
                if "conflict" in states:
                    relation, state = None, "conflict"
                elif "unknown" in states:
                    relation, state = None, "unknown"
                else:
                    relation, state = None, "confirmed"
            items.append(
                {
                    "field_code": task["field_code"],
                    "field_value": value,
                    "candidate_relation": relation,
                    "suggested_field_state": state,
                    "append_check_note": "已根据可读资料形成字段判断。",
                    "check_conclusion": "当前字段已形成可展示的整合结论。",
                }
            )
        return _succeeded(
            request,
            json.dumps(
                {"batch_id": payload["batch"]["batch_id"], "items": items},
                ensure_ascii=False,
            ),
        )


class HighlightWriteFailingArtifactRepository(ArtifactRepository):
    def replace_content_artifact(
        self,
        project_id,
        run_id,
        *,
        content,
        media_type,
        entry,
    ):
        if entry.kind == "evidence_highlight":
            raise ServiceError("artifact_write_failed")
        return super().replace_content_artifact(
            project_id,
            run_id,
            content=content,
            media_type=media_type,
            entry=entry,
        )


def _make_runtime(
    tmp_path,
    model,
    *,
    image_count=1,
    corrupt_images=(),
    include_pdf=False,
    pdf_pages=1,
    include_xlsx=False,
    evidence_service=None,
    artifact_type=ArtifactRepository,
):
    projects = ProjectRepository(tmp_path)
    materials = []
    for index in range(1, image_count + 1):
        material_id = f"mat_image_{index}"
        materials.append(
            ProjectMaterial(
                material_id=material_id,
                kind="image",
                source_ref=f"sources/{material_id}/input.png",
                display_name="input.png",
                registered_order=index,
                registered_at=NOW,
            )
        )
    if include_pdf:
        materials.append(
            ProjectMaterial(
                material_id="mat_pdf",
                kind="pdf",
                source_ref="sources/mat_pdf/input.pdf",
                display_name="input.pdf",
                registered_order=len(materials) + 1,
                registered_at=NOW,
            )
        )
    if include_xlsx:
        materials.append(
            ProjectMaterial(
                material_id="mat_xlsx",
                kind="spreadsheet",
                source_ref="sources/mat_xlsx/input.xlsx",
                display_name="input.xlsx",
                registered_order=len(materials) + 1,
                registered_at=NOW,
            )
        )
    with projects.mutate("prj_test") as mutation:
        mutation.create(
            ProjectRecord(
                project_id="prj_test",
                project_name="S4 测试",
                created_at=NOW,
                updated_at=NOW,
                materials=materials,
                latest_run_id=None,
            )
        )
    corrupt = set(corrupt_images)
    for index in range(1, image_count + 1):
        source = tmp_path / "projects" / "prj_test" / "sources" / f"mat_image_{index}" / "input.png"
        source.parent.mkdir(parents=True)
        if index in corrupt:
            source.write_bytes(b"not an image")
        else:
            Image.new("RGB", (100, 80), (245, 245, 245)).save(source, format="PNG")
    if include_pdf:
        source = tmp_path / "projects" / "prj_test" / "sources" / "mat_pdf" / "input.pdf"
        source.parent.mkdir(parents=True)
        document = fitz.open()
        for page_number in range(1, pdf_pages + 1):
            page = document.new_page(width=200, height=100)
            page.insert_text((20, 50), f"Thickness 1.60 mm page {page_number}")
        document.save(source)
        document.close()
    if include_xlsx:
        source = tmp_path / "projects" / "prj_test" / "sources" / "mat_xlsx" / "input.xlsx"
        source.parent.mkdir(parents=True)
        _make_workbook(source)

    artifacts = artifact_type(tmp_path)
    options = {}
    if evidence_service is not None:
        options = {
            "integrator": SingleXlsxIntegrator(),
            "rule_engine": NoRuleRuleEngine(),
            "evidence_service": evidence_service,
        }
    service = RunService(
        projects,
        artifacts,
        RuleSetService(tmp_path, clock=lambda: NOW),
        model_client=model,
        clock=lambda: NOW,
        id_factory=lambda prefix: f"{prefix}_1",
        recover_on_startup=False,
        **options,
    )
    service.create_run("prj_test", [item.material_id for item in materials], None)
    return service, artifacts


def _wait_terminal(service, timeout=5.0):
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
    raise AssertionError("S4 run did not reach a terminal state")


def _read_evidence(artifacts):
    summary = artifacts.read_run_summary("prj_test", "run_1")
    artifact_index = artifacts.read_artifact_index("prj_test", "run_1", summary.artifact_index_path)
    entries = [item for item in artifact_index.entries if item.kind == "evidence_index"]
    assert len(entries) == 1
    return artifact_index, artifacts.read_evidence_index("prj_test", "run_1", entries[0])


def test_image_and_xlsx_reach_one_product_with_controlled_evidence_content(tmp_path) -> None:
    model = S4Model()
    service, artifacts = _make_runtime(tmp_path, model, include_xlsx=True)

    summary = _wait_terminal(service)

    assert summary.status == RunStatus.COMPLETED
    assert Counter(item.call_purpose for item in model.calls) == {
        CallPurpose.PDF_IMAGE: 1,
        CallPurpose.XLSX: 1,
        CallPurpose.INTEGRATION: 2,
    }
    pdf_call = next(item for item in model.calls if item.call_purpose == CallPurpose.PDF_IMAGE)
    pdf_payload = json.loads(pdf_call.messages[1].content[0].text)
    region_schema = pdf_payload["output_schema"]["assets"][0]["fields"][0]["region"]
    assert list(region_schema) == ["bbox_2d"]
    assert all("0..999 相对坐标" in item for item in region_schema["bbox_2d"])
    analysis_instruction = pdf_payload["analysis_instruction"]
    assert "相对当前图片左上角" in analysis_instruction
    assert "缩放到 [0,999]" in analysis_instruction
    assert "不得返回原图像素坐标" in analysis_instruction
    assert "使用 null" in analysis_instruction
    thickness = next(
        item
        for item in service.get_data_list_view("prj_test", "run_1")
        if item.field_code == "board_thickness_mm"
    )
    assert thickness.field_state == "confirmed"
    assert thickness.evidence_ids == ["ev_image_001"]

    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    assert (run_root / "pdf/asset_manifest.json").is_file()
    assert (run_root / "pdf/model/batch_trace.jsonl").is_file()
    assert (run_root / "pdf/highlights/ev_image_001.png").is_file()
    branch = json.loads((run_root / "branches/pdf_image_result.json").read_text("utf-8"))
    assert branch["status"] == "completed"
    assert branch["evidence"][0]["source_image_index"] == 1

    request = json.loads(
        (run_root / "integration/model/batch_0001/request.json").read_text("utf-8")
    )
    task = next(item for item in request["tasks"] if item["field_code"] == "board_thickness_mm")
    assert task["ordered_candidates"][0]["evidence_summaries"] == [
        {
            "description": "第 1 份工程图片“input.png”第 1 张图的可复核证据",
            "note": "成品板厚 1.60 mm",
        }
    ]
    assert task["ordered_candidates"][1]["evidence_summaries"] == []

    artifact_index, evidence_index = _read_evidence(artifacts)
    evidence = evidence_index.evidence["ev_image_001"]
    assert evidence.asset_path == "pdf/highlights/ev_image_001.png"
    metadata = service.evidence_service.get("prj_test", "run_1", evidence_index, "ev_image_001")
    assert "asset_path" not in metadata.model_dump(mode="json")
    content, media_type = service.evidence_service.get_content(
        "prj_test",
        "run_1",
        artifact_index,
        evidence_index,
        "ev_image_001",
    )
    assert media_type == "image/png"
    highlighted = Image.open(io.BytesIO(content)).convert("RGB")
    assert highlighted.size == (100, 80)
    source = Image.open(run_root / branch["evidence"][0]["source_asset_path"]).convert("RGB")
    assert ImageChops.difference(source, highlighted).getbbox() == (10, 8, 50, 28)
    service.shutdown(0)


def test_highlight_failure_keeps_region_and_falls_back_to_source_asset(tmp_path) -> None:
    def fail_highlight(content, region):
        raise RuntimeError("synthetic highlighter failure")

    artifacts = ArtifactRepository(tmp_path)
    evidence_service = DeterministicEvidenceService(artifacts, highlighter=fail_highlight)
    model = S4Model()
    service, runtime_artifacts = _make_runtime(
        tmp_path,
        model,
        evidence_service=evidence_service,
    )

    summary = _wait_terminal(service)

    assert summary.status == RunStatus.COMPLETED
    assert [item.code for item in summary.error_summary] == ["evidence_highlight_failed"]
    _, evidence_index = _read_evidence(runtime_artifacts)
    evidence = evidence_index.evidence["ev_image_001"]
    assert evidence.region is not None
    assert evidence.asset_path.startswith("pdf/assets/")
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    assert not (run_root / "pdf/highlights/ev_image_001.png").exists()
    service.shutdown(0)


def test_highlight_target_write_failure_records_warning_and_falls_back(tmp_path) -> None:
    service, artifacts = _make_runtime(
        tmp_path,
        S4Model(),
        artifact_type=HighlightWriteFailingArtifactRepository,
    )

    summary = _wait_terminal(service)

    assert summary.status == RunStatus.COMPLETED
    assert [(item.code, item.severity.value) for item in summary.error_summary] == [
        ("evidence_highlight_failed", "warning")
    ]
    artifact_index, evidence_index = _read_evidence(artifacts)
    evidence = evidence_index.evidence["ev_image_001"]
    assert evidence.region is not None
    assert evidence.asset_path.startswith("pdf/assets/")
    assert not any(item.kind == "evidence_highlight" for item in artifact_index.entries)
    assert not (
        tmp_path / "projects" / "prj_test" / "runs" / "run_1" / "pdf/highlights/ev_image_001.png"
    ).exists()
    service.shutdown(0)


def test_evidence_read_failures_use_stable_codes_without_runtime_mutation(tmp_path) -> None:
    service, artifacts = _make_runtime(tmp_path, S4Model())
    assert _wait_terminal(service).status == RunStatus.COMPLETED
    artifact_index, evidence_index = _read_evidence(artifacts)
    asset_path = evidence_index.evidence["ev_image_001"].asset_path
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    observed_paths = [
        run_root / "run_summary.json",
        run_root / "artifact_index.json",
        run_root / "trajectory.jsonl",
    ]
    before = {path: path.read_bytes() for path in observed_paths}

    with pytest.raises(ServiceError) as raised:
        service.evidence_service.get("prj_test", "run_other", evidence_index, "ev_image_001")
    assert raised.value.code == "evidence_index_unavailable"

    with pytest.raises(ServiceError) as raised:
        service.evidence_service.get("prj_test", "run_1", evidence_index, "ev_image_999")
    assert raised.value.code == "evidence_not_found"

    missing_asset_index = artifact_index.model_copy(
        update={"entries": [item for item in artifact_index.entries if item.path != asset_path]}
    )
    with pytest.raises(ServiceError) as raised:
        service.evidence_service.get_content(
            "prj_test",
            "run_1",
            missing_asset_index,
            evidence_index,
            "ev_image_001",
        )
    assert raised.value.code == "artifact_content_unavailable"

    assert {path: path.read_bytes() for path in observed_paths} == before
    service.shutdown(0)


def test_one_bad_image_keeps_later_product_and_records_manifest_failure(tmp_path) -> None:
    service, _ = _make_runtime(tmp_path, S4Model(), image_count=2, corrupt_images={2})

    summary = _wait_terminal(service)

    assert summary.status == RunStatus.COMPLETED_WITH_ERRORS
    assert "image_decode_failed" in {item.code for item in summary.error_summary}
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    branch = json.loads((run_root / "branches/pdf_image_result.json").read_text("utf-8"))
    assert branch["status"] == "completed_with_warnings"
    assert branch["source_material_ids"] == ["mat_image_1", "mat_image_2"]
    assert len(branch["fields"]) == len(branch["evidence"]) == 1
    manifest = json.loads((run_root / "pdf/asset_manifest.json").read_text("utf-8"))
    assert [item["outcome"] for item in manifest["records"]] == ["prepared", "failed"]
    assert service.get_data_list_view("prj_test", "run_1")
    service.shutdown(0)


def test_one_model_batch_failure_does_not_discard_later_image_result(tmp_path) -> None:
    service, _ = _make_runtime(tmp_path, S4Model(fail_pdf_calls={1}), image_count=2)

    summary = _wait_terminal(service)

    assert summary.status == RunStatus.COMPLETED_WITH_ERRORS
    assert "pdf_image_model_call_failed" in {item.code for item in summary.error_summary}
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    branch = json.loads((run_root / "branches/pdf_image_result.json").read_text("utf-8"))
    assert branch["status"] == "completed_with_warnings"
    assert branch["fields"][0]["source_material_id"] == "mat_image_2"
    trace = [
        json.loads(line)
        for line in (run_root / "pdf/model/batch_trace.jsonl").read_text("utf-8").splitlines()
    ]
    assert "batch_failed" in [item["event_type"] for item in trace]
    assert "batch_completed" in [item["event_type"] for item in trace]
    service.shutdown(0)


def test_pdf_page_preparation_keeps_png_text_and_pdf_evidence_identity(tmp_path) -> None:
    service, artifacts = _make_runtime(
        tmp_path,
        S4Model(),
        image_count=0,
        include_pdf=True,
    )

    summary = _wait_terminal(service)

    assert summary.status == RunStatus.COMPLETED
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    manifest = json.loads((run_root / "pdf/asset_manifest.json").read_text("utf-8"))
    record = manifest["records"][0]
    assert record["source_kind"] == "pdf"
    assert record["source_page_number"] == 1
    assert record["outcome"] == "prepared"
    assert record["mime_type"] == "image/png"
    assert record["text_status"] == "extracted"
    page_text = json.loads((run_root / record["text_artifact_path"]).read_text("utf-8"))
    assert page_text["text"] == "Thickness 1.60 mm page 1"

    branch = json.loads((run_root / "branches/pdf_image_result.json").read_text("utf-8"))
    assert branch["evidence"][0]["evidence_id"] == "ev_pdf_001"
    assert branch["evidence"][0]["type"] == "pdf_page"
    assert branch["evidence"][0]["page_index"] == 1
    request = json.loads(
        next((run_root / "pdf/model").glob("pdf_*/attempt_1_request.json")).read_text("utf-8")
    )
    assert request["assets"][0]["page_text"]["status"] == "included"
    assert request["assets"][0]["page_text"]["text"] == "Thickness 1.60 mm page 1"

    _, evidence_index = _read_evidence(artifacts)
    evidence = evidence_index.evidence["ev_pdf_001"]
    assert evidence.type == "pdf_page"
    assert evidence.page_index == 1
    assert evidence.asset_path == "pdf/highlights/ev_pdf_001.png"
    service.shutdown(0)


def test_invalid_envelope_retries_once_and_keeps_protocol_diagnostic(tmp_path) -> None:
    model = S4Model(invalid_pdf_envelope_once=True)
    service, _ = _make_runtime(tmp_path, model)

    summary = _wait_terminal(service)

    assert summary.status == RunStatus.COMPLETED_WITH_ERRORS
    assert model.pdf_call_count == 2
    assert "pdf_image_model_protocol_invalid" in {item.code for item in summary.error_summary}
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    batch_root = run_root / "pdf/model/image_0001_0001"
    assert (batch_root / "attempt_1_parsed.json").is_file()
    assert (batch_root / "attempt_2_request.json").is_file()
    assert (batch_root / "attempt_2_response.json").is_file()
    assert (batch_root / "attempt_2_parsed.json").is_file()
    branch = json.loads((run_root / "branches/pdf_image_result.json").read_text("utf-8"))
    assert branch["status"] == "completed_with_warnings"
    assert len(branch["fields"]) == 1
    service.shutdown(0)


def test_provider_limit_splits_pdf_by_continuous_page_ranges(tmp_path) -> None:
    model = S4Model(limit_multi_asset=True)
    service, artifacts = _make_runtime(
        tmp_path,
        model,
        image_count=0,
        include_pdf=True,
        pdf_pages=2,
    )

    summary = _wait_terminal(service)

    assert summary.status == RunStatus.COMPLETED
    assert model.pdf_call_count == 3
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    trace = [
        json.loads(line)
        for line in (run_root / "pdf/model/batch_trace.jsonl").read_text("utf-8").splitlines()
    ]
    event_types = [item["event_type"] for item in trace]
    assert event_types.count("provider_limit_rejected") == 1
    assert event_types.count("batch_split") == 1
    assert event_types.count("batch_completed") == 2
    split = next(item for item in trace if item["event_type"] == "batch_split")
    assert split["child_batch_ids"] == ["pdf_0001_0001_a", "pdf_0001_0001_b"]

    branch = json.loads((run_root / "branches/pdf_image_result.json").read_text("utf-8"))
    assert [item["page_index"] for item in branch["evidence"]] == [1, 2]
    _, evidence_index = _read_evidence(artifacts)
    assert list(evidence_index.evidence) == ["ev_pdf_001", "ev_pdf_002"]
    service.shutdown(0)


def test_run_evidence_id_helper_reuses_complete_keys_and_separates_types() -> None:
    helper = RunEvidenceIdHelper()
    first = helper.assign(
        evidence_type="pdf_page",
        source_material_id="mat_pdf",
        file_path="sources/mat_pdf/input.pdf",
        page_index=1,
        region=None,
        source_asset_path="pdf/assets/page_1.png",
    )
    same = helper.assign(
        evidence_type="pdf_page",
        source_material_id="mat_pdf",
        file_path="sources/mat_pdf/input.pdf",
        page_index=1,
        region=None,
        source_asset_path="pdf/assets/page_1.png",
    )
    image = helper.assign(
        evidence_type="image",
        source_material_id="mat_image",
        file_path="sources/mat_image/input.png",
        page_index=None,
        region=None,
        source_asset_path="pdf/assets/image_1.png",
        source_image_index=1,
    )

    assert first == same == "ev_pdf_001"
    assert image == "ev_image_001"
