from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from flyboard.integration import SingleXlsxIntegrator
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
    BranchCode,
    BranchField,
    BranchFieldState,
    BranchResult,
    BranchResultStatus,
    ErrorRecord,
    FieldCategory,
    MaterialKind,
    MemberKind,
    NoRuleSet,
    PhaseCode,
    RunInputGroups,
    RunInputMaterial,
    RunInputSnapshot,
    ToolFinding,
    ToolFindingKind,
)
from flyboard.models.field_table import build_current_field_table

NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)
SHA = "a" * 64


class StopSignal:
    def is_requested(self):
        return False

    def check_boundary(self):
        return None


class Diagnostics:
    def __init__(self, errors=()):
        self.errors = {item.error_id: item for item in errors}

    def record_error(self, **values):
        error = ErrorRecord(
            error_id=f"err_00000000-0000-4000-8000-{len(self.errors) + 1:012d}",
            **values,
        )
        self.errors[error.error_id] = error
        return error

    def resolve_error(self, error_id):
        return self.errors[error_id]


class Writer:
    def __init__(self):
        self.artifacts = {}

    def write_json(self, artifact, entry, *, target_failure_error_id=None):
        canonical = type(artifact).model_validate(artifact.model_dump(mode="json"))
        self.artifacts[entry.path] = canonical
        return entry.path


