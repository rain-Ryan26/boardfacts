from __future__ import annotations

from datetime import datetime, timezone

import fitz
import pytest

from flyboard.ingestion import (
    MaterialStager,
    RegistrationFailure,
    StagedUnit,
    StagingResult,
)
from flyboard.models import (
    MaterialKind,
    ProjectRecord,
    RegistrationFailureReason,
    RunSummary,
)
from flyboard.repositories import ArtifactRepository, ProjectRepository
from flyboard.services import MaterialService, ProjectService, ServiceError

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def make_pdf(path) -> None:
    document = fitz.open()
    document.new_page()
    document.save(path)
    document.close()


def test_project_and_material_services_commit_once_and_deduplicate(tmp_path) -> None:
    repository = ProjectRepository(tmp_path)
    projects = ProjectService(
        repository,
        ArtifactRepository(tmp_path),
        clock=lambda: NOW,
        project_id_factory=lambda: "prj_test",
    )
    summary = projects.create_project("  项目 A  ")
    assert summary.project_name == "项目 A"

    source = tmp_path / "drawing.pdf"
    make_pdf(source)
    materials = MaterialService(
        repository,
        MaterialStager(tmp_path),
        clock=lambda: NOW,
        request_id_factory=iter(["req_1", "req_2"]).__next__,
        material_id_factory=lambda: "mat_1",
    )
    first = materials.register_materials("prj_test", str(source))
    record_path = tmp_path / "projects" / "prj_test" / "project.json"
    committed = record_path.read_bytes()
    second = materials.register_materials("prj_test", str(source))

    assert [item.material_id for item in first.materials] == ["mat_1"]
    assert [item.material_id for item in second.materials] == ["mat_1"]
    assert second.failures == []
    assert record_path.read_bytes() == committed
    assert projects.get_materials("prj_test")[0].source_ref == "sources/mat_1/drawing.pdf"


def test_material_registration_missing_project_leaves_no_sources(tmp_path) -> None:
    repository = ProjectRepository(tmp_path)
    source = tmp_path / "drawing.pdf"
    make_pdf(source)
    service = MaterialService(
        repository,
        MaterialStager(tmp_path),
        request_id_factory=lambda: "req_1",
    )
    with pytest.raises(ServiceError) as error:
        service.register_materials("missing", str(source))
    assert error.value.code == "project_not_found"
    assert not (tmp_path / "projects" / "missing" / "sources").exists()


def test_project_delete_scans_all_run_summaries_not_only_latest(tmp_path) -> None:
    repository = ProjectRepository(tmp_path)
    artifacts = ArtifactRepository(tmp_path)
    projects = ProjectService(
        repository,
        artifacts,
        clock=lambda: NOW,
        project_id_factory=lambda: "prj_test",
    )
    projects.create_project("测试")
    active = RunSummary(
        schema_version="1.0",
        run_id="run_not_latest",
        project_id="prj_test",
        status="queued",
        current_phase=None,
        phase_timings=[],
        branch_status={"pdf_image": "queued", "xlsx": "queued", "gerber": "queued"},
        rule_check_summary={
            "status": "skipped",
            "rule_set_id": None,
            "rule_set_version": None,
            "attempted_rule_count": 0,
            "error_count": 0,
            "reason": "rule_set_not_selected",
        },
        model_call_summaries=[],
        error_summary=[],
        stop_requested_at=None,
        stop_reason=None,
        started_at=NOW,
        finished_at=None,
        duration_ms=None,
        artifact_index_path="artifact_index.json",
        project_data_list_path=None,
    )
    artifacts.replace_run_summary("prj_test", "run_not_latest", active)

    with pytest.raises(ServiceError) as error:
        projects.delete_project("prj_test", True)
    assert error.value.code == "active_run_exists"
    assert error.value.active_run_id == "run_not_latest"
    assert repository.get("prj_test").project_id == "prj_test"


def test_material_service_startup_recovers_staging_and_source_orphans(tmp_path) -> None:
    repository = ProjectRepository(tmp_path)
    projects = ProjectService(
        repository,
        ArtifactRepository(tmp_path),
        clock=lambda: NOW,
        project_id_factory=lambda: "prj_test",
    )
    projects.create_project("测试")
    stale = tmp_path / "staging" / "prj_test" / "req_old" / "unit_1"
    stale.mkdir(parents=True)
    (stale / "input.bin").write_bytes(b"stale")
    orphan = tmp_path / "projects" / "prj_test" / "sources" / "mat_orphan"
    orphan.mkdir(parents=True)
    (orphan / "input.bin").write_bytes(b"orphan")

    MaterialService(repository, MaterialStager(tmp_path))
    assert not (tmp_path / "staging" / "prj_test").exists()
    assert not orphan.exists()


