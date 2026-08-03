from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import ValidationError

from flyboard.models import FieldTable

from .models import (
    XlsxCellRecord,
    XlsxRejection,
    XlsxSubmittedField,
    XlsxSubmittedUnknownNote,
)

MARKER = "SUBMIT_XLSX:"
ADDRESS = re.compile(r"^[A-Z]+[1-9][0-9]*$")


@dataclass(frozen=True, slots=True)
class ParsedSubmission:
    fields: tuple[XlsxSubmittedField, ...]
    unknown_notes: tuple[XlsxSubmittedUnknownNote, ...]
    rejections: tuple[XlsxRejection, ...]


class XlsxSubmissionParser:
    def parse(
        self,
        response_text: str,
        *,
        field_table: FieldTable,
        cells: tuple[XlsxCellRecord, ...],
    ) -> ParsedSubmission:
        raw, envelope_rejection = _parse_envelope(response_text)
        if envelope_rejection is not None:
            return ParsedSubmission((), (), (envelope_rejection,))

        by_source = {
            (cell.source_material_id, cell.sheet_index, cell.address): cell for cell in cells
        }
        field_definitions = {item.field_code: item for item in field_table.fields}
        accepted_fields: list[XlsxSubmittedField] = []
        accepted_unknowns: list[XlsxSubmittedUnknownNote] = []
        rejections: list[XlsxRejection] = []

        for index, item in enumerate(raw["fields"]):
            try:
                submitted = XlsxSubmittedField.model_validate(item)
                definition = field_definitions.get(submitted.field_code)
                if definition is None or definition.producer_scope == "integration_only":
                    raise ValueError("field_code is not available to XLSX")
                if submitted.unit != definition.unit:
                    raise ValueError("unit does not match the field table")
                source_cells = _validate_source_cells(
                    submitted.source_cells,
                    submitted.source_material_id,
                    submitted.sheet_index,
                    by_source,
                    allow_empty=False,
                )
                check_note = _validate_check_note(submitted.check_note)
                submitted = submitted.model_copy(
                    update={"source_cells": source_cells, "check_note": check_note}
                )
                accepted_fields.append(submitted)
            except (ValidationError, ValueError, TypeError):
                rejections.append(
                    XlsxRejection(
                        level="field",
                        item_index=index,
                        reason_code="field_item_invalid",
                        message="字段项未通过字段、单位、来源单元格或业务备注合同。",
                    )
                )

        for index, item in enumerate(raw["unknown_notes"]):
            try:
                submitted = XlsxSubmittedUnknownNote.model_validate(item)
                source_cells = _validate_source_cells(
                    submitted.source_cells,
                    submitted.source_material_id,
                    submitted.sheet_index,
                    by_source,
                    allow_empty=True,
                )
                check_note = _validate_check_note(submitted.check_note)
                submitted = submitted.model_copy(
                    update={"source_cells": source_cells, "check_note": check_note}
                )
                accepted_unknowns.append(submitted)
            except (ValidationError, ValueError, TypeError):
                rejections.append(
                    XlsxRejection(
                        level="unknown_note",
                        item_index=index,
                        reason_code="unknown_note_item_invalid",
                        message="未知说明项未通过值、来源单元格或业务备注合同。",
                    )
                )

        if not accepted_fields and not accepted_unknowns and not rejections:
            rejections.append(
                XlsxRejection(
                    level="envelope",
                    item_index=None,
                    reason_code="empty_submission",
                    message="模型提交没有字段或未知说明。",
                )
            )
        return ParsedSubmission(
            tuple(accepted_fields),
            tuple(accepted_unknowns),
            tuple(rejections),
        )


def _parse_envelope(response_text: str) -> tuple[dict[str, list[object]], XlsxRejection | None]:
    if not response_text.startswith(MARKER):
        return {}, _envelope_rejection("missing_marker", "响应必须以唯一 SUBMIT_XLSX 标记开始。")
    payload = response_text[len(MARKER) :].strip()
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return {}, _envelope_rejection("invalid_json", "SUBMIT_XLSX 内容不是完整 JSON。")
    if not isinstance(decoded, dict) or set(decoded) != {"fields", "unknown_notes"}:
        return {}, _envelope_rejection(
            "invalid_envelope", "SUBMIT_XLSX 顶层必须且只能包含 fields 与 unknown_notes。"
        )
    if not isinstance(decoded["fields"], list) or not isinstance(decoded["unknown_notes"], list):
        return {}, _envelope_rejection("invalid_arrays", "fields 与 unknown_notes 必须是数组。")
    return decoded, None


def _envelope_rejection(reason_code: str, message: str) -> XlsxRejection:
    return XlsxRejection(
        level="envelope",
        item_index=None,
        reason_code=reason_code,
        message=message,
    )


def _validate_source_cells(
    addresses: list[str],
    source_material_id: str,
    sheet_index: int,
    by_source: dict[tuple[str, int, str], XlsxCellRecord],
    *,
    allow_empty: bool,
) -> list[str]:
    normalized: list[str] = []
    for address in addresses:
        if not isinstance(address, str):
            raise TypeError("cell address must be a string")
        item = address.replace("$", "").upper()
        if ADDRESS.fullmatch(item) is None:
            raise ValueError("cell address is invalid")
        if item in normalized:
            raise ValueError("source cells must be unique")
        cell = by_source.get((source_material_id, sheet_index, item))
        if cell is None:
            raise ValueError("source cell does not exist")
        if cell.value_kind == "unavailable" or (cell.value_kind == "empty" and not allow_empty):
            raise ValueError("source cell is unavailable for this submission kind")
        normalized.append(item)
    return sorted(normalized, key=_address_key)


def _validate_check_note(check_note: str) -> str:
    note = check_note.strip()
    if not note:
        raise ValueError("check_note must not be blank")
    return note


def _address_key(address: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", address)
    if match is None:
        raise ValueError("cell address is invalid")
    column = 0
    for letter in match.group(1):
        column = column * 26 + ord(letter) - ord("A") + 1
    return int(match.group(2)), column
