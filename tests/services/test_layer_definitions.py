from __future__ import annotations

from datetime import datetime, timezone

import pytest

from flyboard.models import (
    GerberJobPreparation,
    LayerDefinitionItem,
)
from flyboard.services import LayerDefinitionService, ServiceError

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)
LATER = datetime(2026, 7, 18, 0, 1, tzinfo=timezone.utc)


def preparation() -> GerberJobPreparation:
    return GerberJobPreparation(
        schema_version="1.0",
        project_id="prj_test",
        run_id="run_1",
        job_material_id="mat_1",
        layer_inputs=[
            {
                "layer_id": "layer_top",
                "file_path": "top.gbr",
                "detected_type": "gerber",
            },
            {
                "layer_id": "layer_drill",
                "file_path": "holes.drl",
                "detected_type": "drill",
            },
        ],
    )


def submitted_layers() -> list[LayerDefinitionItem]:
    return [
        LayerDefinitionItem(
            layer_id="layer_drill",
            file_path="holes.drl",
            role="drill",
            copper_layer_index=None,
        ),
        LayerDefinitionItem(
            layer_id="layer_top",
            file_path="top.gbr",
            role="top_copper",
            copper_layer_index=1,
        ),
    ]


def test_layer_definition_service_builds_canonical_draft_and_confirmation() -> None:
    service = LayerDefinitionService()
    draft = service.build_draft(preparation(), None, submitted_layers(), NOW)

    assert [item.layer_id for item in draft.layers] == ["layer_top", "layer_drill"]
    assert draft.updated_at == NOW
    assert draft.confirmed_at is None

    confirmed = service.build_confirmed(preparation(), draft, LATER)
    assert confirmed.layers == draft.layers
    assert confirmed.updated_at == NOW
    assert confirmed.confirmed_at == LATER


@pytest.mark.parametrize(
    "layers",
    [
        [],
        [
            LayerDefinitionItem(
                layer_id="layer_top",
                file_path="wrong.gbr",
                role="top_copper",
                copper_layer_index=1,
            ),
            LayerDefinitionItem(
                layer_id="layer_drill",
                file_path="holes.drl",
                role="drill",
                copper_layer_index=None,
            ),
        ],
    ],
)
def test_layer_definition_service_rejects_incomplete_or_mismatched_layers(layers) -> None:
    with pytest.raises(ServiceError) as caught:
        LayerDefinitionService().build_draft(preparation(), None, layers, NOW)

    assert caught.value.code == "run_state_conflict"


def test_layer_definition_service_rejects_cross_job_current_definition() -> None:
    service = LayerDefinitionService()
    draft = service.build_draft(preparation(), None, submitted_layers(), NOW)
    wrong = draft.model_copy(update={"job_material_id": "mat_other"})

    with pytest.raises(ServiceError) as caught:
        service.build_confirmed(preparation(), wrong, LATER)

    assert caught.value.code == "run_state_conflict"