def test_project_and_material_id_collisions_allocate_new_opaque_ids(tmp_path) -> None:
    repository = ProjectRepository(tmp_path)
    artifacts = ArtifactRepository(tmp_path)
    with repository.mutate("prj_collision") as mutation:
        mutation.create(
            ProjectRecord(
                project_id="prj_collision",
                project_name="占用",
                created_at=NOW,
                updated_at=NOW,
                materials=[],
                latest_run_id=None,
            )
        )
    project_ids = iter(["prj_collision", "prj_new"])
    projects = ProjectService(
        repository,
        artifacts,
        clock=lambda: NOW,
        project_id_factory=project_ids.__next__,
    )
    assert projects.create_project("新项目").project_id == "prj_new"

    occupied = tmp_path / "projects" / "prj_new" / "sources" / "mat_collision"
    occupied.mkdir(parents=True)
    (occupied / "old.bin").write_bytes(b"occupied")
    source = tmp_path / "new.pdf"
    make_pdf(source)
    material_ids = iter(["mat_collision", "mat_new"])
    materials = MaterialService(
        repository,
        MaterialStager(tmp_path),
        clock=lambda: NOW,
        request_id_factory=lambda: "req_collision",
        material_id_factory=material_ids.__next__,
        recover_on_startup=False,
    )
    registered = materials.register_materials("prj_new", str(source))
    assert [material.material_id for material in registered.materials] == ["mat_new"]
    assert (occupied / "old.bin").read_bytes() == b"occupied"


class StubStager:
    def __init__(self, result):
        self.result = result

    def stage(self, project_id, request_id, input_path):
        return self.result


def make_staging_result(tmp_path, refs, failures=()):
    request_root = tmp_path / "staged_request"
    request_root.mkdir()
    units = []
    for index, ref in enumerate(refs, start=1):
        unit_root = request_root / f"unit_{index}"
        unit_root.mkdir()
        display_name = ref.rsplit("/", 1)[-1]
        (unit_root / display_name).write_bytes(ref.encode("utf-8"))
        units.append(
            StagedUnit(
                input_ref=ref,
                display_name=display_name,
                kind=MaterialKind.PDF,
                staging_root=unit_root,
                fingerprint=f"fingerprint-{index}",
            )
        )
    return StagingResult(
        request_root=request_root,
        units=tuple(units),
        failures=tuple(failures),
    )


