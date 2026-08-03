from __future__ import annotations

import json

from flyboard.models import build_current_field_table
from flyboard.pdf_image.parser import PdfSubmissionParser


def test_parser_converts_qwen_grounding_region_to_source_pixels() -> None:
    content = json.dumps(
        {
            "schema_version": "1.0",
            "batch_id": "batch_1",
            "assets": [
                {
                    "asset_id": "asset_a",
                    "fields": [
                        {
                            "field_code": "board_thickness_mm",
                            "field_value": "1.6 mm",
                            "unit": "mm",
                            "branch_state": "clear_now",
                            "source_text": "板厚 1.6 mm",
                            "check_note": "来源内容形成字段候选。",
                            "region": {"bbox_2d": [100, 100, 500, 350]},
                        },
                        {
                            "field_code": "outer_copper_thickness",
                            "field_value": "70 µm",
                            "unit": "µm",
                            "branch_state": "clear_now",
                            "source_text": "Outer copper 70 µm",
                            "check_note": "来源内容形成字段候选。",
                            "region": None,
                        },
                        {
                            "field_code": "inner_copper_thickness",
                            "field_value": "18 µm",
                            "unit": "mm",
                            "branch_state": "clear_now",
                            "source_text": "Inner copper 18 µm",
                            "check_note": "来源内容形成字段候选。",
                            "region": None,
                        },
                        {
                            "field_code": "pdf_presence",
                            "field_value": "bad",
                            "unit": None,
                            "branch_state": "clear_now",
                            "source_text": None,
                            "check_note": "来源内容形成字段候选。",
                            "region": None,
                        },
                    ],
                    "unknown_notes": [],
                },
                {"asset_id": "unexpected", "fields": [], "unknown_notes": []},
            ],
        },
        ensure_ascii=False,
    )

    parsed = PdfSubmissionParser().parse(
        content,
        batch_id="batch_1",
        asset_dimensions={"asset_a": (100, 80), "asset_missing": (20, 20)},
        field_table=build_current_field_table(),
    )

    assert not parsed.envelope_rejected
    assert len(parsed.accepted_assets) == 1
    fields = parsed.accepted_assets[0].fields
    assert [item.field_code for item in fields] == [
        "board_thickness_mm",
        "outer_copper_thickness",
    ]
    assert fields[0].region.model_dump(mode="json") == {
        "x": 10,
        "y": 8,
        "width": 40,
        "height": 20,
        "unit": "px",
    }
    assert fields[1].field_value == "70 µm"
    assert fields[1].unit == "µm"
    assert parsed.region_degradations == ()
    assert [item.reason_code for item in parsed.rejections] == [
        "unit_mismatch",
        "invalid_value",
        "asset_missing",
        "asset_unexpected",
    ]


def test_parser_rejects_grounding_coordinates_outside_qwen_range() -> None:
    content = json.dumps(
        {
            "schema_version": "1.0",
            "batch_id": "batch_1",
            "assets": [
                {
                    "asset_id": "asset_a",
                    "fields": [
                        {
                            "field_code": "board_thickness_mm",
                            "field_value": "1.6 mm",
                            "unit": "mm",
                            "branch_state": "clear_now",
                            "source_text": "板厚 1.6 mm",
                            "check_note": "来源内容形成字段候选。",
                            "region": {"bbox_2d": [-1, 100, 500, 350]},
                        }
                    ],
                    "unknown_notes": [],
                }
            ],
        },
        ensure_ascii=False,
    )

    parsed = PdfSubmissionParser().parse(
        content,
        batch_id="batch_1",
        asset_dimensions={"asset_a": (100, 80)},
        field_table=build_current_field_table(),
    )

    assert parsed.accepted_assets[0].fields == []
    assert [item.reason_code for item in parsed.rejections] == ["invalid_region"]
    assert parsed.region_degradations == ()


