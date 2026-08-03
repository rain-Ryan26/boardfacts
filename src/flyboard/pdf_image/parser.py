from __future__ import annotations

import json
import math
from dataclasses import dataclass
from numbers import Real

from flyboard.models import FieldDefinition, FieldTable, ProducerScope, Region

from .models import (
    PdfAcceptedAsset,
    PdfAcceptedField,
    PdfAcceptedUnknownNote,
    PdfModelRejection,
    PdfRawRegion,
    PdfRegionDegradation,
)

_QWEN_GROUNDING_COORDINATE_MAX = 999
_QWEN_GROUNDING_SCALE = 1000


@dataclass(frozen=True, slots=True)
class ParsedPdfBatch:
    accepted_assets: tuple[PdfAcceptedAsset, ...]
    rejections: tuple[PdfModelRejection, ...]
    region_degradations: tuple[PdfRegionDegradation, ...]
    envelope_rejected: bool


class PdfSubmissionParser:
    def parse(
        self,
        content: str,
        *,
        batch_id: str,
        asset_dimensions: dict[str, tuple[int, int]],
        field_table: FieldTable,
    ) -> ParsedPdfBatch:
        try:
            raw = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return _envelope_rejection("invalid_json", "响应不是合法 JSON。")
        if type(raw) is not dict:
            return _envelope_rejection("invalid_envelope", "响应顶层必须是 object。")
        if set(raw) != {"schema_version", "batch_id", "assets"}:
            return _envelope_rejection("invalid_envelope_keys", "响应顶层键集合不合法。")
        if raw["schema_version"] != "1.0" or raw["batch_id"] != batch_id:
            return _envelope_rejection("identity_mismatch", "响应版本或 batch_id 不匹配。")
        if type(raw["assets"]) is not list:
            return _envelope_rejection("assets_not_array", "assets 必须是数组。")

        definitions = {
            item.field_code: item
            for item in field_table.fields
            if item.producer_scope == ProducerScope.BRANCH_OR_INTEGRATION
        }
        expected_ids = list(asset_dimensions)
        occurrences: dict[str, list[tuple[int, object]]] = {}
        rejections: list[PdfModelRejection] = []
        for index, item in enumerate(raw["assets"]):
            asset_id = item.get("asset_id") if type(item) is dict else None
            if not isinstance(asset_id, str) or not asset_id:
                rejections.append(
                    _rejection("asset", None, index, "invalid_asset", "asset 结构或 id 不合法。")
                )
                continue
            occurrences.setdefault(asset_id, []).append((index, item))

        accepted: list[PdfAcceptedAsset] = []
        degradations: list[PdfRegionDegradation] = []
        for asset_id in expected_ids:
            matches = occurrences.get(asset_id, [])
            if not matches:
                rejections.append(
                    _rejection("asset", asset_id, None, "asset_missing", "请求 asset 未返回。")
                )
                continue
            if len(matches) != 1:
                for item_index, _ in matches:
                    rejections.append(
                        _rejection(
                            "asset",
                            asset_id,
                            item_index,
                            "asset_duplicate",
                            "同一 asset 返回多次，全部拒绝。",
                        )
                    )
                continue
            response_index, item = matches[0]
            parsed = self._parse_asset(
                item,
                response_index=response_index,
                asset_id=asset_id,
                dimensions=asset_dimensions[asset_id],
                field_definitions=definitions,
            )
            if parsed[0] is not None:
                accepted.append(parsed[0])
            rejections.extend(parsed[1])
            degradations.extend(parsed[2])

        for asset_id, matches in occurrences.items():
            if asset_id in asset_dimensions:
                continue
            for item_index, _ in matches:
                rejections.append(
                    _rejection(
                        "asset",
                        asset_id,
                        item_index,
                        "asset_unexpected",
                        "响应包含未请求的 asset。",
                    )
                )
        return ParsedPdfBatch(tuple(accepted), tuple(rejections), tuple(degradations), False)

    def _parse_asset(
        self,
        item: object,
        *,
        response_index: int,
        asset_id: str,
        dimensions: tuple[int, int],
        field_definitions: dict[str, FieldDefinition],
    ) -> tuple[
        PdfAcceptedAsset | None,
        list[PdfModelRejection],
        list[PdfRegionDegradation],
    ]:
        if type(item) is not dict or set(item) != {"asset_id", "fields", "unknown_notes"}:
            return (
                None,
                [
                    _rejection(
                        "asset",
                        asset_id,
                        response_index,
                        "invalid_asset_keys",
                        "asset 键集合不合法。",
                    )
                ],
                [],
            )
        if type(item["fields"]) is not list or type(item["unknown_notes"]) is not list:
            return (
                None,
                [
                    _rejection(
                        "asset",
                        asset_id,
                        response_index,
                        "invalid_candidate_arrays",
                        "fields 和 unknown_notes 必须是数组。",
                    )
                ],
                [],
            )

        fields: list[PdfAcceptedField] = []
        notes: list[PdfAcceptedUnknownNote] = []
        rejections: list[PdfModelRejection] = []
        degradations: list[PdfRegionDegradation] = []
        for index, candidate in enumerate(item["fields"]):
            parsed, rejection, degradation = _parse_field(
                candidate,
                asset_id=asset_id,
                item_index=index,
                dimensions=dimensions,
                field_definitions=field_definitions,
            )
            if parsed is not None:
                fields.append(parsed)
            if rejection is not None:
                rejections.append(rejection)
            if degradation is not None:
                degradations.append(degradation)
        for index, candidate in enumerate(item["unknown_notes"]):
            parsed, rejection, degradation = _parse_unknown_note(
                candidate,
                asset_id=asset_id,
                item_index=index,
                dimensions=dimensions,
            )
            if parsed is not None:
                notes.append(parsed)
            if rejection is not None:
                rejections.append(rejection)
            if degradation is not None:
                degradations.append(degradation)
        return (
            PdfAcceptedAsset(asset_id=asset_id, fields=fields, unknown_notes=notes),
            rejections,
            degradations,
        )


