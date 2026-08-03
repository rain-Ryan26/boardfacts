from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from flyboard.models import (
    ApiProblem,
    ArtifactIndex,
    BranchField,
    BranchResult,
    ConflictCandidate,
    DataListUnavailableSummary,
    ErrorRecord,
    Evidence,
    EvidenceMetadataResponse,
    LayerDefinition,
    LayerDefinitionItem,
    LayerDefinitionRequest,
    ModelCallSummary,
    NoRuleSet,
    PhaseTiming,
    ProjectDataListArtifact,
    ProjectDataRow,
    ProjectMaterial,
    ProjectRecord,
    RuleCheckResult,
    RuleCheckSummary,
    RuleInstruction,
    RunInputGroups,
    RunInputMaterial,
    RunInputSnapshot,
    RunSummary,
    SelectedRuleSet,
    TrajectoryEvent,
    build_current_field_table,
    materialize_run_summary,
    to_error_summary,
    to_evidence_metadata,
    to_project_summary,
    unwrap_project_data_list,
)

ERROR_ID = "err_550e8400-e29b-41d4-a716-446655440000"
FINDING_ID = "finding_550e8400-e29b-41d4-a716-446655440000"
SHA256 = "a" * 64
STARTED_AT = "2026-07-18T04:00:00.000Z"


def _project_material(**updates: object) -> ProjectMaterial:
    data: dict[str, object] = {
        "material_id": "mat_001",
        "kind": "pdf",
        "source_ref": "sources/mat_001/input.pdf",
        "display_name": "input.pdf",
        "registered_order": 1,
        "registered_at": STARTED_AT,
    }
    data.update(updates)
    return ProjectMaterial.model_validate(data)


def _run_input_material(**updates: object) -> RunInputMaterial:
    data: dict[str, object] = {
        "material_id": "mat_001",
        "material_order": 1,
        "kind": "pdf",
        "source_ref": "sources/mat_001/input.pdf",
        "sha256": SHA256,
        "registered_at": STARTED_AT,
    }
    data.update(updates)
    return RunInputMaterial.model_validate(data)


def _branch_field(**updates: object) -> BranchField:
    data: dict[str, object] = {
        "field_code": "board_thickness_mm",
        "field_name": "成品板厚",
        "field_value": "1.60 mm",
        "unit": "mm",
        "category": "板件设计参数",
        "branch_state": "clear_now",
        "source": "pdf_ai",
        "source_material_id": "mat_001",
        "source_ref": "PDF_1 第2页",
        "evidence_ids": [],
        "tool_finding_ids": [],
        "error_ids": [],
        "check_note": "PDF 标注。",
    }
    data.update(updates)
    return BranchField.model_validate(data)


def _error_record(**updates: object) -> ErrorRecord:
    data: dict[str, object] = {
        "error_id": ERROR_ID,
        "code": "rule_execution_failed",
        "severity": "error",
        "phase": "rule_check",
        "branch": None,
        "message": "规则执行失败。",
        "source_material_ids": [],
        "artifact_paths": ["rules/rule_trace.jsonl"],
        "suggested_action": "检查规则输入后创建新 run。",
    }
    data.update(updates)
    return ErrorRecord.model_validate(data)


def _skipped_rule_summary() -> RuleCheckSummary:
    return RuleCheckSummary(
        status="skipped",
        rule_set_id=None,
        rule_set_version=None,
        attempted_rule_count=0,
        error_count=0,
        reason="rule_set_not_selected",
    )


def _branch_status() -> dict[str, str]:
    return {"pdf_image": "completed", "xlsx": "skipped", "gerber": "skipped"}


def test_models_are_closed_and_reject_unknown_schema_version() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ProjectMaterial.model_validate(
            {
                **_project_material().model_dump(mode="json"),
                "analysis_status": "completed",
            }
        )

    with pytest.raises(ValidationError):
        RunInputSnapshot(
            schema_version="2.0",
            project_id="project_001",
            run_id="run_001",
            snapshot_id="snapshot_001",
            materials=[_run_input_material()],
            groups=RunInputGroups(
                pdf_inputs=["mat_001"],
                image_inputs=[],
                spreadsheet_inputs=[],
                gerber_jobs=[],
                unsupported_materials=[],
            ),
            field_table=build_current_field_table(),
            rule_set=NoRuleSet(mode="not_selected", reason="rule_set_not_selected"),
        )


