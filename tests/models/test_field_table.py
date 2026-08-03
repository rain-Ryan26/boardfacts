from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from flyboard.models import FieldCategory, ProducerScope
from flyboard.models import field_table as field_table_module
from flyboard.models.field_table import build_current_field_table

EXPECTED_FIELD_CODES = (
    "project_code",
    "project_name",
    "project_instructions",
    "project_analysis_findings",
    "drill_file_presence",
    "gerber_job_presence",
    "spreadsheet_presence",
    "pdf_presence",
    "layer_count",
    "base_material",
    "board_thickness_mm",
    "outer_copper_thickness",
    "inner_copper_thickness",
    "board_size_mm",
    "board_length_mm",
    "board_width_mm",
    "panel_size_mm",
    "usable_area_mm",
    "gerber_bounds_mm",
    "panelization_summary",
    "mechanical_process",
    "fiducial_mark_requirement",
    "break_point_requirement",
    "min_trace_width_mm",
    "min_trace_spacing_mm",
    "min_finished_hole_size",
    "drill_size_list",
    "drill_quantity_list",
    "slot_size_list",
    "plated_hole_requirement",
    "non_plated_hole_requirement",
    "via_type_summary",
    "blind_vias",
    "buried_vias",
    "min_hole_diameter_mm",
    "min_annular_ring_mm",
    "surface_finish",
    "solder_mask_layers",
    "solder_mask_color",
    "silkscreen_layers",
    "silkscreen_color",
    "ipc_class",
    "rohs",
    "ul_flame_class",
    "ul_max_operating_temperature",
    "unknown_note_###",
)

EXPECTED_CORE_CODES = {
    "layer_count",
    "board_length_mm",
    "board_width_mm",
    "base_material",
    "surface_finish",
}

EXPECTED_INTEGRATION_ONLY_CODES = {
    "project_analysis_findings",
    "drill_file_presence",
    "gerber_job_presence",
    "spreadsheet_presence",
    "pdf_presence",
    "unknown_note_###",
}

EXPECTED_MM_CODES = {
    "board_thickness_mm",
    "board_size_mm",
    "board_length_mm",
    "board_width_mm",
    "panel_size_mm",
    "usable_area_mm",
    "gerber_bounds_mm",
    "min_trace_width_mm",
    "min_trace_spacing_mm",
    "min_finished_hole_size",
    "drill_size_list",
    "slot_size_list",
    "min_hole_diameter_mm",
    "min_annular_ring_mm",
}

EXPECTED_MICROMETRE_CODES = {
    "outer_copper_thickness",
    "inner_copper_thickness",
}

EXPECTED_CONTENT_SHA256 = "06e8349a0d1f45a1ff57c52ac68f12225bb25b8defb2c6c7eee80c1adffaecba"


def _canonical_json_bytes(table: object) -> bytes:
    schema_version = table.schema_version  # type: ignore[attr-defined]
    fields = table.fields  # type: ignore[attr-defined]
    return json.dumps(
        {
            "schema_version": schema_version,
            "fields": [field.model_dump(mode="json") for field in fields],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def test_current_field_table_has_the_formal_order_and_metadata() -> None:
    table = build_current_field_table()

    assert table.schema_version == "1.0"
    assert len(table.fields) == 46
    assert tuple(field.field_code for field in table.fields) == EXPECTED_FIELD_CODES
    assert {field.field_code for field in table.fields if field.is_core} == EXPECTED_CORE_CODES
    assert {
        field.field_code for field in table.fields if field.producer_scope == "integration_only"
    } == EXPECTED_INTEGRATION_ONLY_CODES
    assert {field.field_code for field in table.fields if field.unit == "mm"} == EXPECTED_MM_CODES
    assert {
        field.field_code for field in table.fields if field.unit == "µm"
    } == EXPECTED_MICROMETRE_CODES
    assert all(field.unit in {None, "mm", "µm"} for field in table.fields)


def test_current_field_table_has_one_unknown_note_template() -> None:
    table = build_current_field_table()

    templates = [field for field in table.fields if "#" in field.field_code]

    assert len(templates) == 1
    assert templates[0].field_code == "unknown_note_###"
    assert templates[0].field_name == "未知说明"
    assert templates[0].category == "板件设计参数"
    assert templates[0].producer_scope == "integration_only"


def test_current_field_table_is_deterministic() -> None:
    first = build_current_field_table()
    second = build_current_field_table()

    assert first == second
    assert first is not second
    assert first.fields is not second.fields
    assert _canonical_json_bytes(first) == _canonical_json_bytes(second)
    assert first.content_sha256 == second.content_sha256


def test_current_field_table_matches_the_canonical_fixture() -> None:
    table = build_current_field_table()
    canonical_bytes = _canonical_json_bytes(table)
    fixture_path = Path(__file__).parents[1] / "fixtures" / "field_table_v1.json"

    assert canonical_bytes == fixture_path.read_bytes()
    assert hashlib.sha256(canonical_bytes).hexdigest() == EXPECTED_CONTENT_SHA256
    assert table.content_sha256 == EXPECTED_CONTENT_SHA256


@pytest.mark.parametrize(
    ("field_index", "update"),
    [
        (0, {"field_code": "project_code_changed"}),
        (0, {"field_name": "项目编码（变更）"}),
        (0, {"category": FieldCategory.BOARD}),
        (0, {"definition": "变更后的中文字段定义。"}),
        (0, {"unit": "mm"}),
        (0, {"is_core": True}),
        (0, {"producer_scope": ProducerScope.INTEGRATION_ONLY}),
    ],
)
def test_content_hash_changes_when_field_metadata_changes(
    field_index: int,
    update: dict[str, object],
) -> None:
    table = build_current_field_table()
    fields = list(table.fields)
    fields[field_index] = fields[field_index].model_copy(update=update)

    assert (
        field_table_module._compute_content_sha256(table.schema_version, fields)
        != table.content_sha256
    )


def test_content_hash_changes_when_field_order_or_membership_changes() -> None:
    table = build_current_field_table()
    reordered = list(table.fields)
    reordered[0], reordered[1] = reordered[1], reordered[0]

    assert (
        field_table_module._compute_content_sha256(table.schema_version, reordered)
        != table.content_sha256
    )
    assert (
        field_table_module._compute_content_sha256(table.schema_version, table.fields[:-1])
        != table.content_sha256
    )
    assert (
        field_table_module._compute_content_sha256(
            table.schema_version,
            [*table.fields, table.fields[-1]],
        )
        != table.content_sha256
    )