def _parse_field(candidate, *, asset_id, item_index, dimensions, field_definitions):
    expected = {
        "field_code",
        "field_value",
        "unit",
        "branch_state",
        "source_text",
        "check_note",
        "region",
    }
    if type(candidate) is not dict or set(candidate) != expected:
        return (
            None,
            _rejection("field", asset_id, item_index, "invalid_keys", "字段候选键集合不合法。"),
            None,
        )
    field_code = candidate["field_code"]
    field_value = candidate["field_value"]
    unit = candidate["unit"]
    branch_state = candidate["branch_state"]
    source_text = candidate["source_text"]
    check_note = candidate["check_note"]
    definition = field_definitions.get(field_code)
    if (
        definition is None
        or not _non_empty(field_value)
        or (unit is not None and not _non_empty(unit))
        or branch_state not in {"clear_now", "unknown", "conflict"}
        or (source_text is not None and not isinstance(source_text, str))
        or not _valid_check_note(check_note)
    ):
        return (
            None,
            _rejection("field", asset_id, item_index, "invalid_value", "字段候选值不符合协议。"),
            None,
        )
    if unit != definition.unit:
        return (
            None,
            _rejection(
                "field",
                asset_id,
                item_index,
                "unit_mismatch",
                "字段候选单位与 FieldTable 不一致。",
            ),
            None,
        )
    region, degradation, error = _normalize_region(
        candidate["region"], asset_id, "field", item_index, dimensions
    )
    if error is not None:
        return None, _rejection("field", asset_id, item_index, "invalid_region", error), None
    return (
        PdfAcceptedField(
            field_code=field_code,
            field_value=field_value,
            unit=unit,
            branch_state=branch_state,
            source_text=source_text,
            check_note=check_note.strip(),
            region=region,
        ),
        None,
        degradation,
    )


