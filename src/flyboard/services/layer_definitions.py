from __future__ import annotations

from datetime import datetime

from flyboard.errors import ServiceError
from flyboard.models import (
    GerberJobPreparation,
    LayerDefinition,
    LayerDefinitionItem,
)


class LayerDefinitionService:
    """Pure validator and constructor for run-scoped Gerber layer definitions."""

    @staticmethod
    def _validate_identity(
        preparation: GerberJobPreparation,
        current_definition: LayerDefinition | None,
    ) -> None:
        if current_definition is None:
            return
        if (
            current_definition.project_id != preparation.project_id
            or current_definition.run_id != preparation.run_id
            or current_definition.job_material_id != preparation.job_material_id
        ):
            raise ServiceError("run_state_conflict")

    @staticmethod
    def _canonical_layers(
        preparation: GerberJobPreparation,
        submitted_layers: list[LayerDefinitionItem],
    ) -> list[LayerDefinitionItem]:
        if not isinstance(submitted_layers, list):
            raise ServiceError("run_state_conflict")
        by_id = {item.layer_id: item for item in submitted_layers}
        if len(by_id) != len(submitted_layers):
            raise ServiceError("run_state_conflict")
        expected_ids = [item.layer_id for item in preparation.layer_inputs]
        if set(by_id) != set(expected_ids):
            raise ServiceError("run_state_conflict")

        canonical: list[LayerDefinitionItem] = []
        for layer_input in preparation.layer_inputs:
            submitted = by_id[layer_input.layer_id]
            if submitted.file_path != layer_input.file_path:
                raise ServiceError("run_state_conflict")
            canonical.append(submitted.model_copy(deep=True))
        return canonical

    def build_draft(
        self,
        preparation: GerberJobPreparation,
        current_definition: LayerDefinition | None,
        submitted_layers: list[LayerDefinitionItem],
        updated_at: datetime,
    ) -> LayerDefinition:
        self._validate_identity(preparation, current_definition)
        if current_definition is not None and current_definition.confirmed_at is not None:
            raise ServiceError("run_state_conflict")
        canonical_layers = self._canonical_layers(preparation, submitted_layers)
        try:
            return LayerDefinition(
                project_id=preparation.project_id,
                run_id=preparation.run_id,
                job_material_id=preparation.job_material_id,
                layers=canonical_layers,
                updated_at=updated_at,
                confirmed_at=None,
            )
        except (TypeError, ValueError) as exc:
            raise ServiceError("run_state_conflict") from exc

    def build_confirmed(
        self,
        preparation: GerberJobPreparation,
        current_definition: LayerDefinition,
        confirmed_at: datetime,
    ) -> LayerDefinition:
        self._validate_identity(preparation, current_definition)
        if current_definition.confirmed_at is not None:
            raise ServiceError("run_state_conflict")
        canonical_layers = self._canonical_layers(preparation, list(current_definition.layers))
        try:
            return LayerDefinition(
                project_id=current_definition.project_id,
                run_id=current_definition.run_id,
                job_material_id=current_definition.job_material_id,
                layers=canonical_layers,
                updated_at=current_definition.updated_at,
                confirmed_at=confirmed_at,
            )
        except (TypeError, ValueError) as exc:
            raise ServiceError("run_state_conflict") from exc
