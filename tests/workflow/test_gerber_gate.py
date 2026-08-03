from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from flyboard.models import (
    AccessLevel,
    ArtifactConsumer,
    ArtifactIndexEntry,
    BranchCode,
    BranchResult,
    GerberJobPreparation,
    LayerDefinitionItem,
    PhaseCode,
    ProjectMaterial,
    ProjectRecord,
    RunStatus,
)
from flyboard.repositories import ArtifactRepository, ProjectRepository
from flyboard.services import LayerDefinitionService, RuleSetService, ServiceError
from flyboard.workflow import RunService

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def make_gerber_runtime(tmp_path, analyzer, *, job_count=1, artifact_type=ArtifactRepository):
    projects = ProjectRepository(tmp_path)
    materials = [
        ProjectMaterial(
            material_id=f"mat_{index}",
            kind="gerber_job",
            source_ref=f"sources/mat_{index}",
            display_name=f"job_{index}",
            registered_order=index,
            registered_at=NOW,
            member_kinds=["gerber"],
        )
        for index in range(1, job_count + 1)
    ]
    with projects.mutate("prj_test") as mutation:
        mutation.create(
            ProjectRecord(
                project_id="prj_test",
                project_name="Gerber 测试",
                created_at=NOW,
                updated_at=NOW,
                materials=materials,
                latest_run_id=None,
            )
        )
    for material in materials:
        source = tmp_path / "projects" / "prj_test" / "sources" / material.material_id / "top.gbr"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%FSLAX24Y24*%\nM02*")

    artifacts = artifact_type(tmp_path)
    service = RunService(
        projects,
        artifacts,
        RuleSetService(tmp_path),
        gerber_analyzer=analyzer,
        layer_definitions=LayerDefinitionService(),
        clock=lambda: NOW,
        id_factory=lambda prefix: f"{prefix}_1",
        recover_on_startup=False,
    )
    return artifacts, service