def test_project_material_conditional_fields_and_serialization() -> None:
    gerber = _project_material(
        material_id="mat_gerber",
        kind="gerber_job",
        source_ref="sources/mat_gerber",
        display_name="job",
        member_kinds=["gerber", "drill", "gtd"],
    )
    assert [kind.value for kind in gerber.member_kinds or []] == ["gerber", "drill", "gtd"]

    with pytest.raises(ValidationError, match="ordered gerber, drill, gtd"):
        _project_material(
            kind="gerber_job",
            source_ref="sources/mat_001",
            member_kinds=["drill", "gerber"],
        )
    with pytest.raises(ValidationError, match="must be omitted"):
        _project_material(member_kinds=None)
    with pytest.raises(ValidationError, match="requires unsupported_reason_code"):
        _project_material(kind="unsupported")

    unsupported = _project_material(
        kind="unsupported",
        unsupported_reason_code="pdf_invalid",
    )
    assert unsupported.unsupported_reason_code == "pdf_invalid"
    assert "member_kinds" not in _project_material().model_dump(mode="json")


@pytest.mark.parametrize(
    "source_ref",
    [
        "C:/outside/input.pdf",
        "/outside/input.pdf",
        "sources\\mat_001\\input.pdf",
        "sources/mat_001/../input.pdf",
        "file://outside/input.pdf",
        "sources//input.pdf",
    ],
)
def test_project_source_locator_rejects_unsafe_lexical_paths(source_ref: str) -> None:
    with pytest.raises(ValidationError):
        _project_material(source_ref=source_ref)


def test_branch_source_ref_is_an_identifier_not_a_file_locator() -> None:
    field = _branch_field(source_ref="PDF_1 第2页 / 标注 A")
    assert field.source_ref == "PDF_1 第2页 / 标注 A"


def test_run_input_snapshot_requires_complete_stable_groups() -> None:
    snapshot = RunInputSnapshot(
        schema_version="1.0",
        project_id="project_001",
        run_id="run_001",
        snapshot_id="snapshot_001",
        materials=[_run_input_material()],
        groups=RunInputGroups(
            pdf_inputs=["mat_001"],
            image_inputs=[],
            spreadsheet_inputs=[],
            gerber_jobs=[],
            unsupported_materials=[],
        ),
        field_table=build_current_field_table(),
        rule_set=SelectedRuleSet(
            mode="selected",
            rule_set_id="factory_rules",
            rule_set_name="工厂规则",
            rule_set_version="2026.07",
            schema_version="1.0",
            source_kind="system_rule_set_file",
            source_path="rule_sets/factory_rules.json",
            content_sha256=SHA256,
            snapshot_artifact="rules/rule_set_snapshot.json",
        ),
    )
    assert snapshot.groups.pdf_inputs == ["mat_001"]

    with pytest.raises(ValidationError, match="group for pdf"):
        snapshot.model_copy(
            update={
                "groups": RunInputGroups(
                    pdf_inputs=[],
                    image_inputs=["mat_001"],
                    spreadsheet_inputs=[],
                    gerber_jobs=[],
                    unsupported_materials=[],
                )
            }
        ).model_validate(
            snapshot.model_copy(
                update={
                    "groups": RunInputGroups(
                        pdf_inputs=[],
                        image_inputs=["mat_001"],
                        spreadsheet_inputs=[],
                        gerber_jobs=[],
                        unsupported_materials=[],
                    )
                }
            ).model_dump()
        )


def test_evidence_seed_is_a_closed_discriminated_union() -> None:
    data = {
        "evidence_id": "ev_pdf_001",
        "type": "pdf_page",
        "source_material_id": "mat_001",
        "file_path": "sources/mat_001/input.pdf",
        "page_index": 1,
        "region": {"x": 0, "y": 1, "width": 10, "height": 20, "unit": "px"},
        "note": "板厚标注",
        "source_asset_path": "pdf/assets/asset_000001.png",
    }
    result = BranchResult(
        schema_version="1.0",
        run_id="run_001",
        branch="pdf_image",
        status="completed",
        source_material_ids=["mat_001"],
        fields=[_branch_field(evidence_ids=["ev_pdf_001"])],
        unknown_notes=[],
        tool_findings=[],
        evidence=[data],
        errors=[],
    )
    assert result.evidence[0].type == "pdf_page"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        BranchResult.model_validate(
            {
                **result.model_dump(mode="json"),
                "evidence": [{**data, "artifact_path": "gerber/result.json"}],
            }
        )