def test_material_promotion_failure_keeps_successes_on_both_sides(tmp_path, monkeypatch) -> None:
    import flyboard.services.materials as materials_module

    repository = ProjectRepository(tmp_path)
    ProjectService(
        repository,
        ArtifactRepository(tmp_path),
        clock=lambda: NOW,
        project_id_factory=lambda: "prj_test",
    ).create_project("测试")
    staging = make_staging_result(tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    original_replace = materials_module.os.replace

    def fail_middle(source, target):
        if source.name == "unit_2":
            raise OSError("promotion failed")
        return original_replace(source, target)

    monkeypatch.setattr(materials_module.os, "replace", fail_middle)
    material_ids = iter(["mat_1", "mat_2", "mat_3"])
    service = MaterialService(
        repository,
        StubStager(staging),
        clock=lambda: NOW,
        request_id_factory=lambda: "req_1",
        material_id_factory=material_ids.__next__,
        recover_on_startup=False,
    )
    result = service.register_materials("prj_test", "unused")

    assert [material.material_id for material in result.materials] == ["mat_1", "mat_3"]
    assert [failure.input_ref for failure in result.failures] == ["b.pdf"]
    assert result.failures[0].reason_code == RegistrationFailureReason.SOURCE_COPY_FAILED


def test_scan_and_promotion_failures_have_stable_root_first_order(tmp_path, monkeypatch) -> None:
    import flyboard.services.materials as materials_module

    repository = ProjectRepository(tmp_path)
    ProjectService(
        repository,
        ArtifactRepository(tmp_path),
        clock=lambda: NOW,
        project_id_factory=lambda: "prj_test",
    ).create_project("测试")
    staging = make_staging_result(
        tmp_path,
        ["z.pdf"],
        failures=(RegistrationFailure(".", RegistrationFailureReason.CLASSIFICATION_FAILED),),
    )
    monkeypatch.setattr(
        materials_module.os,
        "replace",
        lambda source, target: (_ for _ in ()).throw(OSError("promotion failed")),
    )
    service = MaterialService(
        repository,
        StubStager(staging),
        clock=lambda: NOW,
        request_id_factory=lambda: "req_1",
        material_id_factory=lambda: "mat_1",
        recover_on_startup=False,
    )
    result = service.register_materials("prj_test", "unused")

    assert [failure.input_ref for failure in result.failures] == [".", "z.pdf"]
    assert [failure.reason_code for failure in result.failures] == [
        RegistrationFailureReason.CLASSIFICATION_FAILED,
        RegistrationFailureReason.SOURCE_COPY_FAILED,
    ]


def test_project_create_atomic_failure_leaves_no_partial_record(tmp_path, monkeypatch) -> None:
    import flyboard.repositories.projects as projects_module

    repository = ProjectRepository(tmp_path)
    service = ProjectService(
        repository,
        ArtifactRepository(tmp_path),
        clock=lambda: NOW,
        project_id_factory=lambda: "prj_test",
    )
    monkeypatch.setattr(
        projects_module,
        "atomic_replace_bytes",
        lambda path, content: (_ for _ in ()).throw(OSError("write interrupted")),
    )

    with pytest.raises(ServiceError) as error:
        service.create_project("测试")

    assert error.value.code == "project_record_write_failed"
    assert not (tmp_path / "projects" / "prj_test").exists()
    assert repository.list_records() == []


def test_material_record_commit_failure_cleans_unreferenced_promotions(
    tmp_path, monkeypatch
) -> None:
    from flyboard.repositories.projects import ProjectMutation

    repository = ProjectRepository(tmp_path)
    ProjectService(
        repository,
        ArtifactRepository(tmp_path),
        clock=lambda: NOW,
        project_id_factory=lambda: "prj_test",
    ).create_project("测试")
    staging = make_staging_result(tmp_path, ["a.pdf", "b.pdf"])
    service = MaterialService(
        repository,
        StubStager(staging),
        clock=lambda: NOW,
        request_id_factory=lambda: "req_1",
        material_id_factory=iter(["mat_1", "mat_2"]).__next__,
        recover_on_startup=False,
    )

    def fail_commit(self, next_record, *, expected_latest_run_id="not_checked"):
        raise ServiceError("project_record_write_failed")

    monkeypatch.setattr(ProjectMutation, "commit", fail_commit)

    with pytest.raises(ServiceError) as error:
        service.register_materials("prj_test", "unused")

    assert error.value.code == "project_record_write_failed"
    assert repository.get("prj_test").materials == []
    sources = tmp_path / "projects" / "prj_test" / "sources"
    assert not sources.exists() or list(sources.iterdir()) == []
    assert not staging.request_root.exists()


def test_project_delete_cleanup_failure_returns_committed_pending_result(
    tmp_path, monkeypatch
) -> None:
    repository = ProjectRepository(tmp_path)
    service = ProjectService(
        repository,
        ArtifactRepository(tmp_path),
        clock=lambda: NOW,
        project_id_factory=lambda: "prj_test",
    )
    service.create_project("测试")
    monkeypatch.setattr(repository, "cleanup_deleted_project", lambda project_id: False)

    result = service.delete_project("prj_test", True)

    assert result.status == "deleted"
    assert result.artifact_cleanup.status == "pending"
    with pytest.raises(ServiceError) as error:
        repository.get("prj_test")
    assert error.value.code == "project_not_found"
    assert (tmp_path / "deletions" / "pending" / "prj_test.json").is_file()


def test_material_startup_cleanup_does_not_touch_retained_project(tmp_path) -> None:
    repository = ProjectRepository(tmp_path)
    artifacts = ArtifactRepository(tmp_path)
    projects = ProjectService(
        repository,
        artifacts,
        clock=lambda: NOW,
        project_id_factory=lambda: "prj_retained",
    )
    projects.create_project("保留项目")
    retained_source = tmp_path / "projects" / "prj_retained" / "sources" / "mat_retained"
    retained_source.mkdir(parents=True)
    (retained_source / "input.bin").write_bytes(b"retained")
    projects.delete_project("prj_retained", False)

    MaterialService(repository, MaterialStager(tmp_path))

    assert (retained_source / "input.bin").read_bytes() == b"retained"
    assert (tmp_path / "deletions" / "retained" / "prj_retained.json").is_file()
