from __future__ import annotations

from datetime import datetime, timezone

import pytest

from flyboard.models import ProjectMaterial, ProjectRecord
from flyboard.repositories import ProjectRepository
from flyboard.services import RuleSetService, ServiceError
from flyboard.workflow.input_snapshots import _InputSnapshotBuilder

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def setup_project(tmp_path) -> tuple[ProjectRepository, ProjectRecord]:
    repository = ProjectRepository(tmp_path)
    source = tmp_path / "projects" / "prj_test" / "sources" / "mat_1" / "input.pdf"
    material = ProjectMaterial(
        material_id="mat_1",
        kind="pdf",
        source_ref="sources/mat_1/input.pdf",
        display_name="input.pdf",
        registered_order=1,
        registered_at=NOW,
    )
    record = ProjectRecord(
        project_id="prj_test",
        project_name="测试",
        created_at=NOW,
        updated_at=NOW,
        materials=[material],
        latest_run_id=None,
    )
    with repository.mutate("prj_test") as mutation:
        mutation.create(record)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable pdf bytes")
    return repository, record


def test_snapshot_preflight_builds_stable_groups_without_persisting_run(tmp_path) -> None:
    repository, record = setup_project(tmp_path)
    service = _InputSnapshotBuilder(repository, RuleSetService(tmp_path))
    prepared = service.prepare(record, ["mat_1"], None)
    snapshot = service.build_snapshot(prepared, "run_1")

    assert snapshot.groups.pdf_inputs == ["mat_1"]
    assert snapshot.materials[0].material_order == 1
    assert snapshot.rule_set.mode == "not_selected"
    assert not (tmp_path / "projects" / "prj_test" / "runs").exists()


def test_snapshot_validation_error_order_is_stable(tmp_path) -> None:
    repository, record = setup_project(tmp_path)
    service = _InputSnapshotBuilder(repository, RuleSetService(tmp_path))
    with pytest.raises(ServiceError) as error:
        service.prepare(record, [], None)
    assert error.value.code == "invalid_material_selection"
    with pytest.raises(ServiceError) as error:
        service.prepare(record, ["mat_1", "mat_1"], None)
    assert error.value.code == "duplicate_material_id"
    with pytest.raises(ServiceError) as error:
        service.prepare(record, ["missing"], None)
    assert error.value.code == "material_not_found"


def test_field_table_is_built_once_before_source_resolution(tmp_path, monkeypatch) -> None:
    from flyboard.models import build_current_field_table

    repository, record = setup_project(tmp_path)
    calls: list[str] = []
    original_resolve = repository.resolve_material_source

    def field_table_factory():
        calls.append("field_table")
        return build_current_field_table()

    def resolve_source(project_id, material):
        calls.append("source")
        return original_resolve(project_id, material)

    monkeypatch.setattr(repository, "resolve_material_source", resolve_source)
    builder = _InputSnapshotBuilder(
        repository,
        RuleSetService(tmp_path),
        field_table_factory=field_table_factory,
    )
    builder.prepare(record, ["mat_1"], None)

    assert calls == ["field_table", "source"]
