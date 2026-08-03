from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .schemas import FieldDefinition, FieldTable

__all__ = ["build_current_field_table"]

_SCHEMA_VERSION = "1.0"

_FieldCategory = Literal["项目参数", "板件设计参数"]
_ProducerScope = Literal["branch_or_integration", "integration_only"]


@dataclass(frozen=True, slots=True)
class _FieldCatalogEntry:
    field_code: str
    field_name: str
    category: _FieldCategory
    definition: str
    unit: str | None
    is_core: bool
    producer_scope: _ProducerScope


def _project_field(
    field_code: str,
    field_name: str,
    definition: str,
    *,
    unit: str | None = None,
    is_core: bool = False,
    producer_scope: _ProducerScope = "branch_or_integration",
) -> _FieldCatalogEntry:
    return _FieldCatalogEntry(
        field_code=field_code,
        field_name=field_name,
        category="项目参数",
        definition=definition,
        unit=unit,
        is_core=is_core,
        producer_scope=producer_scope,
    )


def _board_field(
    field_code: str,
    field_name: str,
    definition: str,
    *,
    unit: str | None = None,
    is_core: bool = False,
    producer_scope: _ProducerScope = "branch_or_integration",
) -> _FieldCatalogEntry:
    return _FieldCatalogEntry(
        field_code=field_code,
        field_name=field_name,
        category="板件设计参数",
        definition=definition,
        unit=unit,
        is_core=is_core,
        producer_scope=producer_scope,
    )


