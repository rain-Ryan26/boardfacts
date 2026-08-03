from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from flyboard.errors import ServiceError
from flyboard.ingestion import (
    MaterialStager,
    MaterialUploadStager,
    StagingResult,
    UploadedFileSource,
    material_fingerprint,
)
from flyboard.models import (
    MaterialRegistrationFailure,
    MaterialRegistrationResult,
    ProjectMaterial,
    ProjectRecord,
    RegistrationFailureReason,
)
from flyboard.repositories import ProjectRepository
from flyboard.repositories._filesystem import ensure_no_reparse_points, managed_child
from flyboard.services._support import new_id, utc_now


class MaterialService:
    def __init__(
        self,
        repository: ProjectRepository,
        stager: MaterialStager,
        *,
        upload_stager: MaterialUploadStager | None = None,
        clock: Callable[[], datetime] = utc_now,
        request_id_factory: Callable[[], str] = lambda: new_id("req"),
        material_id_factory: Callable[[], str] = lambda: new_id("mat"),
        recover_on_startup: bool = True,
    ) -> None:
        self.repository = repository
        self.stager = stager
        self.upload_stager = upload_stager or MaterialUploadStager(stager)
        self.clock = clock
        self.request_id_factory = request_id_factory
        self.material_id_factory = material_id_factory
        if recover_on_startup:
            self.recover_stale_storage()

    def recover_stale_storage(self) -> None:
        """Startup-only cleanup of abandoned staging and active-project source orphans."""
        try:
            for child in list(self.stager.staging_root.iterdir()):
                self._remove_tree_safely(self.stager.storage_root, child)
            for listed in self.repository.list_records():
                with self.repository.mutate(listed.project_id) as mutation:
                    current = mutation.get_current()
                    referenced = {material.material_id for material in current.materials}
                    sources = self.repository._project_root(listed.project_id) / "sources"
                    ensure_no_reparse_points(self.repository.storage_root, sources)
                    if not sources.exists():
                        continue
                    for child in list(sources.iterdir()):
                        if child.name not in referenced:
                            self._remove_tree_safely(self.repository.storage_root, child)
        except (OSError, ServiceError) as exc:
            raise ServiceError("material_storage_recovery_failed") from exc

    @staticmethod
    def _remove_tree_safely(storage_root: Path, target: Path) -> None:
        ensure_no_reparse_points(storage_root, target)
        if target.is_dir():
            for current, directories, files in os.walk(target, followlinks=False):
                current_path = Path(current)
                ensure_no_reparse_points(storage_root, current_path)
                for name in (*directories, *files):
                    ensure_no_reparse_points(storage_root, current_path / name)
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

    @classmethod
    def _best_effort_remove(cls, storage_root: Path, target: Path) -> None:
        try:
            cls._remove_tree_safely(storage_root, target)
        except (OSError, ServiceError):
            pass

    def _allocate_material_target(
        self, project_root: Path, used_material_ids: set[str]
    ) -> tuple[str, Path]:
        for _ in range(100):
            material_id = self.material_id_factory()
            if material_id in used_material_ids:
                continue
            target = managed_child(project_root, "sources", material_id)
            if not target.exists():
                return material_id, target
        raise ServiceError("material_id_allocation_failed")

    def register_materials(self, project_id: str, input_path: str) -> MaterialRegistrationResult:
        staging = self.stager.stage(project_id, self.request_id_factory(), Path(input_path))
        return self._register_staging(project_id, staging)

    def register_uploaded_materials(
        self,
        project_id: str,
        input_mode: str,
        files: Iterable[UploadedFileSource],
    ) -> MaterialRegistrationResult:
        staging = self.upload_stager.stage(
            project_id,
            self.request_id_factory(),
            input_mode,
            files,
        )
        return self._register_staging(project_id, staging)

    def _register_staging(
        self, project_id: str, staging: StagingResult
    ) -> MaterialRegistrationResult:
        failures = [
            MaterialRegistrationFailure(
                input_ref=item.input_ref,
                reason_code=item.reason,
            )
            for item in staging.failures
        ]
        promoted: list[Path] = []
        try:
            with self.repository.mutate(project_id) as mutation:
                current = mutation.get_current()
                project_root = self.repository._project_root(project_id)
                existing_by_fingerprint: dict[str, ProjectMaterial] = {}
                try:
                    for material in current.materials:
                        existing_by_fingerprint[material_fingerprint(project_root, material)] = (
                            material
                        )
                except (OSError, ServiceError) as exc:
                    raise ServiceError("material_source_unreadable") from exc

                next_materials = [item.model_copy(deep=True) for item in current.materials]
                next_order = (
                    max((material.registered_order for material in next_materials), default=0) + 1
                )
                registered_at = self.clock()
                used_material_ids = {material.material_id for material in next_materials}
                for unit in staging.units:
                    if unit.fingerprint in existing_by_fingerprint:
                        self._best_effort_remove(self.repository.storage_root, unit.staging_root)
                        continue
                    material_id, target = self._allocate_material_target(
                        project_root, used_material_ids
                    )
                    used_material_ids.add(material_id)
                    try:
                        ensure_no_reparse_points(project_root, target)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        ensure_no_reparse_points(project_root, target.parent)
                        os.replace(unit.staging_root, target)
                    except OSError:
                        failures.append(
                            MaterialRegistrationFailure(
                                input_ref=unit.input_ref,
                                reason_code=RegistrationFailureReason.SOURCE_COPY_FAILED,
                            )
                        )
                        continue
                    promoted.append(target)
                    source_ref = (
                        f"sources/{material_id}"
                        if unit.directory_source
                        else f"sources/{material_id}/{unit.display_name}"
                    )
                    values: dict[str, object] = {
                        "material_id": material_id,
                        "kind": unit.kind,
                        "source_ref": source_ref,
                        "display_name": unit.display_name,
                        "registered_order": next_order,
                        "registered_at": registered_at,
                    }
                    if unit.member_kinds:
                        values["member_kinds"] = list(unit.member_kinds)
                    if unit.unsupported_reason is not None:
                        values["unsupported_reason_code"] = unit.unsupported_reason
                    try:
                        material = ProjectMaterial.model_validate(values)
                    except ValidationError as exc:
                        raise ServiceError("classification_failed") from exc
                    next_materials.append(material)
                    existing_by_fingerprint[unit.fingerprint] = material
                    next_order += 1

                if len(next_materials) != len(current.materials):
                    next_record = ProjectRecord(
                        project_id=current.project_id,
                        project_name=current.project_name,
                        created_at=current.created_at,
                        updated_at=registered_at,
                        materials=next_materials,
                        latest_run_id=current.latest_run_id,
                    )
                    mutation.commit(next_record=next_record)
                    result_materials = next_record.materials
                else:
                    result_materials = current.materials
            promoted.clear()
            failures.sort(key=lambda item: (item.input_ref != ".", item.input_ref))
            return MaterialRegistrationResult(
                materials=[item.model_copy(deep=True) for item in result_materials],
                failures=failures,
            )
        except Exception:
            for target in promoted:
                self._best_effort_remove(self.repository.storage_root, target)
            raise
        finally:
            self._best_effort_remove(self.repository.storage_root, staging.request_root)
