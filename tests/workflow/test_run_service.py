from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

import pytest

from flyboard.models import (
    BranchCode,
    BranchResult,
    BranchStatusMap,
    ProjectMaterial,
    ProjectRecord,
    RuleCheckSummary,
    RunStatus,
    RunSummary,
)
from flyboard.repositories import ArtifactRepository, ProjectRepository
from flyboard.services import RuleSetService, ServiceError
from flyboard.workflow import RunService

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def make_runtime(
    tmp_path,
    *,
    kind="pdf",
    worker=None,
    thread_factory=threading.Thread,
    artifact_type=ArtifactRepository,
):
    projects = ProjectRepository(tmp_path)
    material_values = {
        "material_id": "mat_1",
        "kind": kind,
        "source_ref": "sources/mat_1/input.bin",
        "display_name": "input.bin",
        "registered_order": 1,
        "registered_at": NOW,
    }
    if kind == "unsupported":
        material_values["unsupported_reason_code"] = "unsupported_file_format"
    material = ProjectMaterial.model_validate(material_values)
    record = ProjectRecord(
        project_id="prj_test",
        project_name="测试",
        created_at=NOW,
        updated_at=NOW,
        materials=[material],
        latest_run_id=None,
    )
    with projects.mutate("prj_test") as mutation:
        mutation.create(record)
    source = tmp_path / "projects" / "prj_test" / "sources" / "mat_1" / "input.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable input")
    artifacts = artifact_type(tmp_path)
    rule_sets = RuleSetService(tmp_path)
    workers = {BranchCode.PDF_IMAGE: worker} if worker is not None else {}
    service = RunService(
        projects,
        artifacts,
        rule_sets,
        branch_workers=workers,
        clock=lambda: NOW,
        id_factory=lambda prefix: f"{prefix}_1",
        thread_factory=thread_factory,
        recover_on_startup=False,
    )
    return projects, artifacts, service


