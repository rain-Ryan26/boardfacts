from __future__ import annotations

import json

from flyboard.model_calls import (
    ImageContentPart,
    ModelCallRequest,
    ModelMessage,
    TextContentPart,
)
from flyboard.models import CallPurpose

from .models import (
    PdfIncludedRange,
    PdfModelRequestArtifact,
    PdfRequestAsset,
    PdfRequestPageText,
)

SYSTEM_PROMPT = """你是 BoardFacts 的 PDF/图片工程资料分析节点。只依据当前有序图片、可用页文本和完整字段表提取 PCB 工程事实。

必须遵守：
1. 响应必须是首尾为大括号的单个裸 JSON 对象，不得使用 Markdown code fence 或附加说明。
2. 顶层必须且只能包含 schema_version、batch_id、assets 三个键；每次都必须原样返回 \"schema_version\":\"1.0\" 和请求 batch_id，即使没有提取到候选也不能省略。
3. 每个请求 asset 必须恰好返回一次，并保留请求 asset_id；不得猜测或输出资料 id、页码、路径、evidence id 或最终项目状态。
4. 字段候选和未知说明必须分别返回非空中文 check_note。备注要说明当前可读证据提供了什么事实、根据字段定义形成什么候选或未知判断，以及信息不足或矛盾对本节点结果的影响；不得只复读 source_text，不得回显 asset id、字段编码、资料/证据/error id、路径、内部状态名或协议字段。
5. 严格使用调用方提供的唯一 output_schema，不增删或改名任何键。
"""
ANALYSIS_INSTRUCTION = """逐个 asset 提取可可靠对应字段表的 PCB 工程字段，以及无法归入标准字段但有生产意义的未知说明。
每个字段保留真实工程表达；信息不足但仍能对应正式字段时使用 unknown，明确真实矛盾时使用 conflict。
字段候选只使用 field_table 中 producer_scope 精确等于 branch_or_integration 的 field_code；producer_scope=integration_only 的字段绝对不得输出，尤其不得输出资料存在性或项目处理发现。只有当前 asset 中找到非空、真实、可复核的工程值时才加入 fields；不得遍历字段表补空行，不得为未提供、空白、未知、N/A、无法判断或仅有字段名称的内容提交空字符串候选。这些没有真实值的字段直接省略；只有实际存在且有生产意义、但无法归入标准字段的非空内容才进入 unknown_notes，禁止生成 unknown_note_*。
每个字段候选的 unit 必须与该 field_code 在 field_table 中的 unit 精确一致。定义 unit 非空时，先把资料值换算到该单位，再把换算后的完整表达写入 field_value，并把 unit 设为定义值；复合尺寸或列表的每个数值都要换算。固定换算为 1 mil = 0.0254 mm。定义 unit 为 null 时必须返回 unit=null，并在 field_value 和 source_text 中保留资料原单位。不得因为原文使用其他单位而返回 null 或其他单位。
source_text 只保存可复核原文；check_note 必须另行用简短中文说明“依据什么可读证据、得到什么事实、按字段定义形成什么判断、对候选状态有什么影响”，两者不得互相替代。
region 仅在能够可靠定位支持当前候选的图片区域时返回；格式固定为 {\"bbox_2d\":[x_min,y_min,x_max,y_max]}，四个值必须是相对当前图片左上角、缩放到 [0,999] 的有限数值，并满足 x_max>x_min、y_max>y_min；不得返回原图像素坐标，无法可靠定位或无法满足该坐标合同则使用 null。
响应顶层固定且仅为 schema_version、batch_id、assets，并必须返回 schema_version=1.0；每个请求 asset 必须恰好返回一次。"""
OUTPUT_SCHEMA = {
    "schema_version": "1.0（必填，不得省略）",
    "batch_id": "请求 batch_id（必填）",
    "assets": [
        {
            "asset_id": "请求 asset_id",
            "fields": [
                {
                    "field_code": "允许的标准字段编码",
                    "field_value": "按 FieldTable 单位换算后的非空工程表达",
                    "unit": "必须与该字段的 FieldTable unit 精确一致",
                    "branch_state": "clear_now | unknown | conflict",
                    "source_text": "可复核原文或 null",
                    "check_note": "根据可读证据和字段定义形成的非空中文判断说明",
                    "region": {
                        "bbox_2d": [
                            "x_min（0..999 相对坐标）",
                            "y_min（0..999 相对坐标）",
                            "x_max（0..999 相对坐标）",
                            "y_max（0..999 相对坐标）",
                        ]
                    },
                }
            ],
            "unknown_notes": [
                {
                    "text": "未知说明",
                    "source_text": "原文或 null",
                    "check_note": "为何有生产意义但不能归入标准字段的非空中文判断说明",
                    "region": None,
                }
            ],
        }
    ],
}


