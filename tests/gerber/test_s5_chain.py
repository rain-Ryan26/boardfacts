from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import flyboard.gerber.engine as engine_module
from flyboard.gerber import GerberResult
from flyboard.gerber.models import OUTPUT_KEYS, GerberDiagnosticSubmission
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
from flyboard.models import (
    CallPurpose,
    LayerDefinitionItem,
    ProjectMaterial,
    ProjectRecord,
    RunStatus,
)
from flyboard.repositories import ArtifactRepository, ProjectRepository
from flyboard.services import RuleSetService, ServiceError
from flyboard.workflow import RunService

NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)
GERBER = """G04 BoardFacts S5 fixture*
%FSLAX24Y24*%
%MOMM*%
%ADD10C,0.200*%
%ADD11C,1.000*%
D10*
X000000Y000000D02*
X100000Y000000D01*
D11*
X050000Y050000D03*
M02*
"""
REGION_GERBER = """G04 BoardFacts standard region fixture*
%FSLAX24Y24*%
%MOMM*%
%ADD10C,0.200*%
%ADD11C,1.000*%
D10*
X000000Y000000D02*
X100000Y000000D01*
D11*
X050000Y050000D03*
G36*
X200000Y200000D02*
X300000Y200000D01*
X300000Y300000D01*
X200000Y300000D01*
X200000Y200000D01*
G37*
M02*
"""
DRILL = """M48
METRIC
T01C0.600
T02C0.300
T03C0.400
%
T01
X5.0Y5.0
T03
X1.0Y1.0G85X3.0Y1.0
M30
"""
INCH_REGION_DRILL = """M48
METRIC
T01C15.240
%
T01
X127.0Y127.0
M30
"""
DARK_CLEAR_GERBER = """G04 Ordered dark-clear bounds fixture*
%FSLAX24Y24*%
%MOMM*%
%ADD10R,10.000X10.000*%
%ADD11R,8.000X12.000*%
D10*
X000000Y000000D03*
%LPC*%
D11*
X040000Y000000D03*
M02*
"""
BOTTOM_WITHOUT_PAD = """G04 Copper layer without a pad at the drill*
%FSLAX24Y24*%
%MOMM*%
%ADD10C,0.200*%
D10*
X000000Y000000D02*
X100000Y000000D01*
M02*
"""


class DiagnosticSuccessModel:
    def __init__(
        self,
        *,
        response_model_id=MODEL_ID,
        note="相关层角色尚未明确，使当前字段的工具测量覆盖不完整，需要工程师复核层定义。",
    ) -> None:
        self.response_model_id = response_model_id
        self.note = note
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
        if request.call_purpose == CallPurpose.GERBER_DIAGNOSTIC:
            return ModelCallSucceeded(
                call_purpose=request.call_purpose,
                policy_version=POLICY_VERSION,
                request_model_id=MODEL_ID,
                response_model_id=self.response_model_id,
                capacity_decision=CapacityDecision.UNKNOWN,
                transport_attempt_count=1,
                transport_retried=False,
                transport_retry_reason=None,
                duration_ms=5,
                provider_request_id="req_gerber_diagnostic",
                finish_reason="stop",
                usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                response_text=json.dumps(
                    {"append_check_note": self.note},
                    ensure_ascii=False,
                ),
            )
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
            failure_summary="non-diagnostic calls are not part of this fixture",
        )


def _runtime(
    tmp_path,
    *,
    artifact_type=ArtifactRepository,
    gtd_text=None,
    gerber_files=None,
    drill_text=DRILL,
    model_client=None,
):
    projects = ProjectRepository(tmp_path)
    material = ProjectMaterial(
        material_id="mat_1",
        kind="gerber_job",
        source_ref="sources/mat_1",
        display_name="S5 fixture",
        registered_order=1,
        registered_at=NOW,
        member_kinds=["gerber", "drill"] + (["gtd"] if gtd_text else []),
    )
    with projects.mutate("prj_test") as mutation:
        mutation.create(
            ProjectRecord(
                project_id="prj_test",
                project_name="S5 production Gerber",
                created_at=NOW,
                updated_at=NOW,
                materials=[material],
                latest_run_id=None,
            )
        )
    source = tmp_path / "projects" / "prj_test" / "sources" / "mat_1"
    source.mkdir(parents=True)
    for filename, content in (gerber_files or {"top.gbr": GERBER}).items():
        (source / filename).write_text(content, encoding="utf-8", newline="\n")
    (source / "holes.drl").write_text(drill_text, encoding="utf-8", newline="\n")
    if gtd_text:
        (source / "job.gtd").write_text(gtd_text, encoding="utf-8", newline="\n")
    artifacts = artifact_type(tmp_path)
    service = RunService(
        projects,
        artifacts,
        RuleSetService(tmp_path),
        model_client=model_client,
        clock=lambda: NOW,
        id_factory=lambda prefix: f"{prefix}_1",
        recover_on_startup=False,
    )
    return artifacts, service


