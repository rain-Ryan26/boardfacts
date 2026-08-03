from __future__ import annotations

import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

from pydantic import ValidationError

from flyboard.errors import ServiceError
from flyboard.models import ProjectMaterial, ProjectRecord, RunInputMaterial
from flyboard.repositories._filesystem import (
    atomic_replace_bytes,
    ensure_no_reparse_points,
    json_bytes,
    managed_child,
    read_json_object,
    validate_identifier,
)
from flyboard.repositories._sources import resolve_material_source


class _ProjectLockRegistry:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def get(self, project_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(project_id, threading.RLock())


class ProjectMutation:
    def __init__(self, repository: ProjectRepository, project_id: str) -> None:
        self._repository = repository
        self._project_id = project_id
        self._current: ProjectRecord | None = None

    def get_current(self) -> ProjectRecord:
        record = self._repository._read_active(self._project_id)
        self._current = record
        return record.model_copy(deep=True)

    def create(self, initial_record: ProjectRecord) -> None:
        try:
            canonical = ProjectRecord.model_validate(initial_record.model_dump(mode="json"))
        except (AttributeError, ValidationError) as exc:
            raise ServiceError("project_record_invalid") from exc
        if canonical.project_id != self._project_id:
            raise ServiceError("project_identity_mismatch")
        project_root = self._repository._project_root(self._project_id)
        record_path = project_root / "project.json"
        if (
            project_root.exists()
            or self._repository._pending_receipt(self._project_id).exists()
            or self._repository._retained_receipt(self._project_id).exists()
        ):
            raise ServiceError("project_already_exists")
        created_root = False
        try:
            project_root.mkdir(parents=False, exist_ok=False)
            created_root = True
            atomic_replace_bytes(record_path, json_bytes(canonical))
            self._current = canonical.model_copy(deep=True)
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("project_record_write_failed") from exc
        finally:
            if created_root and not record_path.exists():
                try:
                    project_root.rmdir()
                except OSError:
                    pass

    def commit(
        self,
        next_record: ProjectRecord,
        *,
        expected_latest_run_id: str | None | Literal["not_checked"] = "not_checked",
    ) -> None:
        if self._current is None:
            raise ServiceError("project_mutation_not_loaded")
        try:
            canonical = ProjectRecord.model_validate(next_record.model_dump(mode="json"))
        except (AttributeError, ValidationError) as exc:
            raise ServiceError("project_record_invalid") from exc
        current = self._repository._read_active(self._project_id)
        if canonical.project_id != current.project_id:
            raise ServiceError("project_identity_mismatch")
        if (
            expected_latest_run_id != "not_checked"
            and current.latest_run_id != expected_latest_run_id
        ):
            raise ServiceError("project_latest_run_conflict")
        try:
            atomic_replace_bytes(
                self._repository._record_path(self._project_id), json_bytes(canonical)
            )
        except Exception as exc:
            raise ServiceError("project_record_write_failed") from exc
        self._current = canonical.model_copy(deep=True)

    def remove(self, *, cleanup_mode: Literal["retain", "delete"]) -> ProjectRecord:
        record = self._repository._read_active(self._project_id)
        receipt = (
            self._repository._retained_receipt(self._project_id)
            if cleanup_mode == "retain"
            else self._repository._pending_receipt(self._project_id)
        )
        if receipt.exists():
            raise ServiceError("project_deletion_conflict")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(self._repository._record_path(self._project_id), receipt)
        except OSError as exc:
            raise ServiceError("project_delete_failed") from exc
        self._current = None
        return record.model_copy(deep=True)


class ProjectRepository:
    """Single-process owner of canonical project records and project-keyed locks."""

    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root
        self.storage_root.mkdir(parents=True, exist_ok=True)
        ensure_no_reparse_points(self.storage_root, self.storage_root)
        self.projects_root = self.storage_root / "projects"
        self.pending_root = self.storage_root / "deletions" / "pending"
        self.retained_root = self.storage_root / "deletions" / "retained"
        for root in (self.projects_root, self.pending_root, self.retained_root):
            root.mkdir(parents=True, exist_ok=True)
            ensure_no_reparse_points(self.storage_root, root)
        self._locks = _ProjectLockRegistry()

    def _project_root(self, project_id: str) -> Path:
        return managed_child(self.projects_root, validate_identifier(project_id))

    def _record_path(self, project_id: str) -> Path:
        return self._project_root(project_id) / "project.json"

    def _pending_receipt(self, project_id: str) -> Path:
        return managed_child(self.pending_root, f"{validate_identifier(project_id)}.json")

    def _retained_receipt(self, project_id: str) -> Path:
        return managed_child(self.retained_root, f"{validate_identifier(project_id)}.json")

    def _read_active(self, project_id: str) -> ProjectRecord:
        record_path = self._record_path(project_id)
        ensure_no_reparse_points(self.projects_root, record_path)
        data = read_json_object(
            record_path,
            missing_code="project_not_found",
            invalid_code="project_record_invalid",
        )
        try:
            record = ProjectRecord.model_validate(data)
        except ValidationError as exc:
            raise ServiceError("project_record_invalid") from exc
        if record.project_id != project_id:
            raise ServiceError("project_record_invalid")
        return record

    def resolve_material_source(
        self,
        project_id: str,
        material: ProjectMaterial | RunInputMaterial,
    ) -> Path:
        return resolve_material_source(self._project_root(project_id), material)

    def get(self, project_id: str) -> ProjectRecord:
        lock = self._locks.get(validate_identifier(project_id))
        with lock:
            return self._read_active(project_id).model_copy(deep=True)

    def list_records(self) -> list[ProjectRecord]:
        records: list[ProjectRecord] = []
        try:
            project_ids = sorted(
                item.name for item in self.projects_root.iterdir() if item.is_dir()
            )
        except OSError as exc:
            raise ServiceError("project_list_failed") from exc
        for project_id in project_ids:
            try:
                validate_identifier(project_id)
            except ServiceError:
                continue
            lock = self._locks.get(project_id)
            with lock:
                if not self._record_path(project_id).is_file():
                    continue
                records.append(self._read_active(project_id).model_copy(deep=True))
        return records

    @contextmanager
    def mutate(self, project_id: str) -> Iterator[ProjectMutation]:
        project_id = validate_identifier(project_id)
        lock = self._locks.get(project_id)
        with lock:
            self._project_root(project_id)
            self._pending_receipt(project_id)
            self._retained_receipt(project_id)
            yield ProjectMutation(self, project_id)

    @contextmanager
    def guard_product_run(self, project_id: str, run_id: str) -> Iterator[ProjectRecord]:
        project_id = validate_identifier(project_id)
        validate_identifier(run_id)
        lock = self._locks.get(project_id)
        with lock:
            record = self._read_active(project_id)
            if record.latest_run_id != run_id:
                raise ServiceError("run_not_found")
            yield record.model_copy(deep=True)

    def retry_pending_deletions(self) -> list[str]:
        """Retry every validated pending deletion receipt and return failures."""
        try:
            receipt_names = sorted(
                item.name for item in self.pending_root.iterdir() if item.is_file()
            )
        except OSError as exc:
            raise ServiceError("project_deletion_scan_failed") from exc
        failed: list[str] = []
        for name in receipt_names:
            if not name.endswith(".json"):
                continue
            project_id = name[:-5]
            try:
                validate_identifier(project_id)
            except ServiceError:
                continue
            if not self.cleanup_deleted_project(project_id):
                failed.append(project_id)
        return failed

    def cleanup_deleted_project(self, project_id: str) -> bool:
        """Best-effort pending deletion cleanup; false keeps the retry receipt."""
        project_id = validate_identifier(project_id)
        lock = self._locks.get(project_id)
        with lock:
            try:
                receipt = self._pending_receipt(project_id)
                if not receipt.is_file():
                    return True
                root = self._project_root(project_id)
                if root.exists():
                    self._assert_tree_has_no_reparse_points(root)
                    shutil.rmtree(root)
                receipt.unlink()
                return True
            except (OSError, ServiceError):
                return False

    def _assert_tree_has_no_reparse_points(self, root: Path) -> None:
        ensure_no_reparse_points(self.storage_root, root)
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            ensure_no_reparse_points(root, current_path)
            for name in (*directories, *files):
                ensure_no_reparse_points(root, current_path / name)