def build_request_artifact(batch, snapshot, policy, context, attempt, char_limit):
    return PdfModelRequestArtifact(
        schema_version="1.0",
        run_id=context.run_id,
        batch_id=batch.batch_id,
        parent_batch_id=batch.parent_batch_id,
        attempt=attempt,
        source_material_id=batch.source_material_id,
        model=policy.model_id,
        timeout_seconds=float(policy.timeout_seconds),
        system_prompt=SYSTEM_PROMPT,
        analysis_instruction=ANALYSIS_INSTRUCTION,
        assets=[request_asset(item, char_limit) for item in batch.assets],
        field_table=snapshot.field_table,
        output_schema_version="1.0",
    )


def build_model_request(batch, snapshot, artifact):
    prompt = json.dumps(
        {
            "batch_id": batch.batch_id,
            "assets": [item.model_dump(mode="json") for item in artifact.assets],
            "field_table": snapshot.field_table.model_dump(mode="json"),
            "analysis_instruction": ANALYSIS_INSTRUCTION,
            "output_schema": OUTPUT_SCHEMA,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    content = [TextContentPart(prompt)]
    for asset in batch.assets:
        content.append(TextContentPart(f"asset_id={asset.record.asset_id}"))
        content.append(ImageContentPart(asset.content, "image/png"))
    return ModelCallRequest(
        call_purpose=CallPurpose.PDF_IMAGE,
        messages=(
            ModelMessage(role="system", content=(TextContentPart(SYSTEM_PROMPT),)),
            ModelMessage(role="user", content=tuple(content)),
        ),
    )


def request_asset(asset, char_limit):
    page_text = asset.page_text
    if page_text is None:
        text = PdfRequestPageText(
            status="unavailable",
            text="",
            original_char_count=0,
            normalized_char_count=0,
            truncated=False,
            included_ranges=[],
        )
    elif page_text.status == "empty":
        text = PdfRequestPageText(
            status="empty",
            text="",
            original_char_count=page_text.original_char_count,
            normalized_char_count=0,
            truncated=False,
            included_ranges=[],
        )
    elif char_limit == 0:
        text = PdfRequestPageText(
            status="unavailable",
            text="",
            original_char_count=page_text.original_char_count,
            normalized_char_count=page_text.normalized_char_count,
            truncated=False,
            included_ranges=[],
        )
    elif len(page_text.text) <= char_limit:
        text = PdfRequestPageText(
            status="included",
            text=page_text.text,
            original_char_count=page_text.original_char_count,
            normalized_char_count=page_text.normalized_char_count,
            truncated=False,
            included_ranges=[PdfIncludedRange(start=0, end=len(page_text.text))],
        )
    else:
        first_length = char_limit // 2
        last_length = char_limit - first_length
        tail_start = len(page_text.text) - last_length
        ranges = []
        pieces = []
        if first_length:
            ranges.append(PdfIncludedRange(start=0, end=first_length))
            pieces.append(page_text.text[:first_length])
        if last_length:
            ranges.append(PdfIncludedRange(start=tail_start, end=len(page_text.text)))
            pieces.append(page_text.text[tail_start:])
        text = PdfRequestPageText(
            status="included",
            text="\n".join(pieces),
            original_char_count=page_text.original_char_count,
            normalized_char_count=page_text.normalized_char_count,
            truncated=True,
            included_ranges=ranges,
        )
    return PdfRequestAsset(
        asset_id=asset.record.asset_id,
        asset_path=asset.record.asset_path,
        mime_type="image/png",
        page_text=text,
    )
