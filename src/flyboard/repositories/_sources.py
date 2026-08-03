from __future__ import annotations

from pathlib import Path

from flyboard.errors import ServiceError
from flyboard.models import MaterialKind, ProjectMaterial, RunInputMaterial

from ._filesystem import ensure_no_reparse_points, managed_child


def resolve_material_source(
    project_root: Path,
    material: ProjectMaterial | RunInputMaterial,
) -> Path:
    """Resolve one canonical material locator inside its exact managed source root."""
    expected_root = managed_child(project_root, "sources", material.material_id)
    source = managed_child(project_root, material.source_ref, locator=True)
    try:
        source.relative_to(expected_root)
    except ValueError as exc:
        raise ServiceError("managed_path_escape") from exc
    ensure_no_reparse_points(project_root, source)
    try:
        if material.kind == MaterialKind.GERBER_JOB:
            if not source.is_dir():
                raise ServiceError("material_source_unreadable")
        elif not source.is_file():
            raise ServiceError("material_source_unreadable")
    except OSError as exc:
        raise ServiceError("material_source_unreadable") from exc
    return source