def _parse_unknown_note(candidate, *, asset_id, item_index, dimensions):
    if type(candidate) is not dict or set(candidate) != {
        "text",
        "source_text",
        "check_note",
        "region",
    }:
        return (
            None,
            _rejection(
                "unknown_note", asset_id, item_index, "invalid_keys", "未知说明键集合不合法。"
            ),
            None,
        )
    text = candidate["text"]
    source_text = candidate["source_text"]
    check_note = candidate["check_note"]
    if (
        not _non_empty(text)
        or (source_text is not None and not isinstance(source_text, str))
        or not _valid_check_note(check_note)
    ):
        return (
            None,
            _rejection(
                "unknown_note", asset_id, item_index, "invalid_value", "未知说明值不符合协议。"
            ),
            None,
        )
    region, degradation, error = _normalize_region(
        candidate["region"], asset_id, "unknown_note", item_index, dimensions
    )
    if error is not None:
        return None, _rejection("unknown_note", asset_id, item_index, "invalid_region", error), None
    return (
        PdfAcceptedUnknownNote(
            text=text, source_text=source_text, check_note=check_note.strip(), region=region
        ),
        None,
        degradation,
    )


def _normalize_region(raw, asset_id, item_kind, item_index, dimensions):
    if raw is None:
        return None, None, None
    if type(raw) is not dict or set(raw) != {"bbox_2d"}:
        return None, None, "region 结构不合法。"
    values = raw["bbox_2d"]
    if (
        type(values) is not list
        or len(values) != 4
        or any(isinstance(value, bool) or not isinstance(value, Real) for value in values)
        or any(not math.isfinite(float(value)) for value in values)
    ):
        return None, None, "region bbox_2d 必须包含四个有限数值。"
    left_norm, top_norm, right_norm, bottom_norm = (float(value) for value in values)
    if (
        any(
            value < 0 or value > _QWEN_GROUNDING_COORDINATE_MAX
            for value in (left_norm, top_norm, right_norm, bottom_norm)
        )
        or right_norm <= left_norm
        or bottom_norm <= top_norm
    ):
        return None, None, "region bbox_2d 必须是有效的 Qwen 归一坐标。"

    original = PdfRawRegion(
        bbox_2d=[left_norm, top_norm, right_norm, bottom_norm],
    )
    image_width, image_height = dimensions
    left = max(0, int(left_norm / _QWEN_GROUNDING_SCALE * image_width))
    top = max(0, int(top_norm / _QWEN_GROUNDING_SCALE * image_height))
    right = min(image_width, int(right_norm / _QWEN_GROUNDING_SCALE * image_width))
    bottom = min(image_height, int(bottom_norm / _QWEN_GROUNDING_SCALE * image_height))
    if right <= left or bottom <= top:
        return (
            None,
            PdfRegionDegradation(
                asset_id=asset_id,
                item_kind=item_kind,
                item_index=item_index,
                original_region=original,
                normalized_region=None,
                reason_code="empty_after_clip",
            ),
            None,
        )
    normalized = Region(x=left, y=top, width=right - left, height=bottom - top, unit="px")
    return normalized, None, None


def _valid_check_note(note: object) -> bool:
    return _non_empty(note)


def _non_empty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _rejection(level, asset_id, item_index, reason_code, message):
    return PdfModelRejection(
        level=level,
        asset_id=asset_id,
        item_index=item_index,
        reason_code=reason_code,
        message=message,
    )


def _envelope_rejection(reason_code: str, message: str) -> ParsedPdfBatch:
    return ParsedPdfBatch(
        accepted_assets=(),
        rejections=(
            PdfModelRejection(
                level="envelope",
                asset_id=None,
                item_index=None,
                reason_code=reason_code,
                message=message,
            ),
        ),
        region_degradations=(),
        envelope_rejected=True,
    )