def wait_summary(service, expected, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = service.get_summary("prj_test", "run_1")
        if summary.status == expected:
            return summary
        time.sleep(0.01)
    raise AssertionError(f"run did not reach {expected}")


def test_s3_defaults_do_not_override_explicit_downstream_owners(tmp_path) -> None:
    explicit_rule_owner = object()
    explicit_evidence_owner = object()
    service = RunService(
        ProjectRepository(tmp_path),
        ArtifactRepository(tmp_path),
        RuleSetService(tmp_path),
        rule_engine=explicit_rule_owner,
        evidence_service=explicit_evidence_owner,
        recover_on_startup=False,
    )

    assert service.integrator is None
    assert service.rule_engine is explicit_rule_owner
    assert service.evidence_service is explicit_evidence_owner
    service.shutdown(0)


def test_create_run_persists_snapshot_then_fails_no_processable_materials(tmp_path) -> None:
    projects, _, service = make_runtime(tmp_path, kind="unsupported")
    created = service.create_run("prj_test", ["mat_1"], None)
    failed = wait_summary(service, RunStatus.FAILED)

    assert created.status == RunStatus.QUEUED
    assert projects.get("prj_test").latest_run_id == "run_1"
    assert failed.error_summary[0].code == "no_processable_materials"
    assert failed.branch_status.model_dump(mode="json") == {
        "pdf_image": "skipped",
        "xlsx": "skipped",
        "gerber": "skipped",
    }
    view = service.get_data_list_view("prj_test", "run_1")
    assert view.reason_code == "no_processable_materials"
    assert view.error_summary[0].error_id == failed.error_summary[0].error_id
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    assert (run_root / "run_input_snapshot.json").is_file()
    assert (run_root / "artifact_index.json").is_file()
    assert not (run_root / "project_data_list.json").exists()
    assert not list((run_root / "branches").glob("*_result.json"))
    events = [
        json.loads(line)
        for line in (run_root / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    failed_event = next(event for event in events if event["event_type"] == "run_failed")
    assert failed_event["error_ids"] == [failed.error_summary[0].error_id]
    deadline = time.monotonic() + 2
    while service._registry.snapshot() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service._registry.snapshot() == ()
    service.shutdown(0)


def test_global_admission_conflict_and_running_stop_drain_worker(tmp_path) -> None:
    release = threading.Event()

    def worker(snapshot, context):
        release.wait(2)
        context.stop_signal.check_boundary()
        raise AssertionError("stop boundary should have interrupted the worker")

    _, _, service = make_runtime(tmp_path, worker=worker)
    service.create_run("prj_test", ["mat_1"], None)
    wait_summary(service, RunStatus.RUNNING)

    with pytest.raises(ServiceError) as conflict:
        service.create_run("prj_test", ["mat_1"], None)
    assert conflict.value.code == "active_run_exists"
    assert conflict.value.active_project_id == "prj_test"
    assert conflict.value.active_run_id == "run_1"
    assert conflict.value.run_summary.status == RunStatus.RUNNING

    stopping = service.request_stop("prj_test", "run_1")
    assert stopping.status == RunStatus.RUNNING
    assert stopping.stop_requested_at == NOW
    assert stopping.stop_reason is None
    release.set()
    stopped = wait_summary(service, RunStatus.STOPPED)
    assert stopped.stop_reason == "user_requested"
    assert stopped.branch_status.pdf_image == "stopped"
    service.shutdown(0)


def test_queued_stop_commits_before_coordinator_and_does_not_start_worker(tmp_path) -> None:
    coordinator_waiting = threading.Event()
    release_coordinator = threading.Event()
    worker_started = threading.Event()

    def worker(snapshot, context):
        worker_started.set()
        raise AssertionError("queued stop must prevent branch submission")

    def delayed_thread_factory(**kwargs):
        target = kwargs.pop("target")
        args = kwargs.pop("args", ())

        def delayed_target():
            coordinator_waiting.set()
            release_coordinator.wait(2)
            target(*args)

        return threading.Thread(target=delayed_target, **kwargs)

    _, artifacts, service = make_runtime(
        tmp_path,
        worker=worker,
        thread_factory=delayed_thread_factory,
    )
    try:
        created = service.create_run("prj_test", ["mat_1"], None)
        assert created.status == RunStatus.QUEUED
        assert coordinator_waiting.wait(1)

        stopped = service.request_stop("prj_test", "run_1")
        assert stopped.status == RunStatus.STOPPED
        assert stopped.stop_requested_at == NOW
        assert stopped.stop_reason == "user_requested"

        release_coordinator.set()
        deadline = time.monotonic() + 2
        while service._registry.snapshot() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service._registry.snapshot() == ()
        assert not worker_started.is_set()
        assert artifacts.read_run_summary("prj_test", "run_1").status == RunStatus.STOPPED
    finally:
        release_coordinator.set()
        service.shutdown(0)


def test_global_admission_conflict_reports_active_run_from_another_project(tmp_path) -> None:
    release = threading.Event()

    def worker(snapshot, context):
        release.wait(2)
        context.stop_signal.check_boundary()
        raise AssertionError("stop boundary should have interrupted the worker")

    projects, _, service = make_runtime(tmp_path, worker=worker)
    other_material = ProjectMaterial(
        material_id="mat_2",
        kind="pdf",
        source_ref="sources/mat_2/input.pdf",
        display_name="input.pdf",
        registered_order=1,
        registered_at=NOW,
    )
    with projects.mutate("prj_other") as mutation:
        mutation.create(
            ProjectRecord(
                project_id="prj_other",
                project_name="另一个项目",
                created_at=NOW,
                updated_at=NOW,
                materials=[other_material],
                latest_run_id=None,
            )
        )
    other_source = tmp_path / "projects" / "prj_other" / "sources" / "mat_2" / "input.pdf"
    other_source.parent.mkdir(parents=True)
    other_source.write_bytes(b"stable input")

    try:
        service.create_run("prj_test", ["mat_1"], None)
        wait_summary(service, RunStatus.RUNNING)

        with pytest.raises(ServiceError) as conflict:
            service.create_run("prj_other", ["mat_2"], None)
        assert conflict.value.code == "active_run_exists"
        assert conflict.value.active_project_id == "prj_test"
        assert conflict.value.active_run_id == "run_1"
        assert conflict.value.run_summary.status == RunStatus.RUNNING

        service.request_stop("prj_test", "run_1")
        release.set()
        wait_summary(service, RunStatus.STOPPED)
    finally:
        release.set()
        service.shutdown(0)


def test_parallel_branches_share_executor_and_wait_for_full_barrier(tmp_path) -> None:
    projects = ProjectRepository(tmp_path)
    materials = [
        ProjectMaterial(
            material_id="mat_pdf",
            kind="pdf",
            source_ref="sources/mat_pdf/input.pdf",
            display_name="input.pdf",
            registered_order=1,
            registered_at=NOW,
        ),
        ProjectMaterial(
            material_id="mat_xlsx",
            kind="spreadsheet",
            source_ref="sources/mat_xlsx/input.xlsx",
            display_name="input.xlsx",
            registered_order=2,
            registered_at=NOW,
        ),
    ]
    with projects.mutate("prj_test") as mutation:
        mutation.create(
            ProjectRecord(
                project_id="prj_test",
                project_name="测试",
                created_at=NOW,
                updated_at=NOW,
                materials=materials,
                latest_run_id=None,
            )
        )
    for material in materials:
        source = tmp_path / "projects" / "prj_test" / material.source_ref
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"stable input")

    pdf_started = threading.Event()
    xlsx_started = threading.Event()
    release_pdf = threading.Event()
    release_xlsx = threading.Event()

    def pdf_worker(snapshot, context):
        pdf_started.set()
        release_pdf.wait(2)
        raise RuntimeError("pdf branch failure")

    def xlsx_worker(snapshot, context):
        xlsx_started.set()
        release_xlsx.wait(2)
        raise RuntimeError("xlsx branch failure")

    service = RunService(
        projects,
        ArtifactRepository(tmp_path),
        RuleSetService(tmp_path),
        branch_workers={
            BranchCode.PDF_IMAGE: pdf_worker,
            BranchCode.XLSX: xlsx_worker,
        },
        clock=lambda: NOW,
        id_factory=lambda prefix: f"{prefix}_1",
        recover_on_startup=False,
    )
    try:
        service.create_run("prj_test", ["mat_pdf", "mat_xlsx"], None)
        assert pdf_started.wait(1)
        assert xlsx_started.wait(1)

        release_pdf.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = service.get_summary("prj_test", "run_1")
            if current.branch_status.pdf_image == "failed":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("pdf branch did not commit its terminal result")

        assert current.status == RunStatus.RUNNING
        assert current.branch_status.xlsx == "running"

        release_xlsx.set()
        failed = wait_summary(service, RunStatus.FAILED)
        assert failed.branch_status.pdf_image == "failed"
        assert failed.branch_status.xlsx == "failed"
        assert [item.code for item in failed.error_summary].count("branch_unhandled_exception") == 2
        assert failed.error_summary[-1].code == "no_adoptable_branch_results"
    finally:
        release_pdf.set()
        release_xlsx.set()
        service.shutdown(0)


def test_service_shutdown_drains_worker_then_commits_interrupted_failure(tmp_path) -> None:
    release = threading.Event()

    def worker(snapshot, context):
        release.wait(2)
        context.stop_signal.check_boundary()
        raise AssertionError("shutdown boundary should have interrupted the worker")

    _, _, service = make_runtime(tmp_path, worker=worker)
    service.create_run("prj_test", ["mat_1"], None)
    wait_summary(service, RunStatus.RUNNING)

    shutdown_thread = threading.Thread(target=service.shutdown, args=(2,))
    shutdown_thread.start()
    try:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            sessions = service._registry.snapshot()
            if sessions and sessions[0].control.reason == "service_shutdown":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("service shutdown intent was not latched")

        release.set()
        shutdown_thread.join(3)
        assert not shutdown_thread.is_alive()
        failed = service.get_summary("prj_test", "run_1")
        assert failed.status == RunStatus.FAILED
        assert failed.stop_reason is None
        assert failed.error_summary[-1].code == "service_interrupted"
        assert failed.branch_status.pdf_image == "failed"
    finally:
        release.set()
        shutdown_thread.join(3)


def test_selected_branch_without_owner_fails_instead_of_staying_running(tmp_path) -> None:
    _, _, service = make_runtime(tmp_path)
    service.branch_workers[BranchCode.PDF_IMAGE] = None
    service.create_run("prj_test", ["mat_1"], None)
    failed = wait_summary(service, RunStatus.FAILED)

    assert failed.error_summary[-1].code == "run_dispatch_failed"
    assert failed.branch_status.pdf_image == "failed"
    service.shutdown(0)


def test_unhandled_branch_exception_writes_failed_result_and_tool_envelope(tmp_path) -> None:
    def worker(snapshot, context):
        raise RuntimeError("boom")

    _, _, service = make_runtime(tmp_path, worker=worker)
    service.create_run("prj_test", ["mat_1"], None)
    failed = wait_summary(service, RunStatus.FAILED)
    codes = [item.code for item in failed.error_summary]

    assert "branch_unhandled_exception" in codes
    assert "no_adoptable_branch_results" in codes
    run_root = tmp_path / "projects" / "prj_test" / "runs" / "run_1"
    assert (run_root / "branches" / "pdf_image_result.json").is_file()
    assert (run_root / "integration" / "tool_findings.json").is_file()
    assert service.get_tool_findings_view("prj_test", "run_1") == []
    service.shutdown(0)


def test_post_commit_dispatch_failure_returns_failed_without_rolling_back_latest(tmp_path) -> None:
    def failing_thread_factory(**kwargs):
        raise RuntimeError("thread unavailable")

    projects, _, service = make_runtime(tmp_path, thread_factory=failing_thread_factory)
    response = service.create_run("prj_test", ["mat_1"], None)

    assert response.status == RunStatus.FAILED
    assert response.error_summary[-1].code == "run_dispatch_failed"
    assert projects.get("prj_test").latest_run_id == "run_1"
    service.shutdown(0)


def queued_summary(status=RunStatus.QUEUED, stop_requested_at=None):
    return RunSummary(
        schema_version="1.0",
        run_id="run_1",
        project_id="prj_test",
        status=status,
        current_phase=None if status == RunStatus.QUEUED else "branch_analysis",
        phase_timings=[],
        branch_status=BranchStatusMap(pdf_image="queued", xlsx="skipped", gerber="skipped"),
        rule_check_summary=RuleCheckSummary(
            status="skipped",
            rule_set_id=None,
            rule_set_version=None,
            attempted_rule_count=0,
            error_count=0,
            reason="rule_set_not_selected",
        ),
        model_call_summaries=[],
        error_summary=[],
        stop_requested_at=stop_requested_at,
        stop_reason=None,
        started_at=NOW,
        finished_at=None,
        duration_ms=None,
        artifact_index_path="artifact_index.json",
        project_data_list_path=None,
    )


@pytest.mark.parametrize(
    ("status", "stop_requested_at", "expected"),
    [
        (RunStatus.QUEUED, None, RunStatus.FAILED),
        (RunStatus.RUNNING, None, RunStatus.FAILED),
        (RunStatus.RUNNING, NOW, RunStatus.STOPPED),
    ],
)
def test_startup_scan_closes_nonterminal_runs(
    tmp_path, status, stop_requested_at, expected
) -> None:
    projects = ProjectRepository(tmp_path)
    with projects.mutate("prj_test") as mutation:
        mutation.create(
            ProjectRecord(
                project_id="prj_test",
                project_name="测试",
                created_at=NOW,
                updated_at=NOW,
                materials=[],
                latest_run_id="run_1",
            )
        )
    artifacts = ArtifactRepository(tmp_path)
    artifacts.replace_run_summary(
        "prj_test",
        "run_1",
        queued_summary(status, stop_requested_at),
    )
    service = RunService(
        projects,
        artifacts,
        RuleSetService(tmp_path),
        clock=lambda: NOW,
    )
    recovered = service.get_summary("prj_test", "run_1")

    assert recovered.status == expected
    if expected == RunStatus.FAILED:
        assert recovered.error_summary[-1].code == "service_interrupted"
    else:
        assert recovered.stop_reason == "user_requested"
    service.shutdown(0)


class FailSecondIndexWrite(ArtifactRepository):
    def __init__(self, storage_root):
        super().__init__(storage_root)
        self.index_writes = 0

    def replace_artifact_index(self, project_id, run_id, index):
        self.index_writes += 1
        if self.index_writes == 2:
            raise ServiceError("artifact_index_update_failed")
        return super().replace_artifact_index(project_id, run_id, index)


def test_creation_index_failure_keeps_old_latest_and_reuses_first_error(tmp_path) -> None:
    projects, _, service = make_runtime(
        tmp_path,
        kind="unsupported",
        artifact_type=FailSecondIndexWrite,
    )
    with pytest.raises(ServiceError) as caught:
        service.create_run("prj_test", ["mat_1"], None)

    assert caught.value.code == "artifact_index_update_failed"
    assert projects.get("prj_test").latest_run_id is None
    persisted = service.artifacts.read_run_summary("prj_test", "run_1")
    assert persisted.status == RunStatus.FAILED
    assert [error.code for error in persisted.error_summary] == ["artifact_index_update_failed"]
    assert persisted.error_summary[0].error_id == caught.value.error_id
    assert persisted.artifact_index_path is None
    assert (
        tmp_path / "projects" / "prj_test" / "runs" / "run_1" / "run_input_snapshot.json"
    ).is_file()
    service.shutdown(0)


def test_precommit_project_failure_keeps_old_latest_and_compensates_run(
    tmp_path, monkeypatch
) -> None:
    from flyboard.repositories.projects import ProjectMutation

    projects, artifacts, service = make_runtime(tmp_path)

    def fail_commit(self, next_record, *, expected_latest_run_id="not_checked"):
        raise ServiceError("project_record_write_failed")

    monkeypatch.setattr(ProjectMutation, "commit", fail_commit)
    with pytest.raises(ServiceError) as error:
        service.create_run("prj_test", ["mat_1"], None)

    assert error.value.code == "project_record_write_failed"
    assert projects.get("prj_test").latest_run_id is None
    compensated = artifacts.read_run_summary("prj_test", "run_1")
    assert compensated.status == RunStatus.FAILED
    assert compensated.error_summary[-1].code == "run_creation_failed"
    service.shutdown(0)


class FailBranchResultWrite(ArtifactRepository):
    def replace_json_artifact(self, project_id, run_id, *, artifact, entry):
        if entry.kind == "branch_result":
            raise ServiceError("artifact_write_failed")
        return super().replace_json_artifact(
            project_id,
            run_id,
            artifact=artifact,
            entry=entry,
        )


class FailSecondTrajectoryAppend(ArtifactRepository):
    def __init__(self, storage_root):
        super().__init__(storage_root)
        self.trajectory_appends = 0

    def append_trajectory(self, project_id, run_id, event):
        self.trajectory_appends += 1
        if self.trajectory_appends == 2:
            raise ServiceError("trajectory_write_failed")
        return super().append_trajectory(project_id, run_id, event)


def test_nested_branch_write_failure_reuses_one_diagnostic_identity(tmp_path) -> None:
    def worker(snapshot, context):
        raise RuntimeError("boom")

    _, _, service = make_runtime(
        tmp_path,
        worker=worker,
        artifact_type=FailBranchResultWrite,
    )
    service.create_run("prj_test", ["mat_1"], None)
    failed = wait_summary(service, RunStatus.FAILED)
    branch_write_errors = [
        error for error in failed.error_summary if error.code == "branch_result_write_failed"
    ]

    assert len(branch_write_errors) == 1
    assert len({error.error_id for error in branch_write_errors}) == 1
    service.shutdown(0)


def test_trajectory_failure_is_not_renumbered_by_coordinator(tmp_path) -> None:
    release = threading.Event()

    def worker(snapshot, context):
        release.wait(1)
        raise RuntimeError("worker should not start after trajectory failure")

    _, _, service = make_runtime(
        tmp_path,
        worker=worker,
        artifact_type=FailSecondTrajectoryAppend,
    )
    service.create_run("prj_test", ["mat_1"], None)
    failed = wait_summary(service, RunStatus.FAILED)
    record_errors = [
        error for error in failed.error_summary if error.code == "run_record_write_failed"
    ]

    assert len(record_errors) == 1
    release.set()
    service.shutdown(0)


def test_adoptable_branch_without_business_chain_fails_at_integration_barrier(
    tmp_path,
) -> None:
    def worker(snapshot, context):
        return BranchResult.model_validate(
            {
                "schema_version": "1.0",
                "run_id": "run_1",
                "branch": "pdf_image",
                "status": "completed",
                "source_material_ids": ["mat_1"],
                "fields": [
                    {
                        "field_code": "board_thickness_mm",
                        "field_name": "成品板厚",
                        "field_value": "1.6",
                        "unit": "mm",
                        "category": "板件设计参数",
                        "branch_state": "clear_now",
                        "source": "pdf_ai",
                        "source_material_id": "mat_1",
                        "source_ref": "PDF_1_page1",
                        "evidence_ids": [],
                        "tool_finding_ids": [],
                        "error_ids": [],
                        "check_note": "测试候选。",
                    }
                ],
                "unknown_notes": [],
                "tool_findings": [],
                "evidence": [],
                "errors": [],
            }
        )

    _, _, service = make_runtime(tmp_path, worker=worker)
    service.create_run("prj_test", ["mat_1"], None)
    failed = wait_summary(service, RunStatus.FAILED)

    assert failed.error_summary[-1].code == "run_dispatch_failed"
    assert failed.error_summary[-1].phase == "integration"
    assert failed.branch_status.pdf_image == "completed"
    service.shutdown(0)