def test_parser_degrades_grounding_box_smaller_than_one_source_pixel() -> None:
    content = json.dumps(
        {
            "schema_version": "1.0",
            "batch_id": "batch_1",
            "assets": [
                {
                    "asset_id": "asset_a",
                    "fields": [
                        {
                            "field_code": "board_thickness_mm",
                            "field_value": "1.6 mm",
                            "unit": "mm",
                            "branch_state": "clear_now",
                            "source_text": "板厚 1.6 mm",
                            "check_note": "来源内容形成字段候选。",
                            "region": {"bbox_2d": [1, 1, 2, 2]},
                        }
                    ],
                    "unknown_notes": [],
                }
            ],
        },
        ensure_ascii=False,
    )

    parsed = PdfSubmissionParser().parse(
        content,
        batch_id="batch_1",
        asset_dimensions={"asset_a": (100, 80)},
        field_table=build_current_field_table(),
    )

    assert parsed.accepted_assets[0].fields[0].region is None
    assert parsed.rejections == ()
    assert [item.reason_code for item in parsed.region_degradations] == ["empty_after_clip"]
    assert parsed.region_degradations[0].original_region.bbox_2d == [1.0, 1.0, 2.0, 2.0]


def test_parser_scales_reference_grounding_box_on_large_non_square_asset() -> None:
    content = json.dumps(
        {
            "schema_version": "1.0",
            "batch_id": "batch_1",
            "assets": [
                {
                    "asset_id": "asset_a",
                    "fields": [
                        {
                            "field_code": "surface_finish",
                            "field_value": "OSP",
                            "unit": None,
                            "branch_state": "clear_now",
                            "source_text": "Surface finish OSP",
                            "check_note": "来源内容形成字段候选。",
                            "region": {"bbox_2d": [650, 440, 800, 460]},
                        }
                    ],
                    "unknown_notes": [],
                }
            ],
        }
    )

    parsed = PdfSubmissionParser().parse(
        content,
        batch_id="batch_1",
        asset_dimensions={"asset_a": (2352, 2386)},
        field_table=build_current_field_table(),
    )

    assert parsed.rejections == ()
    assert parsed.region_degradations == ()
    assert parsed.accepted_assets[0].fields[0].region.model_dump(mode="json") == {
        "x": 1528,
        "y": 1049,
        "width": 353,
        "height": 48,
        "unit": "px",
    }


def test_parser_treats_non_empty_check_note_as_opaque_text() -> None:
    content = json.dumps(
        {
            "schema_version": "1.0",
            "batch_id": "batch_1",
            "assets": [
                {
                    "asset_id": "asset_a",
                    "fields": [
                        {
                            "field_code": "rohs",
                            "field_value": "RoHS 3.0",
                            "unit": None,
                            "branch_state": "clear_now",
                            "source_text": "RoHS 3.0",
                            "check_note": " field_code=rohs，来源 mat_1，状态 clear_now。 ",
                            "region": None,
                        }
                    ],
                    "unknown_notes": [],
                }
            ],
        },
        ensure_ascii=False,
    )

    parsed = PdfSubmissionParser().parse(
        content,
        batch_id="batch_1",
        asset_dimensions={"asset_a": (100, 80)},
        field_table=build_current_field_table(),
    )

    assert parsed.rejections == ()
    assert parsed.accepted_assets[0].fields[0].check_note == (
        "field_code=rohs，来源 mat_1，状态 clear_now。"
    )


def test_parser_rejects_blank_check_note() -> None:
    content = json.dumps(
        {
            "schema_version": "1.0",
            "batch_id": "batch_1",
            "assets": [
                {
                    "asset_id": "asset_a",
                    "fields": [
                        {
                            "field_code": "board_thickness_mm",
                            "field_value": "1.6 mm",
                            "unit": "mm",
                            "branch_state": "clear_now",
                            "source_text": "板厚 1.6 mm",
                            "check_note": "   ",
                            "region": None,
                        }
                    ],
                    "unknown_notes": [],
                }
            ],
        },
        ensure_ascii=False,
    )

    parsed = PdfSubmissionParser().parse(
        content,
        batch_id="batch_1",
        asset_dimensions={"asset_a": (100, 80)},
        field_table=build_current_field_table(),
    )

    assert parsed.accepted_assets[0].fields == []
    assert [item.reason_code for item in parsed.rejections] == ["invalid_value"]


def test_parser_rejects_bad_envelope_as_retryable_whole_batch_failure() -> None:
    parsed = PdfSubmissionParser().parse(
        '{"schema_version":"1.0","batch_id":"wrong","assets":[]}',
        batch_id="batch_1",
        asset_dimensions={"asset_a": (100, 80)},
        field_table=build_current_field_table(),
    )

    assert parsed.envelope_rejected
    assert parsed.accepted_assets == ()
    assert parsed.rejections[0].level == "envelope"
    assert parsed.rejections[0].reason_code == "identity_mismatch"