_FIELD_CATALOG = (
    _project_field(
        "project_code",
        "项目编码",
        "项目、料号、板号或客户项目编号。",
    ),
    _project_field(
        "project_name",
        "项目名称",
        "项目或板件名称。资料没有独立名称时可暂缺，不用 `description` 顶替。",
    ),
    _project_field(
        "project_instructions",
        "项目指示",
        "承接项目资料层面的说明、文件引用关系、设计资料指向和特殊解释规则；不承接板内生产参数和工艺要求。",
    ),
    _project_field(
        "project_analysis_findings",
        "项目处理发现",
        "承接资料登记、资料识别、参与分析、无法识别资料和暂不支持资料等项目处理层发现；用于追加多条处理说明，不承接板内生产参数、规则检查结论、报价或人工字段修改记录。",
        producer_scope="integration_only",
    ),
    _project_field(
        "drill_file_presence",
        "Drill 文件存在性",
        "是否存在 Drill 文件或可解析钻孔资料。",
        producer_scope="integration_only",
    ),
    _project_field(
        "gerber_job_presence",
        "Gerber 作业存在性",
        "是否存在 Gerber、GTD、Gerber Job 或可解析设计文件。",
        producer_scope="integration_only",
    ),
    _project_field(
        "spreadsheet_presence",
        "技术表存在性",
        "是否存在 XLSX 或其他结构化技术表。",
        producer_scope="integration_only",
    ),
    _project_field(
        "pdf_presence",
        "PDF/图片资料存在性",
        "是否存在 PDF 图纸、规格书、钻孔表、图纸截图或客户补充图片；PDF 与图片进入同一 PDF/图片分析分支，第一版不单独设置 `image_presence`。",
        producer_scope="integration_only",
    ),
    _board_field(
        "layer_count",
        "PCB 层数",
        "资料明确声明的 PCB 层数可以形成字段候选；GerberScout 只按已确认 copper role 的唯一 `copper_layer_index` 计数，Gerber 文件数量、解析文件数量不得直接作为 PCB 层数候选。",
        is_core=True,
    ),
    _board_field(
        "base_material",
        "基材",
        "FR4、FR4-V0 等板材主体材料。",
        is_core=True,
    ),
    _board_field(
        "board_thickness_mm",
        "成品板厚",
        "只表示 PCB 成品总厚度；其他厚度或缺失说明不得顶替。",
        unit="mm",
    ),
    _board_field(
        "outer_copper_thickness",
        "外层铜厚",
        "外层铜厚独立字段，不和内层铜厚合并；固定以 `µm` 输出，资料中的 `um`、`μm`、`µm` 按微米单位规范化。",
        unit="µm",
    ),
    _board_field(
        "inner_copper_thickness",
        "内层铜厚",
        "内层铜厚独立字段，即使数值相同也不得复用另一来源；固定以 `µm` 输出，资料中的 `um`、`μm`、`µm` 按微米单位规范化。",
        unit="µm",
    ),
    _board_field(
        "board_size_mm",
        "单板尺寸",
        "单片 PCB 成品尺寸的复合表达，不得被 panel、frame 或 Gerber bounds 覆盖。",
        unit="mm",
    ),
    _board_field(
        "board_length_mm",
        "单板长度",
        "单片 PCB 成品长度，单位统一为 mm。",
        unit="mm",
        is_core=True,
    ),
    _board_field(
        "board_width_mm",
        "单板宽度",
        "单片 PCB 成品宽度，单位统一为 mm。",
        unit="mm",
        is_core=True,
    ),
    _board_field(
        "panel_size_mm",
        "拼板尺寸",
        "panel、frame、usable area 等拼板或工艺边尺寸。",
        unit="mm",
    ),
    _board_field(
        "usable_area_mm",
        "可用区域尺寸",
        "PDF 图纸中 panel 可用区域的尺寸。",
        unit="mm",
    ),
    _board_field(
        "gerber_bounds_mm",
        "Gerber 作业外框",
        "GerberScout 联合当前 job 中未忽略 Gerber 图形层得到 `{min_x, min_y, max_x, max_y}`；不包含 Drill-only 坐标，只作为工具测量结果，不等同单板尺寸或 panel 尺寸。",
        unit="mm",
    ),
    _board_field(
        "panelization_summary",
        "拼板摘要",
        "PDF panel layout 和 XLSX 矩阵信息都可进入该字段；多来源表达可合并摘要，不直接判冲突。",
    ),
    _board_field(
        "mechanical_process",
        "机械加工",
        "V-cut、scoring、铣边、break tab 等机械加工说明。",
    ),
    _board_field(
        "fiducial_mark_requirement",
        "Mark 点要求",
        "PDF 图纸出现 fiducial marks 时记录。",
    ),
    _board_field(
        "break_point_requirement",
        "断点要求",
        "PDF 图纸出现 break-point 示例或结构时记录。",
    ),
    _board_field(
        "min_trace_width_mm",
        "最小线宽",
        "可来自 GerberScout 对已确认铜层且覆盖完整的几何测量；unsupported primitive 或部分覆盖只能形成不可靠候选或缺值诊断，与最小线距分开。",
        unit="mm",
    ),
    _board_field(
        "min_trace_spacing_mm",
        "最小线距",
        "可来自结构化资料，也可来自 GerberScout 对已确认铜层且覆盖完整的最小铜间距测量；与最小线宽分开。",
        unit="mm",
    ),
    _board_field(
        "min_finished_hole_size",
        "最小成品孔径",
        "客户资料声明的最小成品孔径，不等同钻孔尺寸全集。",
        unit="mm",
    ),
    _board_field(
        "drill_size_list",
        "钻孔尺寸列表",
        "钻孔表或 GerberScout 中实际 hit 引用的普通圆孔 tool 尺寸集合；未使用 tool、slot 和 route 不进入该字段。",
        unit="mm",
    ),
    _board_field(
        "drill_quantity_list",
        "钻孔数量列表",
        "钻孔表或 GerberScout 中各普通圆孔尺寸对应的实际 hit 数量；必须与 `drill_size_list` 使用同一 hit 集合，不等同订单生产数量。",
    ),
    _board_field(
        "slot_size_list",
        "槽孔尺寸列表",
        "槽孔、长圆孔或 routing slot 的宽、长和数量集合；GerberScout 只承接稳定解析的 slot/route，不能把普通圆孔或文件名猜测写入该字段；含义仍无法判断时作为未知说明保留。",
        unit="mm",
    ),
    _board_field(
        "plated_hole_requirement",
        "金属化孔要求",
        "钻孔表 `PLTD` 等字段，或 GerberScout 从 GTD、Drill 属性、X2/X3 文件功能元数据得到的明确金属化信息；文件名猜测不能形成候选。",
    ),
    _board_field(
        "non_plated_hole_requirement",
        "非金属化孔要求",
        "NPTH、hole not plated，或 GerberScout 从 GTD、Drill 属性、X2/X3 文件功能元数据得到的明确非金属化信息；文件名猜测不能形成候选。",
    ),
    _board_field(
        "via_type_summary",
        "孔类型摘要",
        "通孔、盲孔、埋孔等孔类型语义；不接收孔径列表。",
    ),
    _board_field(
        "blind_vias",
        "盲孔要求",
        "是否存在盲孔及相关说明。",
    ),
    _board_field(
        "buried_vias",
        "埋孔要求",
        "是否存在埋孔及相关说明。",
    ),
    _board_field(
        "min_hole_diameter_mm",
        "工具测得最小孔径",
        "GerberScout 从实际 hit 引用的普通圆孔 tool 中取得最小直径；未使用 tool、slot 和 route 不参与最小值。",
        unit="mm",
    ),
    _board_field(
        "min_annular_ring_mm",
        "最小环宽",
        "GerberScout 在已确认铜层与 drill role 间形成稳定 pad/drill 关联后测得的候选；关联覆盖不足时只能形成不可靠候选或缺值诊断。",
        unit="mm",
    ),
    _board_field(
        "surface_finish",
        "表面处理",
        "HASL、ENIG、OSP 等表面处理。",
        is_core=True,
    ),
    _board_field(
        "solder_mask_layers",
        "阻焊层面",
        "TOP、BOT 或双面阻焊要求；GerberScout 只根据已确认的 `top_solder_mask`、`bottom_solder_mask` role 形成候选。",
    ),
    _board_field(
        "solder_mask_color",
        "阻焊颜色",
        "阻焊油墨颜色。",
    ),
    _board_field(
        "silkscreen_layers",
        "字符层面",
        "TOP、BOT 或双面字符要求；GerberScout 只根据已确认的 `top_silkscreen`、`bottom_silkscreen` role 形成候选。",
    ),
    _board_field(
        "silkscreen_color",
        "字符颜色",
        "丝印或字符颜色。",
    ),
    _board_field(
        "ipc_class",
        "IPC 等级",
        "IPC Class 或制造等级。",
    ),
    _board_field(
        "rohs",
        "RoHS 要求",
        "RoHS 合规要求。",
    ),
    _board_field(
        "ul_flame_class",
        "UL 阻燃等级",
        "UL94 V-0 等阻燃等级。",
    ),
    _board_field(
        "ul_max_operating_temperature",
        "UL 最高工作温度",
        "UL 认证最高工作温度。",
    ),
    _board_field(
        "unknown_note_###",
        "未知说明",
        "最终项目数据列表中的未知说明行。无法归入既有字段但有生产意义的参数、工艺、备注或其他说明，以及资料中只有字段名称和占位表达而没有真实工程值的内容，由整合阶段按稳定遍历顺序生成 `unknown_note_001`、`unknown_note_002` 等独立行；分支阶段不得预分配该最终编码。",
        producer_scope="integration_only",
    ),
)


def _canonical_json_bytes(
    schema_version: str,
    fields: Sequence[FieldDefinition],
) -> bytes:
    canonical_json = json.dumps(
        {
            "schema_version": schema_version,
            "fields": [field.model_dump(mode="json") for field in fields],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return canonical_json.encode("utf-8")


def _compute_content_sha256(
    schema_version: str,
    fields: Sequence[FieldDefinition],
) -> str:
    return hashlib.sha256(_canonical_json_bytes(schema_version, fields)).hexdigest()


def build_current_field_table() -> FieldTable:
    fields = [
        FieldDefinition(
            field_code=entry.field_code,
            field_name=entry.field_name,
            category=entry.category,
            definition=entry.definition,
            unit=entry.unit,
            is_core=entry.is_core,
            producer_scope=entry.producer_scope,
        )
        for entry in _FIELD_CATALOG
    ]
    return FieldTable(
        schema_version=_SCHEMA_VERSION,
        content_sha256=_compute_content_sha256(_SCHEMA_VERSION, fields),
        fields=fields,
    )