class Model:
    def __init__(self):
        self.requests = []

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
        self.requests.append(request)
        if control is not None:
            assert control.allow_attempt(1)
        payload = json.loads(request.messages[1].content[0].text)
        items = []
        for task in payload["field_tasks"]:
            if task["operation"] == "diagnostic":
                value, relation, state = None, None, "missing"
            elif len(task["ordered_candidates"]) > 1:
                value = task["ordered_candidates"][0]["field_value"]
                relation, state = "different", "conflict"
            else:
                value = task["ordered_candidates"][0]["field_value"]
                states = {item["branch_state"] for item in task["ordered_candidates"]}
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
        return ModelCallSucceeded(
            call_purpose=request.call_purpose,
            policy_version=POLICY_VERSION,
            request_model_id=MODEL_ID,
            response_model_id=MODEL_ID,
            capacity_decision=CapacityDecision.UNKNOWN,
            transport_attempt_count=1,
            transport_retried=False,
            transport_retry_reason=None,
            duration_ms=2,
            provider_request_id="req_integration",
            finish_reason="stop",
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            response_text=json.dumps(
                {"batch_id": payload["batch"]["batch_id"], "items": items},
                ensure_ascii=False,
            ),
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


def material(material_id, order, kind, *, member_kinds=None):
    options = {"member_kinds": member_kinds} if kind == MaterialKind.GERBER_JOB else {}
    return RunInputMaterial(
        material_id=material_id,
        material_order=order,
        kind=kind,
        source_ref=(
            f"sources/{material_id}"
            if kind == MaterialKind.GERBER_JOB
            else f"sources/{material_id}/input.bin"
        ),
        sha256=SHA,
        registered_at=NOW,
        **options,
    )


def snapshot(materials):
    return RunInputSnapshot(
        schema_version="1.0",
        project_id="prj_test",
        run_id="run_test",
        snapshot_id="snap_test",
        materials=materials,
        groups=RunInputGroups(
            pdf_inputs=[item.material_id for item in materials if item.kind == MaterialKind.PDF],
            image_inputs=[],
            spreadsheet_inputs=[
                item.material_id for item in materials if item.kind == MaterialKind.SPREADSHEET
            ],
            gerber_jobs=[
                item.material_id for item in materials if item.kind == MaterialKind.GERBER_JOB
            ],
            unsupported_materials=[],
        ),
        field_table=build_current_field_table(),
        rule_set=NoRuleSet(mode="not_selected", reason="rule_set_not_selected"),
    )


def field(
    table,
    code,
    value,
    material_id,
    source_ref,
    *,
    state=BranchFieldState.CLEAR_NOW,
    finding_ids=None,
    error_ids=None,
):
    definition = next(item for item in table.fields if item.field_code == code)
    return BranchField(
        field_code=code,
        field_name=definition.field_name,
        field_value=value,
        unit=definition.unit,
        category=definition.category,
        branch_state=state,
        source=source_ref,
        source_material_id=material_id,
        source_ref=source_ref,
        evidence_ids=[],
        tool_finding_ids=finding_ids or [],
        error_ids=error_ids or [],
        check_note=f"{source_ref} 原始判断。",
    )


def unknown(value, material_id, source_ref):
    return BranchField(
        field_code="unknown_note_candidate",
        field_name="未知说明候选",
        field_value=value,
        unit=None,
        category=FieldCategory.BOARD,
        branch_state=BranchFieldState.UNKNOWN,
        source=source_ref,
        source_material_id=material_id,
        source_ref=source_ref,
        evidence_ids=[],
        tool_finding_ids=[],
        error_ids=[],
        check_note=value,
    )


def branch(
    run_snapshot, code, status, source_ids, *, fields=None, notes=None, findings=None, errors=None
):
    return BranchResult(
        schema_version="1.0",
        run_id=run_snapshot.run_id,
        branch=code,
        status=status,
        source_material_ids=source_ids,
        fields=fields or [],
        unknown_notes=notes or [],
        tool_findings=findings or [],
        evidence=[],
        errors=errors or [],
    )


def context(errors=()):
    diagnostics = Diagnostics(errors)
    model = Model()
    writer = Writer()
    return (
        SimpleNamespace(
            run_id="run_test",
            stop_signal=StopSignal(),
            artifact_writer=writer,
            diagnostics=diagnostics,
            model_client=model,
            model_calls=Recorder(),
            trajectory_writer=Trajectory(),
        ),
        model,
        writer,
    )


def test_multi_source_uses_material_order_and_keeps_conflict_diagnostic_and_unknowns():
    materials = [
        material("mat_xlsx", 1, MaterialKind.SPREADSHEET),
        material("mat_pdf", 2, MaterialKind.PDF),
        material(
            "mat_gerber",
            3,
            MaterialKind.GERBER_JOB,
            member_kinds=[MemberKind.GERBER, MemberKind.DRILL],
        ),
    ]
    run_snapshot = snapshot(materials)
    error = ErrorRecord(
        error_id="err_00000000-0000-4000-8000-000000000001",
        code="gerber_measurement_unavailable",
        severity="warning",
        phase=PhaseCode.BRANCH_ANALYSIS,
        branch=BranchCode.GERBER,
        message="最小孔径无法可靠测量。",
        source_material_ids=["mat_gerber"],
        artifact_paths=[],
        suggested_action="复核钻孔资料。",
    )
    finding = ToolFinding(
        finding_id="finding_00000000-0000-4000-8000-000000000001",
        producer="GerberScout",
        code="measurement_missing",
        kind=ToolFindingKind.COVERAGE_GAP,
        message="最小孔径缺失。",
        source_material_ids=["mat_gerber"],
        evidence_ids=[],
        error_id=error.error_id,
    )
    pdf = branch(
        run_snapshot,
        BranchCode.PDF_IMAGE,
        BranchResultStatus.COMPLETED,
        ["mat_pdf"],
        fields=[
            field(
                run_snapshot.field_table,
                "outer_copper_thickness",
                "70 µm",
                "mat_pdf",
                "PDF_1_page1",
            )
        ],
        notes=[unknown("PDF 未知说明", "mat_pdf", "PDF_1_page1")],
    )
    xlsx = branch(
        run_snapshot,
        BranchCode.XLSX,
        BranchResultStatus.COMPLETED,
        ["mat_xlsx"],
        fields=[
            field(
                run_snapshot.field_table,
                "outer_copper_thickness",
                "35 µm",
                "mat_xlsx",
                "XLSX_1_sheet1",
            )
        ],
        notes=[unknown("XLSX 未知说明", "mat_xlsx", "XLSX_1_sheet1")],
    )
    gerber = branch(
        run_snapshot,
        BranchCode.GERBER,
        BranchResultStatus.COMPLETED_WITH_WARNINGS,
        ["mat_gerber"],
        fields=[
            field(
                run_snapshot.field_table,
                "min_hole_diameter_mm",
                None,
                "mat_gerber",
                "GERBER_1",
                state=BranchFieldState.MISSING,
                finding_ids=[finding.finding_id],
                error_ids=[error.error_id],
            ),
            field(
                run_snapshot.field_table,
                "min_annular_ring_mm",
                "0.35",
                "mat_gerber",
                "GERBER_1",
                state=BranchFieldState.UNKNOWN,
            ),
        ],
        findings=[finding],
        errors=[error],
    )
    ctx, model, writer = context([error])

    result = SingleXlsxIntegrator().merge(run_snapshot, [pdf, xlsx, gerber], [finding], ctx)

    assert not result.errors
    assert len(model.requests) == 3
    compare_tasks = [
        task
        for path, artifact in writer.artifacts.items()
        if path.startswith("integration/model/batch_")
        and path.endswith("/request.json")
        and artifact.operation == "compare"
        for task in artifact.tasks
    ]
    compare = next(item for item in compare_tasks if item.field_code == "outer_copper_thickness")
    assert [item.field_value for item in compare.ordered_candidates] == ["35 µm", "70 µm"]
    annular = next(item for item in compare_tasks if item.field_code == "min_annular_ring_mm")
    assert "必须保留固定主候选表达" in annular.decision_rule
    assert "不得返回 null 或 missing" in annular.decision_rule
    assert "不能把有值候选改成缺值诊断" in annular.decision_rule
    diagnostic = writer.artifacts["integration/model/batch_0003/request.json"].tasks[0]
    assert diagnostic.operation == "diagnostic"
    assert diagnostic.diagnostics.tool_findings[0].message == finding.message
    copper_thickness = next(
        item
        for item in result.draft_project_data_list
        if item.field_code == "outer_copper_thickness"
    )
    assert copper_thickness.field_state == "conflict"
    assert copper_thickness.source == "XLSX_1_sheet1"
    assert [item.field_value for item in copper_thickness.conflict_candidates] == [
        "35 µm",
        "70 µm",
    ]
    assert [item.source_material_id for item in copper_thickness.conflict_candidates] == [
        "mat_xlsx",
        "mat_pdf",
    ]
    hole = next(
        item for item in result.draft_project_data_list if item.field_code == "min_hole_diameter_mm"
    )
    assert hole.field_state == "missing"
    assert hole.tool_finding_ids == [finding.finding_id]
    assert hole.error_ids == [error.error_id]
    unknowns = [
        item
        for item in result.draft_project_data_list
        if item.field_code.startswith("unknown_note_")
    ]
    assert [item.field_value for item in unknowns] == ["XLSX 未知说明", "PDF 未知说明"]


def test_failed_branch_remains_visible_while_other_source_is_delivered():
    materials = [
        material("mat_pdf", 1, MaterialKind.PDF),
        material("mat_xlsx", 2, MaterialKind.SPREADSHEET),
    ]
    run_snapshot = snapshot(materials)
    error = ErrorRecord(
        error_id="err_00000000-0000-4000-8000-000000000001",
        code="pdf_failed",
        severity="error",
        phase=PhaseCode.BRANCH_ANALYSIS,
        branch=BranchCode.PDF_IMAGE,
        message="PDF 分支失败。",
        source_material_ids=["mat_pdf"],
        artifact_paths=[],
        suggested_action="检查 PDF。",
    )
    pdf = branch(
        run_snapshot, BranchCode.PDF_IMAGE, BranchResultStatus.FAILED, ["mat_pdf"], errors=[error]
    )
    xlsx = branch(
        run_snapshot,
        BranchCode.XLSX,
        BranchResultStatus.COMPLETED,
        ["mat_xlsx"],
        fields=[
            field(
                run_snapshot.field_table,
                "board_thickness_mm",
                "1.60 mm",
                "mat_xlsx",
                "XLSX_1_sheet1",
            )
        ],
    )
    ctx, _, _ = context([error])

    result = SingleXlsxIntegrator().merge(run_snapshot, [pdf, xlsx], [], ctx)

    thickness = next(
        item for item in result.draft_project_data_list if item.field_code == "board_thickness_mm"
    )
    assert thickness.field_state == "confirmed"
    findings = next(
        item
        for item in result.draft_project_data_list
        if item.field_code == "project_analysis_findings"
    )
    assert findings.field_state == "conflict"
    assert findings.error_ids == [error.error_id]
    pdf_presence = next(
        item for item in result.draft_project_data_list if item.field_code == "pdf_presence"
    )
    assert pdf_presence.field_value == "存在，分析失败"
    assert pdf_presence.error_ids == [error.error_id]


def test_invalid_field_metadata_fails_before_model_call():
    materials = [material("mat_xlsx", 1, MaterialKind.SPREADSHEET)]
    run_snapshot = snapshot(materials)
    invalid = field(
        run_snapshot.field_table,
        "board_thickness_mm",
        "1.60 mm",
        "mat_xlsx",
        "XLSX_1_sheet1",
    ).model_copy(update={"field_name": "错误字段名"})
    xlsx = branch(
        run_snapshot,
        BranchCode.XLSX,
        BranchResultStatus.COMPLETED,
        ["mat_xlsx"],
        fields=[invalid],
    )
    ctx, model, writer = context()

    result = SingleXlsxIntegrator().merge(run_snapshot, [xlsx], [], ctx)

    assert result.draft_project_data_list == []
    assert [item.code for item in result.errors] == ["integration_input_contract_invalid"]
    assert model.requests == []
    assert writer.artifacts == {}


def test_duplicate_cross_branch_finding_identity_fails_without_invalid_result_projection():
    materials = [
        material("mat_pdf", 1, MaterialKind.PDF),
        material("mat_xlsx", 2, MaterialKind.SPREADSHEET),
    ]
    run_snapshot = snapshot(materials)
    finding_id = "finding_00000000-0000-4000-8000-000000000001"
    pdf_finding = ToolFinding(
        finding_id=finding_id,
        producer="pdf",
        code="warning",
        kind=ToolFindingKind.COVERAGE_GAP,
        message="PDF warning",
        source_material_ids=["mat_pdf"],
        evidence_ids=[],
        error_id=None,
    )
    xlsx_finding = pdf_finding.model_copy(
        update={"producer": "xlsx", "message": "XLSX warning", "source_material_ids": ["mat_xlsx"]}
    )
    pdf = branch(
        run_snapshot,
        BranchCode.PDF_IMAGE,
        BranchResultStatus.COMPLETED_WITH_WARNINGS,
        ["mat_pdf"],
        fields=[
            field(
                run_snapshot.field_table,
                "board_thickness_mm",
                "1.70 mm",
                "mat_pdf",
                "PDF_1_page1",
                finding_ids=[finding_id],
            )
        ],
        findings=[pdf_finding],
    )
    xlsx = branch(
        run_snapshot,
        BranchCode.XLSX,
        BranchResultStatus.COMPLETED_WITH_WARNINGS,
        ["mat_xlsx"],
        fields=[
            field(
                run_snapshot.field_table,
                "board_thickness_mm",
                "1.60 mm",
                "mat_xlsx",
                "XLSX_1_sheet1",
                finding_ids=[finding_id],
            )
        ],
        findings=[xlsx_finding],
    )
    ctx, model, _ = context()

    result = SingleXlsxIntegrator().merge(
        run_snapshot, [pdf, xlsx], [pdf_finding, xlsx_finding], ctx
    )

    assert [item.code for item in result.errors] == ["integration_input_contract_invalid"]
    assert result.tool_findings == []
    assert model.requests == []