def wait_summary(service, expected, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = service.get_summary("prj_test", "run_1")
        if summary.status == expected:
            return summary
        time.sleep(0.01)
    raise AssertionError(f"run did not reach {expected}")


def wait_for_draft_save(service, job_material_id="mat_1", timeout=3.0):
    layers = [
        LayerDefinitionItem(
            layer_id="layer_top",
            file_path="top.gbr",
            role="top_copper",
            copper_layer_index=1,
        )
    ]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return service.save_layer_definition_draft(
                "prj_test",
                "run_1",
                job_material_id,
                layers,
            )
        except ServiceError as exc:
            if exc.code not in {"gerber_preparation_not_ready", "run_state_conflict"}:
                raise
            time.sleep(0.01)
    raise AssertionError("Gerber preparation did not become draftable")


class SuccessfulGerberAnalyzer:
    def __init__(self) -> None:
        self.analysis_calls = 0
        self.analysis_inputs = None

    def prepare(self, snapshot, job_material_id, context):
        preparation = GerberJobPreparation(
            schema_version="1.0",
            project_id="prj_test",
            run_id="run_1",
            job_material_id=job_material_id,
            layer_inputs=[
                {
                    "layer_id": "layer_top",
                    "file_path": "top.gbr",
                    "detected_type": "gerber",
                }
            ],
        )
        context.artifact_writer.write_content(
            b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            "image/svg+xml",
            ArtifactIndexEntry(
                path=f"gerberscout/{job_material_id}/preview/layer_top.svg",
                kind="gerber_preview",
                producer_phase=PhaseCode.GERBER_PREPARATION,
                consumers=[
                    ArtifactConsumer.API,
                    ArtifactConsumer.GUI,
                    ArtifactConsumer.REGRESSION,
                ],
                access_level=AccessLevel.PROJECT_SCOPED,
                source_material_ids=[job_material_id],
            ),
        )
        context.artifact_writer.write_json(
            preparation,
            ArtifactIndexEntry(
                path=f"gerberscout/{job_material_id}/preparation.json",
                kind="gerber_preparation",
                producer_phase=PhaseCode.GERBER_PREPARATION,
                consumers=[
                    ArtifactConsumer.WORKFLOW,
                    ArtifactConsumer.API,
                    ArtifactConsumer.GUI,
                    ArtifactConsumer.REGRESSION,
                ],
                access_level=AccessLevel.PROJECT_SCOPED,
                source_material_ids=[job_material_id],
            ),
        )
        return preparation, []

    def analyze(self, snapshot, context, preparations, layer_definitions):
        self.analysis_calls += 1
        self.analysis_inputs = (list(preparations), list(layer_definitions))
        return BranchResult(
            schema_version="1.0",
            run_id="run_1",
            branch=BranchCode.GERBER,
            status="completed",
            source_material_ids=list(snapshot.groups.gerber_jobs),
            fields=[
                {
                    "field_code": "board_thickness_mm",
                    "field_name": "成品板厚",
                    "field_value": "1.6",
                    "unit": "mm",
                    "category": "板件设计参数",
                    "branch_state": "clear_now",
                    "source": "gerberscout",
                    "source_material_id": preparations[0].job_material_id,
                    "source_ref": "top.gbr",
                    "evidence_ids": [],
                    "tool_finding_ids": [],
                    "error_ids": [],
                    "check_note": "测试 Gerber 候选。",
                }
            ],
            unknown_notes=[],
            tool_findings=[],
            evidence=[],
            errors=[],
        )


def failed_preparation(job_material_id, context):
    error = context.diagnostics.record_error(
        code="gerber_input_parse_failed",
        severity="error",
        phase=PhaseCode.GERBER_PREPARATION,
        branch=BranchCode.GERBER,
        message="测试 preparation 失败。",
        source_material_ids=[job_material_id],
        artifact_paths=[],
        suggested_action="检查测试输入。",
    )
    return None, [error]


class FailedGerberAnalyzer:
    def prepare(self, snapshot, job_material_id, context):
        return failed_preparation(job_material_id, context)

    def analyze(self, snapshot, context, preparations, layer_definitions):
        raise AssertionError("all-failed preparations must not schedule analysis")


class MixedGerberAnalyzer(SuccessfulGerberAnalyzer):
    def prepare(self, snapshot, job_material_id, context):
        if job_material_id == "mat_1":
            return failed_preparation(job_material_id, context)
        return super().prepare(snapshot, job_material_id, context)


class FailGerberBranchResultWrite(ArtifactRepository):
    def replace_json_artifact(self, project_id, run_id, *, artifact, entry):
        if entry.kind == "branch_result" and entry.source_material_ids == ["mat_1"]:
            raise ServiceError("artifact_write_failed")
        return super().replace_json_artifact(
            project_id,
            run_id,
            artifact=artifact,
            entry=entry,
        )


class FailDefinitionArtifactEvent(ArtifactRepository):
    def __init__(self, storage_root):
        super().__init__(storage_root)
        self.definition_artifact_events = 0
        self.fail_on_definition_artifact_event = 1

    def append_trajectory(self, project_id, run_id, event):
        if event.event_type == "artifact_written" and event.artifact_paths == [
            "layer_definitions.json"
        ]:
            self.definition_artifact_events += 1
            if self.definition_artifact_events == self.fail_on_definition_artifact_event:
                raise ServiceError("trajectory_write_failed")
        return super().append_trajectory(project_id, run_id, event)


def test_confirmed_preparation_schedules_gerber_analysis_once(tmp_path) -> None:
    analyzer = SuccessfulGerberAnalyzer()
    _, service = make_gerber_runtime(tmp_path, analyzer)
    service.create_run("prj_test", ["mat_1"], None)

    draft = wait_for_draft_save(service)
    confirmed = service.confirm_layer_definition("prj_test", "run_1", "mat_1")
    assert confirmed.confirmed_at == NOW
    assert draft.updated_at == confirmed.updated_at

    failed_at_next_barrier = wait_summary(service, RunStatus.FAILED)
    assert failed_at_next_barrier.branch_status.gerber == "completed", [
        (error.code, error.phase, error.branch) for error in failed_at_next_barrier.error_summary
    ]
    assert failed_at_next_barrier.error_summary[-1].phase == "integration"
    assert analyzer.analysis_calls == 1
    preparations, definitions = analyzer.analysis_inputs
    assert [item.job_material_id for item in preparations] == ["mat_1"]
    assert [item.job_material_id for item in definitions] == ["mat_1"]

    repeated = service.confirm_layer_definition("prj_test", "run_1", "mat_1")
    assert repeated == confirmed
    assert analyzer.analysis_calls == 1
    service.shutdown(0)


def test_all_failed_preparations_commit_failed_gerber_result(tmp_path) -> None:
    _, service = make_gerber_runtime(tmp_path, FailedGerberAnalyzer())
    service.create_run("prj_test", ["mat_1"], None)
    failed = wait_summary(service, RunStatus.FAILED)

    assert failed.branch_status.gerber == "failed"
    preparation_errors = [
        error for error in failed.error_summary if error.code == "gerber_input_parse_failed"
    ]
    assert len(preparation_errors) == 1
    result_path = (
        tmp_path / "projects" / "prj_test" / "runs" / "run_1" / "branches" / "gerber_result.json"
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["errors"][0]["error_id"] == preparation_errors[0].error_id
    service.shutdown(0)


def test_all_failed_preparation_result_write_failure_closes_run(tmp_path) -> None:
    _, service = make_gerber_runtime(
        tmp_path,
        FailedGerberAnalyzer(),
        artifact_type=FailGerberBranchResultWrite,
    )
    service.create_run("prj_test", ["mat_1"], None)
    failed = wait_summary(service, RunStatus.FAILED)

    errors = [error for error in failed.error_summary if error.code == "branch_result_write_failed"]
    assert len(errors) == 1
    assert failed.branch_status.gerber == "failed"
    gerber_timings = [
        timing
        for timing in failed.phase_timings
        if timing.phase == "branch_analysis"
        and timing.scope_type == "branch"
        and timing.scope_id == "gerber"
    ]
    assert len(gerber_timings) == 1
    assert gerber_timings[0].status == "failed"
    session = service._registry.get("prj_test", "run_1")
    assert session is None or session.control.task_count() == 0
    service.shutdown(0)


def test_draft_returns_committed_definition_when_artifact_event_fails(tmp_path) -> None:
    analyzer = SuccessfulGerberAnalyzer()
    _, service = make_gerber_runtime(
        tmp_path,
        analyzer,
        artifact_type=FailDefinitionArtifactEvent,
    )
    service.create_run("prj_test", ["mat_1"], None)

    draft = wait_for_draft_save(service)
    assert draft.updated_at == NOW
    assert draft.confirmed_at is None
    failed = wait_summary(service, RunStatus.FAILED)
    assert analyzer.analysis_calls == 0
    assert [
        error.code for error in failed.error_summary if error.code == "run_record_write_failed"
    ] == ["run_record_write_failed"]
    persisted = json.loads(
        (
            tmp_path / "projects" / "prj_test" / "runs" / "run_1" / "layer_definitions.json"
        ).read_text(encoding="utf-8")
    )
    assert persisted["definitions"][0]["updated_at"] is not None
    assert persisted["definitions"][0]["confirmed_at"] is None
    service.shutdown(0)


def test_confirm_returns_committed_definition_when_artifact_event_fails(tmp_path) -> None:
    analyzer = SuccessfulGerberAnalyzer()
    artifacts, service = make_gerber_runtime(
        tmp_path,
        analyzer,
        artifact_type=FailDefinitionArtifactEvent,
    )
    artifacts.fail_on_definition_artifact_event = 2
    service.create_run("prj_test", ["mat_1"], None)
    wait_for_draft_save(service)

    confirmed = service.confirm_layer_definition("prj_test", "run_1", "mat_1")
    assert confirmed.confirmed_at == NOW
    failed = wait_summary(service, RunStatus.FAILED)
    assert analyzer.analysis_calls == 0
    assert [
        error.code for error in failed.error_summary if error.code == "run_record_write_failed"
    ] == ["run_record_write_failed"]

    repeated = service.confirm_layer_definition("prj_test", "run_1", "mat_1")
    assert repeated == confirmed
    persisted = json.loads(
        (
            tmp_path / "projects" / "prj_test" / "runs" / "run_1" / "layer_definitions.json"
        ).read_text(encoding="utf-8")
    )
    assert persisted["definitions"][0]["confirmed_at"] is not None
    service.shutdown(0)


def test_partial_preparation_failure_is_merged_into_gerber_result(tmp_path) -> None:
    analyzer = MixedGerberAnalyzer()
    _, service = make_gerber_runtime(tmp_path, analyzer, job_count=2)
    service.create_run("prj_test", ["mat_1", "mat_2"], None)

    wait_for_draft_save(service, "mat_2")
    service.confirm_layer_definition("prj_test", "run_1", "mat_2")
    failed_at_next_barrier = wait_summary(service, RunStatus.FAILED)

    assert failed_at_next_barrier.branch_status.gerber == "completed_with_warnings"
    assert analyzer.analysis_calls == 1
    preparations, definitions = analyzer.analysis_inputs
    assert [item.job_material_id for item in preparations] == ["mat_2"]
    assert [item.job_material_id for item in definitions] == ["mat_2"]

    result_path = (
        tmp_path / "projects" / "prj_test" / "runs" / "run_1" / "branches" / "gerber_result.json"
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "completed_with_warnings"
    assert result["source_material_ids"] == ["mat_1", "mat_2"]
    assert [error["code"] for error in result["errors"]] == ["gerber_input_parse_failed"]
    service.shutdown(0)


def test_stop_while_waiting_for_layer_confirmation_does_not_analyze(tmp_path) -> None:
    analyzer = SuccessfulGerberAnalyzer()
    _, service = make_gerber_runtime(tmp_path, analyzer)
    service.create_run("prj_test", ["mat_1"], None)
    wait_for_draft_save(service)

    stopping = service.request_stop("prj_test", "run_1")
    assert stopping.status == RunStatus.RUNNING
    stopped = wait_summary(service, RunStatus.STOPPED)

    assert stopped.branch_status.gerber == "stopped"
    assert analyzer.analysis_calls == 0
    service.shutdown(0)