def test_branch_result_rejects_dangling_or_cross_source_evidence() -> None:
    with pytest.raises(ValidationError, match="same source"):
        BranchResult(
            schema_version="1.0",
            run_id="run_001",
            branch="pdf_image",
            status="completed",
            source_material_ids=["mat_001", "mat_002"],
            fields=[_branch_field(evidence_ids=["ev_pdf_001"])],
            unknown_notes=[],
            tool_findings=[],
            evidence=[
                {
                    "evidence_id": "ev_pdf_001",
                    "type": "pdf_page",
                    "source_material_id": "mat_002",
                    "file_path": "sources/mat_002/input.pdf",
                    "page_index": 1,
                    "region": None,
                    "note": None,
                    "source_asset_path": "pdf/assets/asset_000001.png",
                }
            ],
            errors=[],
        )


def test_project_data_row_conflict_mapping_is_self_consistent() -> None:
    candidates = [
        ConflictCandidate(
            field_value="1.60 mm",
            source_material_id="mat_001",
            source_ref="XLSX_1_sheet1",
            evidence_ids=[],
        ),
        ConflictCandidate(
            field_value="1.50 mm",
            source_material_id="mat_002",
            source_ref="PDF_1 第2页",
            evidence_ids=["ev_pdf_001"],
        ),
    ]
    row = ProjectDataRow(
        index=1,
        field_code="board_thickness_mm",
        field_name="成品板厚",
        field_value="1.60 mm",
        source="XLSX_1_sheet1",
        evidence_ids=["ev_pdf_001"],
        tool_finding_ids=[],
        error_ids=[],
        conflict_candidates=candidates,
        category="板件设计参数",
        field_state="conflict",
        check_note="来源不一致。",
        check_conclusion="需要复核。",
    )
    assert row.evidence_ids == ["ev_pdf_001"]

    with pytest.raises(ValidationError, match="stable candidate evidence union"):
        ProjectDataRow.model_validate({**row.model_dump(mode="json"), "evidence_ids": []})


def test_rule_contracts_enforce_decision_and_terminal_matrices() -> None:
    RuleInstruction(
        decision="no_violation",
        append_check_note="规则适用且未发现违反。",
        check_conclusion=None,
    )
    with pytest.raises(ValidationError, match="null check_conclusion"):
        RuleInstruction(
            decision="no_violation",
            append_check_note="未发现违反。",
            check_conclusion="错误覆盖",
        )

    skipped = RuleCheckResult(
        schema_version="1.0",
        run_id="run_001",
        status="skipped",
        reason="rule_set_not_selected",
        rule_set=None,
        executed_rule_ids=[],
        attempted_rule_count=0,
        errors=[],
    )
    assert skipped.status == "skipped"
    with pytest.raises(ValidationError, match="invalid field combination"):
        RuleCheckResult.model_validate(
            {**skipped.model_dump(mode="json"), "attempted_rule_count": 1}
        )


def test_phase_timing_enforces_scope_and_exact_duration() -> None:
    timing = PhaseTiming(
        phase="branch_analysis",
        scope_type="branch",
        scope_id="pdf_image",
        status="completed",
        started_at=STARTED_AT,
        finished_at="2026-07-18T04:00:01.234Z",
        duration_ms=1234,
    )
    assert timing.duration_ms == 1234
    with pytest.raises(ValidationError, match="duration_ms"):
        PhaseTiming.model_validate({**timing.model_dump(mode="json"), "duration_ms": 1233})
    with pytest.raises(ValidationError, match="branch scope"):
        PhaseTiming.model_validate({**timing.model_dump(mode="json"), "scope_id": "unknown"})