def _wait_preparation(service, artifacts, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            summary = artifacts.read_run_summary("prj_test", "run_1")
            index = artifacts.read_artifact_index("prj_test", "run_1", summary.artifact_index_path)
            values = artifacts.read_gerber_preparations("prj_test", "run_1", index)
        except ServiceError:
            pass
        else:
            if values:
                return values[0]
        time.sleep(0.01)
    raise AssertionError("production Gerber preparation did not become readable")


def _wait_terminal(service, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = service.get_summary("prj_test", "run_1")
        if summary.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
            return summary
        time.sleep(0.01)
    raise AssertionError("run did not reach a terminal state")


def test_production_gerber_chain_emits_previews_13_outputs_and_evidence(tmp_path) -> None:
    artifacts, service = _runtime(tmp_path)
    service.create_run("prj_test", ["mat_1"], None)
    preparation = _wait_preparation(service, artifacts)
    assert [item.detected_type for item in preparation.layer_inputs] == ["drill", "gerber"]

    layers = [
        LayerDefinitionItem(
            layer_id=item.layer_id,
            file_path=item.file_path,
            role="drill" if item.detected_type == "drill" else "top_copper",
            copper_layer_index=None if item.detected_type == "drill" else 1,
        )
        for item in preparation.layer_inputs
    ]
    service.save_layer_definition_draft("prj_test", "run_1", "mat_1", layers)
    service.confirm_layer_definition("prj_test", "run_1", "mat_1")
    summary = _wait_terminal(service)
    assert summary.branch_status.gerber == "completed"

    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    result_data = json.loads(
        (run_root / "gerberscout" / "mat_1" / "result.json").read_text(encoding="utf-8")
    )
    assert len(result_data["outputs"]) == 13
    outputs = {item["key"]: item for item in result_data["outputs"]}
    assert outputs["layers.copper_layer_count"]["value"] == 1
    assert outputs["geometry.min_trace_width_mm"]["value"] == 0.2
    assert outputs["geometry.min_copper_spacing_mm"]["value"] == 4.4
    assert outputs["drill.used_hole_size_summary"]["value"] == [0.6]
    assert outputs["drill.used_hole_quantity_summary"]["value"] == [
        {"diameter_mm": 0.6, "quantity": 1}
    ]
    assert outputs["drill.slot_size_summary"]["value"] == [
        {"width_mm": 0.4, "length_mm": 2.4, "quantity": 1}
    ]
    assert outputs["drill.min_used_hole_diameter_mm"]["value"] == 0.6
    assert outputs["geometry.min_annular_ring_mm"]["value"] == 0.2
    assert {item["key"] for item in result_data["outputs"]} == {
        "layers.copper_layer_count",
        "layers.solder_mask_sides",
        "layers.silkscreen_sides",
        "geometry.job_bounds_mm",
        "geometry.min_trace_width_mm",
        "geometry.min_copper_spacing_mm",
        "drill.used_hole_size_summary",
        "drill.used_hole_quantity_summary",
        "drill.slot_size_summary",
        "drill.min_used_hole_diameter_mm",
        "geometry.min_annular_ring_mm",
        "drill.plating_summary.plated",
        "drill.plating_summary.non_plated",
    }
    summary_data = json.loads(
        (run_root / "gerberscout" / "mat_1" / "adapter_summary.json").read_text(encoding="utf-8")
    )
    assert {item["field_code"] for item in summary_data["field_outcomes"]} >= {
        "layer_count",
        "gerber_bounds_mm",
        "min_trace_width_mm",
        "min_trace_spacing_mm",
        "drill_size_list",
        "drill_quantity_list",
        "min_hole_diameter_mm",
        "min_annular_ring_mm",
    }

    branch = json.loads((run_root / "branches" / "gerber_result.json").read_text(encoding="utf-8"))
    assert branch["status"] == "completed"
    assert [item["artifact_kind"] for item in branch["evidence"]] == [
        "result",
        "adapter_summary",
    ]
    assert all(len(item["evidence_ids"]) == 2 for item in branch["fields"])

    preview_texts = [
        (run_root / "gerberscout" / "mat_1" / "preview" / f"{item.layer_id}.svg").read_text(
            encoding="utf-8"
        )
        for item in preparation.layer_inputs
    ]
    view_boxes = [text.split('viewBox="', 1)[1].split('"', 1)[0] for text in preview_texts]
    assert len(set(view_boxes)) == 1

    invalid = dict(result_data)
    invalid["outputs"] = invalid["outputs"][:-1]
    with pytest.raises(ValidationError):
        GerberResult.model_validate(invalid)
    service.shutdown(0)


def _confirm(service, preparation, *, unknown=False):
    layers = [
        LayerDefinitionItem(
            layer_id=item.layer_id,
            file_path=item.file_path,
            role=(
                "unknown" if unknown else "drill" if item.detected_type == "drill" else "top_copper"
            ),
            copper_layer_index=(1 if not unknown and item.detected_type == "gerber" else None),
        )
        for item in preparation.layer_inputs
    ]
    deadline = time.monotonic() + 2.0
    while True:
        try:
            service.save_layer_definition_draft(
                "prj_test", "run_1", preparation.job_material_id, layers
            )
        except ServiceError as exc:
            if exc.code != "gerber_preparation_not_ready" or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
        else:
            break
    service.confirm_layer_definition("prj_test", "run_1", preparation.job_material_id)


@pytest.mark.parametrize(
    (
        "unit_statement",
        "drill_text",
        "expected_trace_width",
        "expected_max_x",
        "expected_annular_ring",
    ),
    [
        ("%MOMM*%", DRILL, 0.2, 30.0, 0.2),
        ("%MOIN*%", INCH_REGION_DRILL, 5.08, 762.0, 5.08),
    ],
    ids=["mm", "inch"],
)
def test_standard_region_keeps_direct_measurements_and_stable_artifacts(
    tmp_path,
    unit_statement,
    drill_text,
    expected_trace_width,
    expected_max_x,
    expected_annular_ring,
) -> None:
    region_gerber = REGION_GERBER.replace("%MOMM*%", unit_statement)
    artifacts, service = _runtime(
        tmp_path,
        gerber_files={"top.gbr": region_gerber},
        drill_text=drill_text,
    )
    service.create_run("prj_test", ["mat_1"], None)
    preparation = _wait_preparation(service, artifacts)
    _confirm(service, preparation)
    summary = _wait_terminal(service)

    assert summary.branch_status.gerber in {"completed", "completed_with_warnings"}
    error_codes = {item.code for item in summary.error_summary}
    assert "gerber_result_contract_invalid" not in error_codes
    assert "gerber_no_adoptable_result" not in error_codes

    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    result_path = run_root / "gerberscout" / "mat_1" / "result.json"
    adapter_path = run_root / "gerberscout" / "mat_1" / "adapter_summary.json"
    result = json.loads(result_path.read_text("utf-8"))
    outputs = {item["key"]: item for item in result["outputs"]}
    assert len(outputs) == 13
    assert outputs["geometry.min_trace_width_mm"]["value"] == expected_trace_width
    assert outputs["geometry.job_bounds_mm"]["value"]["max_x"] == expected_max_x
    assert outputs["geometry.min_annular_ring_mm"]["value"] == expected_annular_ring
    assert adapter_path.is_file()

    branch = json.loads((run_root / "branches" / "gerber_result.json").read_text("utf-8"))
    assert branch["status"] in {"completed", "completed_with_warnings"}
    assert [item["artifact_kind"] for item in branch["evidence"]] == [
        "result",
        "adapter_summary",
    ]
    service.shutdown(0)


def _run_confirmed_result(tmp_path):
    artifacts, service = _runtime(tmp_path)
    service.create_run("prj_test", ["mat_1"], None)
    preparation = _wait_preparation(service, artifacts)
    _confirm(service, preparation)
    summary = _wait_terminal(service)
    result_path = (
        tmp_path
        / "projects"
        / "prj_test"
        / "runs"
        / "run_1"
        / "gerberscout"
        / "mat_1"
        / "result.json"
    )
    return json.loads(result_path.read_text("utf-8")), preparation, summary, service


def test_job_source_budget_degrades_only_polygon_dependent_capabilities(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(engine_module, "_MAX_JOB_SOURCE_OBJECTS", 1)
    result, preparation, summary, service = _run_confirmed_result(tmp_path)
    outputs = {item["key"]: item for item in result["outputs"]}

    assert summary.branch_status.gerber == "completed_with_warnings"
    assert outputs["geometry.job_bounds_mm"]["coverage"] == "none"
    assert outputs["geometry.min_copper_spacing_mm"]["coverage"] == "none"
    assert outputs["geometry.min_annular_ring_mm"]["coverage"] == "none"
    assert outputs["geometry.min_trace_width_mm"]["value"] == 0.2
    assert outputs["geometry.min_trace_width_mm"]["coverage"] == "complete"
    assert outputs["drill.used_hole_size_summary"]["value"] == [0.6]
    assert list(
        (
            tmp_path
            / "projects"
            / "prj_test"
            / "runs"
            / "run_1"
            / "gerberscout"
            / "mat_1"
            / "preview"
        ).glob("*.svg")
    )
    budget_keys = {
        key
        for item in result["checks"]
        if item["code"] == "gerber_geometry_budget_exceeded"
        for key in item["affected_output_keys"]
    }
    assert budget_keys == {
        "geometry.job_bounds_mm",
        "geometry.min_copper_spacing_mm",
        "geometry.min_annular_ring_mm",
    }
    assert preparation.layer_inputs
    service.shutdown(0)


def test_polygon_coordinate_budget_preserves_direct_measurements(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(engine_module, "_MAX_JOB_POLYGON_INPUT_COORDINATES", 1)
    result, _, _, service = _run_confirmed_result(tmp_path)
    outputs = {item["key"]: item for item in result["outputs"]}

    assert outputs["geometry.job_bounds_mm"]["coverage"] == "none"
    assert outputs["geometry.min_trace_width_mm"]["value"] == 0.2
    assert outputs["drill.min_used_hole_diameter_mm"]["value"] == 0.6
    assert any(item["code"] == "gerber_geometry_budget_exceeded" for item in result["checks"])
    service.shutdown(0)


def test_spacing_uses_batched_single_nearest_queries(monkeypatch) -> None:
    calls = []

    class RecordingTree:
        def __init__(self, components):
            assert len(components) == 3

        def query_nearest(self, batch, *, exclusive, all_matches, return_distance):
            calls.append((len(batch), exclusive, all_matches, return_distance))
            return None, [0.5] * len(batch)

    geometries = engine_module.unary_union(
        [
            engine_module.box(0, 0, 1, 1),
            engine_module.box(2, 0, 3, 1),
            engine_module.box(4, 0, 5, 1),
        ]
    )
    member = SimpleNamespace(relative_path="top.gbr")
    coverage = engine_module._Coverage()
    monkeypatch.setattr(engine_module, "STRtree", RecordingTree)
    monkeypatch.setattr(engine_module, "_SPACING_QUERY_BATCH_SIZE", 2)

    output = engine_module._spacing_output(
        [member],
        [],
        {"top.gbr": engine_module._LayerGeometry(geometries, True)},
        coverage,
    )

    assert output.value == 0.5
    assert output.coverage == "complete"
    assert calls == [(2, True, False, True), (1, True, False, True)]


def test_spacing_component_budget_degrades_only_spacing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(engine_module, "_MAX_SPACING_COMPONENTS", 1)
    result, _, _, service = _run_confirmed_result(tmp_path)
    outputs = {item["key"]: item for item in result["outputs"]}

    assert outputs["geometry.job_bounds_mm"]["coverage"] == "complete"
    assert outputs["geometry.min_copper_spacing_mm"]["coverage"] == "none"
    assert outputs["geometry.min_trace_width_mm"]["value"] == 0.2
    assert any(
        item["code"] == "gerber_geometry_budget_exceeded"
        and item["affected_output_keys"] == ["geometry.min_copper_spacing_mm"]
        for item in result["checks"]
    )
    service.shutdown(0)


def test_annular_candidate_relation_budget_preserves_drill_outputs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(engine_module, "_MAX_ANNULAR_CANDIDATE_RELATIONS", 0)
    result, _, _, service = _run_confirmed_result(tmp_path)
    outputs = {item["key"]: item for item in result["outputs"]}

    assert outputs["geometry.min_annular_ring_mm"]["coverage"] == "none"
    assert outputs["drill.used_hole_size_summary"]["value"] == [0.6]
    assert any(
        item["code"] == "gerber_geometry_budget_exceeded"
        and item["affected_output_keys"] == ["geometry.min_annular_ring_mm"]
        for item in result["checks"]
    )
    service.shutdown(0)


def test_arc_segment_budget_is_cumulative_per_job(monkeypatch) -> None:
    monkeypatch.setattr(engine_module, "_MAX_JOB_ARC_SEGMENTS", 20)
    item = SimpleNamespace(
        x1=1.0,
        y1=0.0,
        x2=0.0,
        y2=1.0,
        cx=0.0,
        cy=0.0,
        clockwise=False,
        width=0.2,
    )
    budget = engine_module._GeometryBudget()

    points = engine_module._arc_points(item, budget)
    assert len(points) == 19
    with pytest.raises(engine_module._GeometryBudgetExceeded):
        engine_module._arc_points(item, budget)
    assert budget.exhausted is True


def test_unknown_roles_degrade_fields_and_keep_deterministic_fallback(tmp_path) -> None:
    artifacts, service = _runtime(tmp_path)
    service.create_run("prj_test", ["mat_1"], None)
    preparation = _wait_preparation(service, artifacts)
    _confirm(service, preparation, unknown=True)
    summary = _wait_terminal(service)
    assert summary.branch_status.gerber == "completed_with_warnings"

    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    branch = json.loads((run_root / "branches" / "gerber_result.json").read_text("utf-8"))
    fields = {item["field_code"]: item for item in branch["fields"]}
    assert fields["gerber_bounds_mm"]["branch_state"] == "clear_now"
    assert fields["min_trace_width_mm"]["branch_state"] == "unknown"
    assert fields["drill_size_list"]["branch_state"] == "unknown"
    assert fields["min_trace_width_mm"]["check_note"].startswith("GerberScout 已形成候选值")
    fallback_errors = [
        item for item in summary.error_summary if item.code == "gerber_diagnostic_model_failed"
    ]
    assert fallback_errors
    assert all(item.severity == "warning" for item in fallback_errors)
    service.shutdown(0)


def test_diagnostic_success_records_one_response_event_after_artifact_commit(tmp_path) -> None:
    artifacts, service = _runtime(tmp_path, model_client=DiagnosticSuccessModel())
    service.create_run("prj_test", ["mat_1"], None)
    preparation = _wait_preparation(service, artifacts)
    _confirm(service, preparation, unknown=True)
    summary = _wait_terminal(service)

    diagnostic_summary = next(
        item
        for item in summary.model_call_summaries
        if item.call_purpose == CallPurpose.GERBER_DIAGNOSTIC
    )
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    events = [
        json.loads(line) for line in (run_root / "trajectory.jsonl").read_text("utf-8").splitlines()
    ]
    response_events = [
        item
        for item in events
        if item["event_type"] == "model_response_received"
        and item["summary"]["call_purpose"] == CallPurpose.GERBER_DIAGNOSTIC.value
    ]
    response_artifacts = list(
        (run_root / "gerberscout" / "mat_1" / "diagnostic").rglob("response.json")
    )
    assert diagnostic_summary.success_count > 0
    assert len(response_events) == diagnostic_summary.success_count
    assert len(response_artifacts) == diagnostic_summary.success_count
    assert all(len(item["artifact_paths"]) == 2 for item in response_events)
    assert all(item["artifact_paths"][1].endswith("/response.json") for item in response_events)
    service.shutdown(0)


def test_diagnostic_model_receives_readable_messages_without_canonical_links(tmp_path) -> None:
    model = DiagnosticSuccessModel()
    artifacts, service = _runtime(tmp_path, model_client=model)
    service.create_run("prj_test", ["mat_1"], None)
    preparation = _wait_preparation(service, artifacts)
    _confirm(service, preparation, unknown=True)
    _wait_terminal(service)

    requests = [
        item for item in model.requests if item.call_purpose == CallPurpose.GERBER_DIAGNOSTIC
    ]
    assert requests
    payloads = [json.loads(item.messages[1].content[0].text) for item in requests]
    system_prompt = requests[0].messages[0].content[0].text
    assert "说明诊断事实怎样影响当前字段" in system_prompt
    assert "不得输出内部标识" in system_prompt
    assert all(set(item) == {"task", "field_context", "response_schema"} for item in payloads)
    assert all(
        item["response_schema"]["append_check_note"]
        == "不含内部标识、状态枚举或路径的非空中文业务说明"
        for item in payloads
    )
    assert all(
        set(item["field_context"]) == {"field", "tool_findings", "errors"} for item in payloads
    )
    assert all(
        set(item["field_context"]["field"]) == {"field_name", "definition", "field_value", "unit"}
        for item in payloads
    )
    assert all(
        set(finding) == {"message"}
        for item in payloads
        for finding in item["field_context"]["tool_findings"]
    )
    assert all(
        set(error) == {"message", "suggested_action"}
        for item in payloads
        for error in item["field_context"]["errors"]
    )

    serialized = json.dumps(payloads, ensure_ascii=False)
    assert "层角色尚未明确" in serialized
    for forbidden in (
        "field_code",
        "output_key",
        "job_material_id",
        "source_material_id",
        "finding_id",
        "error_id",
        "status",
        '"mode"',
        "available",
        "unreliable",
        "missing_value",
        "unreliable_value",
        "finding_only",
        "role=unknown",
        "finding_",
        "err_",
        "mat_",
        "ev_",
    ):
        assert forbidden not in serialized

    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    branch = json.loads((run_root / "branches" / "gerber_result.json").read_text("utf-8"))
    affected = [item for item in branch["fields"] if item["tool_finding_ids"]]
    assert affected
    assert all(item["check_note"] == model.note for item in affected)
    service.shutdown(0)


def test_diagnostic_non_empty_text_is_preserved_without_semantic_rejection(tmp_path) -> None:
    model = DiagnosticSuccessModel(note="最小线宽当前状态为 unreliable，需要复核。")
    artifacts, service = _runtime(tmp_path, model_client=model)
    service.create_run("prj_test", ["mat_1"], None)
    preparation = _wait_preparation(service, artifacts)
    _confirm(service, preparation, unknown=True)
    summary = _wait_terminal(service)

    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    branch = json.loads((run_root / "branches" / "gerber_result.json").read_text("utf-8"))
    fields = {item["field_code"]: item for item in branch["fields"]}
    assert fields["min_trace_width_mm"]["check_note"] == model.note
    assert not any(item.code == "gerber_diagnostic_model_failed" for item in summary.error_summary)
    service.shutdown(0)


def test_diagnostic_model_identity_mismatch_keeps_only_request_artifact(tmp_path) -> None:
    artifacts, service = _runtime(
        tmp_path,
        model_client=DiagnosticSuccessModel(response_model_id="unexpected-model"),
    )
    service.create_run("prj_test", ["mat_1"], None)
    preparation = _wait_preparation(service, artifacts)
    _confirm(service, preparation, unknown=True)
    summary = _wait_terminal(service)

    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    assert any(item.code == "gerber_diagnostic_model_failed" for item in summary.error_summary)
    assert not list((run_root / "gerberscout" / "mat_1" / "diagnostic").rglob("response.json"))
    diagnostic_summary = next(
        item
        for item in summary.model_call_summaries
        if item.call_purpose == CallPurpose.GERBER_DIAGNOSTIC
    )
    events = [
        json.loads(line) for line in (run_root / "trajectory.jsonl").read_text("utf-8").splitlines()
    ]
    response_events = [
        item
        for item in events
        if item["event_type"] == "model_response_received"
        and item["summary"]["call_purpose"] == CallPurpose.GERBER_DIAGNOSTIC.value
    ]
    assert len(response_events) == diagnostic_summary.success_count
    assert all(len(item["artifact_paths"]) == 1 for item in response_events)
    service.shutdown(0)


def test_job_bounds_use_ordered_dark_clear_final_image(tmp_path) -> None:
    artifacts, service = _runtime(tmp_path, gerber_files={"top.gbr": DARK_CLEAR_GERBER})
    service.create_run("prj_test", ["mat_1"], None)
    preparation = _wait_preparation(service, artifacts)
    _confirm(service, preparation)
    _wait_terminal(service)

    result_path = (
        tmp_path
        / "projects"
        / "prj_test"
        / "runs"
        / "run_1"
        / "gerberscout"
        / "mat_1"
        / "result.json"
    )
    result = json.loads(result_path.read_text("utf-8"))
    outputs = {item["key"]: item for item in result["outputs"]}
    assert outputs["geometry.job_bounds_mm"]["value"] == {
        "min_x": -5.0,
        "min_y": -5.0,
        "max_x": 0.0,
        "max_y": 5.0,
    }
    service.shutdown(0)


def test_annular_ring_requires_a_unique_pad_on_each_known_copper_layer(tmp_path) -> None:
    artifacts, service = _runtime(
        tmp_path,
        gerber_files={
            "top.gbr": GERBER,
            "bottom.gbr": BOTTOM_WITHOUT_PAD,
        },
    )
    service.create_run("prj_test", ["mat_1"], None)
    preparation = _wait_preparation(service, artifacts)
    layers = []
    for item in preparation.layer_inputs:
        if item.detected_type == "drill":
            role = "drill"
            copper_layer_index = None
        elif item.file_path == "top.gbr":
            role = "top_copper"
            copper_layer_index = 1
        else:
            role = "bottom_copper"
            copper_layer_index = 2
        layers.append(
            LayerDefinitionItem(
                layer_id=item.layer_id,
                file_path=item.file_path,
                role=role,
                copper_layer_index=copper_layer_index,
            )
        )
    service.save_layer_definition_draft("prj_test", "run_1", "mat_1", layers)
    service.confirm_layer_definition("prj_test", "run_1", "mat_1")
    _wait_terminal(service)

    result_path = (
        tmp_path
        / "projects"
        / "prj_test"
        / "runs"
        / "run_1"
        / "gerberscout"
        / "mat_1"
        / "result.json"
    )
    result = json.loads(result_path.read_text("utf-8"))
    outputs = {item["key"]: item for item in result["outputs"]}
    annular = outputs["geometry.min_annular_ring_mm"]
    assert annular["value"] == 0.2
    assert annular["coverage"] == "partial"
    assert any(
        item["code"] == "gerber_capability_unsupported"
        and item["source_files"] == ["bottom.gbr"]
        and item["affected_output_keys"] == ["geometry.min_annular_ring_mm"]
        for item in result["checks"]
    )
    service.shutdown(0)


def test_gtd_npth_does_not_create_false_plated_requirement(tmp_path) -> None:
    artifacts, service = _runtime(
        tmp_path,
        gtd_text="{File holes.drl}\nDrill classification: NPTH / non-plated\n",
    )
    service.create_run("prj_test", ["mat_1"], None)
    preparation = _wait_preparation(service, artifacts)
    _confirm(service, preparation)
    _wait_terminal(service)

    result_path = (
        tmp_path
        / "projects"
        / "prj_test"
        / "runs"
        / "run_1"
        / "gerberscout"
        / "mat_1"
        / "result.json"
    )
    result = json.loads(result_path.read_text("utf-8"))
    outputs = {item["key"]: item for item in result["outputs"]}
    assert outputs["drill.plating_summary.plated"]["applicable"] is False
    assert outputs["drill.plating_summary.non_plated"]["applicable"] is True
    service.shutdown(0)


class FailingJobOneArtifactRepository(ArtifactRepository):
    def __init__(self, storage_root, filename):
        super().__init__(storage_root)
        self._filename = filename

    def replace_json_artifact(self, project_id, run_id, *, artifact, entry):
        if entry.path == f"gerberscout/mat_1/{self._filename}":
            raise ServiceError("artifact_write_failed")
        return super().replace_json_artifact(project_id, run_id, artifact=artifact, entry=entry)


class FailingJobOneIndexRepository(ArtifactRepository):
    def replace_artifact_index(self, project_id, run_id, index):
        if any(item.path == "gerberscout/mat_1/result.json" for item in index.entries):
            raise ServiceError("artifact_index_update_failed")
        return super().replace_artifact_index(project_id, run_id, index)


def _multi_runtime(tmp_path, failing_filename=None, *, index_failure=False):
    projects = ProjectRepository(tmp_path)
    materials = [
        ProjectMaterial(
            material_id=f"mat_{index}",
            kind="gerber_job",
            source_ref=f"sources/mat_{index}",
            display_name=f"S5 job {index}",
            registered_order=index,
            registered_at=NOW,
            member_kinds=["gerber", "drill"],
        )
        for index in (1, 2)
    ]
    with projects.mutate("prj_test") as mutation:
        mutation.create(
            ProjectRecord(
                project_id="prj_test",
                project_name="S5 multi-job",
                created_at=NOW,
                updated_at=NOW,
                materials=materials,
                latest_run_id=None,
            )
        )
    for material in materials:
        source = tmp_path / "projects" / "prj_test" / "sources" / material.material_id
        source.mkdir(parents=True)
        (source / "top.gbr").write_text(GERBER, encoding="utf-8", newline="\n")
        (source / "holes.drl").write_text(DRILL, encoding="utf-8", newline="\n")
    artifacts = (
        FailingJobOneIndexRepository(tmp_path)
        if index_failure
        else FailingJobOneArtifactRepository(tmp_path, failing_filename)
    )
    counters = {}

    def make_id(prefix):
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}_{counters[prefix]}"

    service = RunService(
        projects,
        artifacts,
        RuleSetService(tmp_path),
        clock=lambda: NOW,
        id_factory=make_id,
        recover_on_startup=False,
    )
    return artifacts, service


def _wait_preparations(service, artifacts, count=2, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            summary = artifacts.read_run_summary("prj_test", "run_1")
            index = artifacts.read_artifact_index("prj_test", "run_1", summary.artifact_index_path)
            values = artifacts.read_gerber_preparations("prj_test", "run_1", index)
        except ServiceError:
            pass
        else:
            if len(values) == count:
                return values
        time.sleep(0.01)
    raise AssertionError("all production Gerber preparations did not become readable")


@pytest.mark.parametrize("failing_filename", ["result.json", "adapter_summary.json"])
def test_multi_job_artifact_failure_drops_only_failed_job(tmp_path, failing_filename) -> None:
    artifacts, service = _multi_runtime(tmp_path, failing_filename)
    service.create_run("prj_test", ["mat_1", "mat_2"], None)
    for preparation in _wait_preparations(service, artifacts):
        _confirm(service, preparation)
    summary = _wait_terminal(service)
    assert summary.branch_status.gerber == "completed_with_warnings"
    assert any(item.code == "artifact_write_failed" for item in summary.error_summary)

    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    branch = json.loads((run_root / "branches" / "gerber_result.json").read_text("utf-8"))
    assert {item["source_material_id"] for item in branch["fields"]} == {"mat_2"}
    assert {item["source_material_id"] for item in branch["evidence"]} == {"mat_2"}
    assert all("mat_1" not in item["source_material_ids"] for item in branch["tool_findings"])
    service.shutdown(0)


def test_diagnostic_submission_rejects_whitespace_only_note() -> None:
    with pytest.raises(ValidationError):
        GerberDiagnosticSubmission(append_check_note="   ")


@pytest.mark.parametrize(
    "note",
    [
        "available",
        "最小线宽当前状态为 unreliable，需要复核。",
        "最小线宽关联 finding_id=finding_00000000-0000-4000-8000-000000000001。",
        "最小线宽关联 error_id=err_00000000-0000-4000-8000-000000000001。",
        "field_code=min_trace_width_mm 当前存在问题。",
        "请复核 projects/run_1/gerberscout/result.json。",
        "请把结果写入 append_check_note。",
    ],
)
def test_diagnostic_submission_treats_non_empty_note_as_opaque_text(note) -> None:
    submission = GerberDiagnosticSubmission(append_check_note=f"  {note}  ")

    assert submission.append_check_note == note


def test_artifact_index_failure_stops_remaining_jobs_and_exposes_no_candidates(
    tmp_path,
) -> None:
    artifacts, service = _multi_runtime(tmp_path, index_failure=True)
    service.create_run("prj_test", ["mat_1", "mat_2"], None)
    for preparation in _wait_preparations(service, artifacts):
        _confirm(service, preparation)
    summary = _wait_terminal(service)
    assert summary.branch_status.gerber == "failed"
    assert any(item.code == "artifact_index_update_failed" for item in summary.error_summary)

    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    branch = json.loads((run_root / "branches" / "gerber_result.json").read_text("utf-8"))
    assert branch["status"] == "failed"
    assert branch["fields"] == []
    assert branch["evidence"] == []
    assert branch["tool_findings"] == []
    assert not (run_root / "gerberscout" / "mat_2" / "result.json").exists()
    service.shutdown(0)


def test_missing_expected_output_uses_specific_error_code(tmp_path, monkeypatch) -> None:
    import flyboard.gerber.engine as engine_module

    real_plating_outputs = engine_module._plating_outputs

    def omit_expected_output(job, holes, coverage):
        outputs = real_plating_outputs(job, holes, coverage)
        outputs.pop("drill.plating_summary.non_plated")
        return outputs

    monkeypatch.setattr(engine_module, "_plating_outputs", omit_expected_output)
    artifacts, service = _runtime(tmp_path)
    service.create_run("prj_test", ["mat_1"], None)
    preparation = _wait_preparation(service, artifacts)
    _confirm(service, preparation)
    summary = _wait_terminal(service)

    codes = {item.code for item in summary.error_summary}
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    assert "gerber_expected_output_missing" in codes
    assert "gerber_result_contract_invalid" not in codes
    assert not (run_root / "gerberscout" / "mat_1" / "result.json").exists()
    assert not (run_root / "gerberscout" / "mat_1" / "adapter_summary.json").exists()
    service.shutdown(0)


def test_adapter_uses_spec_order_when_result_array_is_reversed(tmp_path, monkeypatch) -> None:
    import flyboard.gerber.analyzer as analyzer_module

    real_builder = analyzer_module.build_gerber_result

    def reverse_outputs(job, definition):
        result = real_builder(job, definition)
        return GerberResult.model_validate(
            {
                **result.model_dump(mode="python"),
                "outputs": list(reversed(result.outputs)),
            }
        )

    monkeypatch.setattr(analyzer_module, "build_gerber_result", reverse_outputs)
    artifacts, service = _runtime(tmp_path)
    service.create_run("prj_test", ["mat_1"], None)
    preparation = _wait_preparation(service, artifacts)
    _confirm(service, preparation)
    _wait_terminal(service)

    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    result = json.loads((run_root / "gerberscout" / "mat_1" / "result.json").read_text("utf-8"))
    summary = json.loads(
        (run_root / "gerberscout" / "mat_1" / "adapter_summary.json").read_text("utf-8")
    )
    assert [item["key"] for item in result["outputs"]] == list(reversed(OUTPUT_KEYS))
    applicable = {item["key"] for item in result["outputs"] if item["applicable"] is True}
    assert [item["output_key"] for item in summary["field_outcomes"]] == [
        key for key in OUTPUT_KEYS if key in applicable
    ]
    service.shutdown(0)
