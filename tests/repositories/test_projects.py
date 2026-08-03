from __future__ import annotations

from datetime import datetime, timezone

import pytest

from flyboard.models import ProjectRecord
from flyboard.repositories import ProjectRepository
from flyboard.services import ServiceError

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def record(project_id: str = "prj_test", latest: str | None = None) -> ProjectRecord:
    return ProjectRecord(
        project_id=project_id,
        project_name="测试项目",
        created_at=NOW,
        updated_at=NOW,
        materials=[],
        latest_run_id=latest,
    )


def test_project_mutation_create_commit_cas_guard_and_remove(tmp_path) -> None:
    repository = ProjectRepository(tmp_path)
    with repository.mutate("prj_test") as mutation:
        mutation.create(record())
    assert repository.get("prj_test").project_name == "测试项目"
    assert [item.project_id for item in repository.list_records()] == ["prj_test"]

    with repository.mutate("prj_test") as mutation:
        current = mutation.get_current()
        mutation.commit(
            current.model_copy(update={"latest_run_id": "run_1"}),
            expected_latest_run_id=None,
        )
    with repository.guard_product_run("prj_test", "run_1") as guarded:
        assert guarded.latest_run_id == "run_1"

    with repository.mutate("prj_test") as mutation:
        current = mutation.get_current()
        with pytest.raises(ServiceError) as error:
            mutation.commit(current, expected_latest_run_id=None)
    assert error.value.code == "project_latest_run_conflict"

    with repository.mutate("prj_test") as mutation:
        mutation.get_current()
        removed = mutation.remove(cleanup_mode="retain")
    assert removed.project_id == "prj_test"
    with pytest.raises(ServiceError) as error:
        repository.get("prj_test")
    assert error.value.code == "project_not_found"
    assert (tmp_path / "deletions" / "retained" / "prj_test.json").is_file()


def test_project_repository_rejects_path_identity_and_partial_create(tmp_path) -> None:
    repository = ProjectRepository(tmp_path)
    with pytest.raises(ServiceError) as error:
        repository.get("../escape")
    assert error.value.code == "invalid_managed_path"

    with repository.mutate("prj_test") as mutation:
        with pytest.raises(ServiceError) as error:
            mutation.create(record("other"))
    assert error.value.code == "project_identity_mismatch"
    assert not (tmp_path / "projects" / "prj_test" / "project.json").exists()


def test_project_record_reader_rejects_reparse_target(tmp_path) -> None:
    repository = ProjectRepository(tmp_path)
    with repository.mutate("prj_test") as mutation:
        mutation.create(record())
    record_path = tmp_path / "projects" / "prj_test" / "project.json"
    external = tmp_path / "external-project.json"
    external.write_bytes(record_path.read_bytes())
    record_path.unlink()
    try:
        record_path.symlink_to(external)
    except OSError:
        pytest.skip("file symlinks are unavailable on this Windows host")

    with pytest.raises(ServiceError) as error:
        repository.get("prj_test")
    assert error.value.code == "managed_path_reparse_point"


def test_pending_cleanup_path_failure_keeps_receipt_for_retry(tmp_path, monkeypatch) -> None:
    repository = ProjectRepository(tmp_path)
    with repository.mutate("prj_test") as mutation:
        mutation.create(record())
    with repository.mutate("prj_test") as mutation:
        mutation.get_current()
        mutation.remove(cleanup_mode="delete")

    receipt = tmp_path / "deletions" / "pending" / "prj_test.json"

    def reject_managed_root(project_id):
        raise ServiceError("managed_path_reparse_point")

    monkeypatch.setattr(repository, "_project_root", reject_managed_root)

    assert repository.cleanup_deleted_project("prj_test") is False
    assert repository.retry_pending_deletions() == ["prj_test"]
    assert receipt.is_file()
