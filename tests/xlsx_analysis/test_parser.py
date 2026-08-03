from __future__ import annotations

import json

import pytest

from flyboard.models import build_current_field_table
from flyboard.xlsx_analysis.analyzer import (
    ANALYSIS_INSTRUCTION,
    OUTPUT_PROTOCOL,
    SYSTEM_PROMPT,
    _user_prompt,
)
from flyboard.xlsx_analysis.models import XlsxCellRecord
from flyboard.xlsx_analysis.parser import XlsxSubmissionParser


def cell(address="B1", *, origin="literal"):
    formula = "A1*2" if origin != "literal" else None
    return XlsxCellRecord(
        source_material_id="mat_1",
        sheet_index=1,
        sheet_name="技术要求",
        address=address,
        row=1,
        column=2,
        value_kind="number",
        raw_value="1.6",
        normalized_value="1.6",
        display_text="1.6",
        formula=formula,
        value_origin=origin,
        number_format_code=None,
        merged_range=None,
        row_hidden=False,
        column_hidden=False,
    )


def unavailable_formula_cell():
    return XlsxCellRecord(
        source_material_id="mat_1",
        sheet_index=1,
        sheet_name="技术要求",
        address="B1",
        row=1,
        column=2,
        value_kind="unavailable",
        raw_value=None,
        normalized_value=None,
        display_text=None,
        formula="A1*2",
        value_origin="formula_without_cache",
        number_format_code=None,
        merged_range=None,
        row_hidden=False,
        column_hidden=False,
    )


def empty_cell():
    return XlsxCellRecord(
        source_material_id="mat_1",
        sheet_index=1,
        sheet_name="技术要求",
        address="B1",
        row=1,
        column=2,
        value_kind="empty",
        raw_value=None,
        normalized_value=None,
        display_text="",
        formula=None,
        value_origin="literal",
        number_format_code=None,
        merged_range=None,
        row_hidden=False,
        column_hidden=False,
    )


def response(fields, unknown_notes=None):
    return "SUBMIT_XLSX: " + json.dumps(
        {"fields": fields, "unknown_notes": unknown_notes or []},
        ensure_ascii=False,
    )


def submitted(**changes):
    values = {
        "field_code": "board_thickness_mm",
        "field_value": "1.60 mm",
        "unit": "mm",
        "branch_state": "clear_now",
        "source_material_id": "mat_1",
        "sheet_index": 1,
        "source_cells": ["$b$1"],
        "check_note": "成品板厚可读。",
    }
    values.update(changes)
    return values


def unknown_note(**changes):
    values = {
        "field_value": "阻抗要求：未提供",
        "source_material_id": "mat_1",
        "sheet_index": 1,
        "source_cells": ["B1"],
        "check_note": "资料只给出占位表达。",
    }
    values.update(changes)
    return values


def test_parser_keeps_valid_items_and_rejects_only_invalid_items() -> None:
    parsed = XlsxSubmissionParser().parse(
        response([submitted(), submitted(field_code="not_in_table")]),
        field_table=build_current_field_table(),
        cells=(cell(),),
    )

    assert len(parsed.fields) == 1
    assert parsed.fields[0].source_cells == ["B1"]
    assert parsed.fields[0].branch_state == "clear_now"
    assert [item.reason_code for item in parsed.rejections] == ["field_item_invalid"]


def test_parser_accepts_copper_thickness_in_the_field_table_unit() -> None:
    parsed = XlsxSubmissionParser().parse(
        response(
            [
                submitted(
                    field_code="outer_copper_thickness",
                    field_value="35 µm",
                    unit="µm",
                ),
                submitted(
                    field_code="inner_copper_thickness",
                    field_value="18 µm",
                    unit=None,
                ),
            ]
        ),
        field_table=build_current_field_table(),
        cells=(cell(),),
    )

    assert [(item.field_code, item.unit) for item in parsed.fields] == [
        ("outer_copper_thickness", "µm")
    ]
    assert [item.reason_code for item in parsed.rejections] == ["field_item_invalid"]


def test_parser_rejects_missing_state_and_treats_formula_cache_note_as_opaque() -> None:
    parser = XlsxSubmissionParser()
    table = build_current_field_table()
    cached = (cell(origin="formula_cache"),)

    rejected = parser.parse(
        response([submitted(branch_state="missing")]), field_table=table, cells=cached
    )
    assert not rejected.fields
    assert len(rejected.rejections) == 1

    accepted = parser.parse(
        response([submitted(check_note="技术表给出可用计算结果。")]),
        field_table=table,
        cells=cached,
    )
    assert len(accepted.fields) == 1
    assert accepted.rejections == ()


def test_unknown_note_accepts_empty_source_but_rejects_formula_without_cache() -> None:
    parser = XlsxSubmissionParser()
    table = build_current_field_table()

    accepted = parser.parse(
        response([], [unknown_note()]),
        field_table=table,
        cells=(empty_cell(),),
    )
    assert len(accepted.unknown_notes) == 1
    assert not accepted.rejections

    rejected = parser.parse(
        response([], [unknown_note()]),
        field_table=table,
        cells=(unavailable_formula_cell(),),
    )
    assert not rejected.unknown_notes
    assert [item.reason_code for item in rejected.rejections] == ["unknown_note_item_invalid"]


