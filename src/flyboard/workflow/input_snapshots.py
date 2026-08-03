from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from flyboard.errors import ServiceError
from flyboard.ingestion import material_sha256_from_source
from flyboard.models import (
    FieldTable,
    MaterialKind,
    NoRuleSet,
    ProjectRecord,
    RuleSetSnapshot,
    RunInputGroups,
    RunInputMaterial,
    RunInputSnapshot,
    SelectedRuleSet,
    build_current_field_table,
)
from flyboard.repositories import ProjectRepository
from flyboard.services.rule_sets import RuleSetService


@dataclass(frozen=True, slots=True)
class _PreparedRunInputs:
    project_id: str
    materials: tuple[RunInputMaterial, ...]
    groups: RunInputGroups
    field_table: FieldTable
    rule_set_snapshot: RuleSetSnapshot | None


class _InputSnapshotBuilder:
    """Batch-B preflight and construction; it never allocates or persists a run."""

    def __init__(
        self,
        repository: ProjectRepository,
        rule_sets: RuleSetService,
        *,
        field_table_factory: Callable[[], FieldTable] = build_current_field_table,
    ) -> None:
        self.repository = repository
        self.rule_sets = rule_sets
        self.field_table_factory = field_table_factory

    def prepare(
        self,
        project_record: ProjectRecord,
        material_ids: list[str],
        rule_set_id: str | None,
    ) -> _PreparedRunInputs:
        if (
            not isinstance(material_ids, list)
            or not material_ids
            or any(not isinstance(item, str) or not item for item in material_ids)
        ):
            raise ServiceError("invalid_material_selection")
        if len(material_ids) != len(set(material_ids)):
            raise ServiceError("duplicate_material_id")
        try:
            candidate_field_table = self.field_table_factory()
            field_table = FieldTable.model_validate(candidate_field_table.model_dump(mode="json"))
        except Exception as exc:
            raise ServiceError("field_table_invalid") from exc
        by_id = {material.material_id: material for material in project_record.materials}
        if any(material_id not in by_id for material_id in material_ids):
            raise ServiceError("material_not_found")
        selected = sorted(
            (by_id[material_id] for material_id in material_ids),
            key=lambda material: material.registered_order,
        )
        run_materials: list[RunInputMaterial] = []
        for order, material in enumerate(selected, start=1):
            try:
                source = self.repository.resolve_material_source(
                    project_record.project_id, material
                )
            except (OSError, ServiceError) as exc:
                raise ServiceError("material_source_unreadable") from exc
            try:
                digest = material_sha256_from_source(source, material)
            except ServiceError as exc:
                raise ServiceError("material_source_unreadable") from exc
            except PermissionError as exc:
                raise ServiceError("material_source_unreadable") from exc
            except OSError as exc:
                raise ServiceError("material_sha256_failed") from exc
            values: dict[str, object] = {
                "material_id": material.material_id,
                "material_order": order,
                "kind": material.kind,
                "source_ref": material.source_ref,
                "sha256": digest,
                "registered_at": material.registered_at,
            }
            if material.member_kinds is not None:
                values["member_kinds"] = list(material.member_kinds)
            if material.unsupported_reason_code is not None:
                values["unsupported_reason_code"] = material.unsupported_reason_code
            run_materials.append(RunInputMaterial.model_validate(values))

        rule_snapshot = (
            self.rule_sets.prepare_snapshot(rule_set_id, field_table)
            if rule_set_id is not None
            else None
        )
        groups = RunInputGroups(
            pdf_inputs=self._ids(run_materials, MaterialKind.PDF),
            image_inputs=self._ids(run_materials, MaterialKind.IMAGE),
            spreadsheet_inputs=self._ids(run_materials, MaterialKind.SPREADSHEET),
            gerber_jobs=self._ids(run_materials, MaterialKind.GERBER_JOB),
            unsupported_materials=self._ids(run_materials, MaterialKind.UNSUPPORTED),
        )
        return _PreparedRunInputs(
            project_id=project_record.project_id,
            materials=tuple(item.model_copy(deep=True) for item in run_materials),
            groups=groups.model_copy(deep=True),
            field_table=field_table.model_copy(deep=True),
            rule_set_snapshot=(
                rule_snapshot.model_copy(deep=True) if rule_snapshot is not None else None
            ),
        )

    @staticmethod
    def build_snapshot(prepared: _PreparedRunInputs, run_id: str) -> RunInputSnapshot:
        if prepared.rule_set_snapshot is None:
            selection = NoRuleSet(mode="not_selected", reason="rule_set_not_selected")
        else:
            snapshot = prepared.rule_set_snapshot
            selection = SelectedRuleSet(
                mode="selected",
                rule_set_id=snapshot.rule_set_id,
                rule_set_name=snapshot.rule_set_name,
                rule_set_version=snapshot.rule_set_version,
                schema_version=snapshot.schema_version,
                source_kind=snapshot.source_kind,
                source_path=snapshot.source_path,
                content_sha256=snapshot.content_sha256,
                snapshot_artifact="rules/rule_set_snapshot.json",
            )
        return RunInputSnapshot(
            schema_version="1.0",
            project_id=prepared.project_id,
            run_id=run_id,
            snapshot_id=f"{run_id}_manifest",
            materials=[item.model_copy(deep=True) for item in prepared.materials],
            groups=prepared.groups.model_copy(deep=True),
            field_table=prepared.field_table.model_copy(deep=True),
            rule_set=selection,
        )

    @staticmethod
    def _ids(materials: list[RunInputMaterial], kind: MaterialKind) -> list[str]:
        return [material.material_id for material in materials if material.kind == kind]