def test_layer_definition_enforces_copper_contract_and_closed_request() -> None:
    layers = [
        LayerDefinitionItem(
            layer_id="top",
            file_path="top.gbr",
            role="top_copper",
            copper_layer_index=1,
        ),
        LayerDefinitionItem(
            layer_id="inner",
            file_path="inner.gbr",
            role="inner_copper",
            copper_layer_index=2,
        ),
        LayerDefinitionItem(
            layer_id="bottom",
            file_path="bottom.gbr",
            role="bottom_copper",
            copper_layer_index=3,
        ),
    ]
    definition = LayerDefinition(
        project_id="project_001",
        run_id="run_001",
        job_material_id="mat_gerber",
        layers=layers,
        updated_at=STARTED_AT,
        confirmed_at="2026-07-18T04:00:01.000Z",
    )
    assert definition.confirmed_at is not None

    with pytest.raises(ValidationError, match="greater than all inner"):
        LayerDefinition.model_validate(
            {
                **definition.model_dump(mode="json"),
                "layers": [
                    layers[0].model_dump(mode="json"),
                    {**layers[1].model_dump(mode="json"), "copper_layer_index": 4},
                    layers[2].model_dump(mode="json"),
                ],
            }
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        LayerDefinitionRequest.model_validate(
            {
                "project_id": "project_001",
                "run_id": "run_001",
                "job_material_id": "mat_gerber",
                "layers": [item.model_dump(mode="json") for item in layers],
                "updated_at": STARTED_AT,
            }
        )


def test_artifact_index_and_trajectory_reject_duplicate_or_invalid_entries() -> None:
    entry = {
        "path": "project_data_list.json",
        "kind": "project_data_list",
        "producer_phase": "rule_check",
        "consumers": ["api", "gui"],
        "access_level": "project_scoped",
        "source_material_ids": ["mat_001"],
    }
    with pytest.raises(ValidationError, match="entries.path"):
        ArtifactIndex(
            schema_version="1.0",
            run_id="run_001",
            entries=[entry, entry],
        )

    with pytest.raises(ValidationError, match="phase_started"):
        TrajectoryEvent(
            schema_version="1.0",
            run_id="run_001",
            timestamp=STARTED_AT,
            phase="integration",
            branch=None,
            event_type="phase_started",
            summary={"batch_count": 1},
            duration_ms=1,
            source_material_ids=[],
            artifact_paths=[],
            error_ids=[],
        )


def test_api_problem_is_closed_and_requires_http_error_status() -> None:
    problem = ApiProblem(
        kind="api_problem",
        code="not_found",
        message="资源不存在。",
        status=404,
        error_id=None,
        details={"project_id": "project_001"},
    )
    assert problem.status == 404
    with pytest.raises(ValidationError):
        ApiProblem.model_validate({**problem.model_dump(mode="json"), "status": 200})


def test_pure_projections_exclude_internal_locators_and_do_not_mutate_sources() -> None:
    record = ProjectRecord(
        project_id="project_001",
        project_name="样例项目",
        created_at="2026-07-18T12:00:00+08:00",
        updated_at="2026-07-18T12:00:01+08:00",
        materials=[_project_material()],
        latest_run_id="run_001",
    )
    project_summary = to_project_summary(record)
    assert project_summary.material_count == 1
    assert project_summary.model_dump(mode="json")["created_at"] == "2026-07-18T04:00:00.000Z"

    error = _error_record()
    projected_error = to_error_summary(error)
    assert "artifact_paths" not in projected_error.model_dump(mode="json")

    evidence = Evidence(
        evidence_id="ev_pdf_001",
        type="pdf_page",
        source_material_id="mat_001",
        file_path="sources/mat_001/input.pdf",
        page_index=1,
        asset_path="pdf/highlights/ev_pdf_001.png",
        region=None,
        note="板厚标注",
    )
    metadata = to_evidence_metadata(evidence)
    assert isinstance(metadata, EvidenceMetadataResponse)
    assert "asset_path" not in metadata.model_dump(mode="json")

    running_timing = PhaseTiming(
        phase="branch_analysis",
        scope_type="branch",
        scope_id="pdf_image",
        status="running",
        started_at=STARTED_AT,
        finished_at=None,
        duration_ms=None,
    )
    summary = RunSummary(
        schema_version="1.0",
        run_id="run_001",
        project_id="project_001",
        status="running",
        current_phase="branch_analysis",
        phase_timings=[running_timing],
        branch_status={"pdf_image": "running", "xlsx": "skipped", "gerber": "skipped"},
        rule_check_summary=_skipped_rule_summary(),
        model_call_summaries=[
            ModelCallSummary(
                call_purpose="xlsx",
                logical_call_count=1,
                success_count=1,
                failure_count=0,
                total_duration_ms=120,
                transport_retry_count=0,
            )
        ],
        error_summary=[error],
        stop_requested_at=None,
        stop_reason=None,
        started_at=STARTED_AT,
        finished_at=None,
        duration_ms=None,
        artifact_index_path="artifact_index.json",
        project_data_list_path=None,
    )
    response = materialize_run_summary(
        summary,
        now=datetime(2026, 7, 18, 4, 0, 3, tzinfo=timezone.utc),
    )
    assert response.duration_ms == 3000
    assert response.phase_timings[0].duration_ms == 3000
    assert summary.duration_ms is None
    assert summary.phase_timings[0].duration_ms is None
    assert response.model_call_summaries == summary.model_call_summaries
    assert response.model_call_summaries[0] is not summary.model_call_summaries[0]
    dumped = response.model_dump(mode="json")
    assert "artifact_index_path" not in dumped
    assert "project_data_list_path" not in dumped
    assert "artifact_paths" not in dumped["error_summary"][0]
    assert dumped["model_call_summaries"][0]["call_purpose"] == "xlsx"

    missing = summary.model_dump(mode="python")
    missing.pop("model_call_summaries")
    with pytest.raises(ValidationError, match="model_call_summaries"):
        RunSummary.model_validate(missing)

    wrong_order = summary.model_dump(mode="python")
    wrong_order["model_call_summaries"] = [
        {
            "call_purpose": "integration",
            "logical_call_count": 1,
            "success_count": 1,
            "failure_count": 0,
            "total_duration_ms": 1,
            "transport_retry_count": 0,
        },
        wrong_order["model_call_summaries"][0],
    ]
    with pytest.raises(ValidationError, match="stable call purpose order"):
        RunSummary.model_validate(wrong_order)


def test_project_data_list_unwrap_returns_a_deep_copy() -> None:
    row = ProjectDataRow(
        index=1,
        field_code="base_material",
        field_name="基材",
        field_value="FR4",
        source="XLSX_1_sheet1",
        evidence_ids=[],
        tool_finding_ids=[],
        error_ids=[],
        conflict_candidates=[],
        category="板件设计参数",
        field_state="confirmed",
        check_note="单一来源。",
        check_conclusion="检查通过",
    )
    artifact = ProjectDataListArtifact(schema_version="1.0", run_id="run_001", rows=[row])
    rows = unwrap_project_data_list(artifact)
    assert rows == [row]
    assert rows is not artifact.rows
    assert rows[0] is not artifact.rows[0]


def test_data_list_unavailable_summary_keeps_rule_and_run_state() -> None:
    summary = DataListUnavailableSummary(
        kind="data_list_unavailable",
        run_status="running",
        reason_code=None,
        current_phase="branch_analysis",
        branch_status=_branch_status(),
        rule_check_summary=_skipped_rule_summary(),
        error_summary=[],
        stop_requested_at=None,
        stop_reason=None,
        run_summary_endpoint="/projects/project_001/runs/run_001",
    )
    assert summary.rule_check_summary.status == "skipped"


def test_material_source_ref_must_match_material_identity_and_kind() -> None:
    with pytest.raises(ValidationError, match="rooted at sources"):
        _project_material(source_ref="project.json")
    with pytest.raises(ValidationError, match="rooted at sources"):
        _project_material(source_ref="sources/mat_other/input.pdf")
    with pytest.raises(ValidationError, match="managed source file"):
        _project_material(source_ref="sources/mat_001")
    with pytest.raises(ValidationError, match="managed source directory"):
        _project_material(
            kind="gerber_job",
            source_ref="sources/mat_001/job",
            member_kinds=["gerber"],
        )
