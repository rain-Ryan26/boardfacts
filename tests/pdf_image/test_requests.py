from __future__ import annotations

from flyboard.pdf_image.requests import ANALYSIS_INSTRUCTION, OUTPUT_SCHEMA


def test_prompt_separates_standard_fields_from_unknown_notes() -> None:
    assert "producer_scope 精确等于 branch_or_integration" in ANALYSIS_INSTRUCTION
    assert "producer_scope=integration_only 的字段绝对不得输出" in ANALYSIS_INSTRUCTION
    assert "才进入 unknown_notes" in ANALYSIS_INSTRUCTION
    assert "禁止生成 unknown_note_*" in ANALYSIS_INSTRUCTION
    assert "来源冲突摘要" not in ANALYSIS_INSTRUCTION
    field_hint = OUTPUT_SCHEMA["assets"][0]["fields"][0]["field_code"]
    assert field_hint == "允许的标准字段编码"


def test_prompt_delivers_normalized_region_coordinate_contract() -> None:
    assert "相对当前图片左上角" in ANALYSIS_INSTRUCTION
    assert "缩放到 [0,999]" in ANALYSIS_INSTRUCTION
    assert "不得返回原图像素坐标" in ANALYSIS_INSTRUCTION
    assert "无法可靠定位或无法满足该坐标合同则使用 null" in ANALYSIS_INSTRUCTION

    region_schema = OUTPUT_SCHEMA["assets"][0]["fields"][0]["region"]
    assert region_schema == {
        "bbox_2d": [
            "x_min（0..999 相对坐标）",
            "y_min（0..999 相对坐标）",
            "x_max（0..999 相对坐标）",
            "y_max（0..999 相对坐标）",
        ]
    }