def test_prompt_defines_integration_owned_fields_and_uses_eligible_example() -> None:
    field_table = build_current_field_table().model_dump(mode="json")
    prompt = _user_prompt(field_table, {"workbooks": [], "cells": []})
    scope_section = prompt.split("本次字段生产范围", maxsplit=1)[1].split(
        "最终输出协议", maxsplit=1
    )[0]

    assert "producer_scope=branch_or_integration" in SYSTEM_PROMPT
    assert "只能由后续 Integration" in SYSTEM_PROMPT
    assert "producer_scope=branch_or_integration" in ANALYSIS_INSTRUCTION
    assert "producer_scope 必须等于 branch_or_integration" in OUTPUT_PROTOCOL
    assert "直接派生自上述完整字段表" in prompt
    for definition in field_table["fields"]:
        assert definition["field_code"] in scope_section
    assert "spreadsheet_presence" in scope_section
    assert prompt.index("XLSX 解析内容") < prompt.index("本次字段生产范围")
    assert prompt.index("本次字段生产范围") < prompt.index("最终输出协议")
    assert '"field_code": "board_thickness_mm"' in OUTPUT_PROTOCOL
    assert '"field_code": "字段表中的编码"' not in OUTPUT_PROTOCOL


def test_prompt_keeps_document_unit_in_value_when_field_table_unit_is_null() -> None:
    field_table = build_current_field_table().model_dump(mode="json")
    prompt = _user_prompt(field_table, {"workbooks": [], "cells": []})
    final_protocol = prompt.split("最终输出协议：\n", maxsplit=1)[1]

    assert "unit 属性必须为 null" in ANALYSIS_INSTRUCTION
    assert "不表示删除或省略资料中的物理单位" in ANALYSIS_INSTRUCTION
    assert "字段表 unit 为 null 只表示提交对象的 unit 属性必须为 null" in final_protocol
    assert "field_value 仍必须保留完整工程表达" in final_protocol

    example = json.loads(final_protocol.split("SUBMIT_XLSX: ", maxsplit=1)[1])
    temperature = next(
        item for item in example["fields"] if item["field_code"] == "ul_max_operating_temperature"
    )
    assert temperature["field_value"] == "135 °C"
    assert temperature["unit"] is None
    assert prompt.index("XLSX 解析内容") < prompt.index("最终输出协议")


def test_prompt_requires_non_empty_chinese_business_notes_without_internal_echo() -> None:
    prompt = "\n".join((SYSTEM_PROMPT, ANALYSIS_INSTRUCTION, OUTPUT_PROTOCOL))

    assert "非空中文" in prompt
    assert "不得只" in prompt
    for forbidden in (
        "field_code",
        "source_material_id",
        "source_ref",
        "branch_state",
        "SUBMIT_XLSX",
    ):
        assert forbidden in prompt


def test_parser_rejects_only_blank_notes_for_both_item_kinds() -> None:
    parser = XlsxSubmissionParser()
    table = build_current_field_table()

    for check_note in ("", "   "):
        parsed_field = parser.parse(
            response([submitted(check_note=check_note)]),
            field_table=table,
            cells=(cell(),),
        )
        assert not parsed_field.fields
        assert [item.reason_code for item in parsed_field.rejections] == ["field_item_invalid"]

        parsed_unknown = parser.parse(
            response([], [unknown_note(check_note=check_note)]),
            field_table=table,
            cells=(cell(),),
        )
        assert not parsed_unknown.unknown_notes
        assert [item.reason_code for item in parsed_unknown.rejections] == [
            "unknown_note_item_invalid"
        ]


@pytest.mark.parametrize(
    "check_note",
    [
        "accepted",
        "技术表对应 mat_1。",
        "字段 board_thickness_mm 已形成候选。",
        "当前状态为 clear_now。",
        "来源为 source_ref=sources/mat_1/input.xlsx。",
        "SUBMIT_XLSX 已提交。",
        "已确认。",
    ],
)
def test_parser_treats_non_empty_note_text_as_opaque(check_note) -> None:
    parser = XlsxSubmissionParser()
    table = build_current_field_table()

    parsed_field = parser.parse(
        response([submitted(check_note=f"  {check_note}  ")]),
        field_table=table,
        cells=(cell(),),
    )
    parsed_unknown = parser.parse(
        response([], [unknown_note(check_note=f"  {check_note}  ")]),
        field_table=table,
        cells=(cell(),),
    )

    assert parsed_field.fields[0].check_note == check_note
    assert parsed_field.rejections == ()
    assert parsed_unknown.unknown_notes[0].check_note == check_note
    assert parsed_unknown.rejections == ()


def test_parser_accepts_field_value_echo_and_trims_note() -> None:
    parser = XlsxSubmissionParser()
    table = build_current_field_table()

    echoed = parser.parse(
        response([], [unknown_note(check_note="  阻抗要求：未提供。  ")]),
        field_table=table,
        cells=(cell(),),
    )
    accepted = parser.parse(
        response([submitted(check_note="  技术表给出成品板厚 1.60 mm。  ")]),
        field_table=table,
        cells=(cell(),),
    )

    assert echoed.unknown_notes[0].check_note == "阻抗要求：未提供。"
    assert echoed.rejections == ()
    assert accepted.fields[0].check_note == "技术表给出成品板厚 1.60 mm。"
    assert accepted.rejections == ()


def test_parser_keeps_other_items_when_one_business_note_is_invalid() -> None:
    parsed = XlsxSubmissionParser().parse(
        response(
            [
                submitted(),
                submitted(
                    field_code="outer_copper_thickness",
                    field_value="35 µm",
                    unit="µm",
                    check_note="   ",
                ),
            ]
        ),
        field_table=build_current_field_table(),
        cells=(cell(),),
    )

    assert [item.field_code for item in parsed.fields] == ["board_thickness_mm"]
    assert [item.reason_code for item in parsed.rejections] == ["field_item_invalid"]
