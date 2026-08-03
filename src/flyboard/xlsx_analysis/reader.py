from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from flyboard.models import ErrorRecord, RunInputMaterial, ToolFinding

if TYPE_CHECKING:
    from flyboard.workflow import AnalysisContext

from .models import XlsxCellRecord, XlsxSheetRecord, XlsxWorkbookRecord

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CELL_REF = re.compile(r"^([A-Za-z]+)([1-9][0-9]*)$")
RANGE_REF = re.compile(r"^([A-Za-z]+[1-9][0-9]*)(?::([A-Za-z]+[1-9][0-9]*))?$")


@dataclass(frozen=True, slots=True)
class XlsxReadResult:
    workbook: XlsxWorkbookRecord
    cells: tuple[XlsxCellRecord, ...]
    errors: tuple[ErrorRecord, ...]
    findings: tuple[ToolFinding, ...]


class XlsxReader:
    """Deterministic OOXML reader for the first-version cell-only XLSX boundary."""

    def read(
        self,
        path: Path,
        material: RunInputMaterial,
        context: AnalysisContext,
    ) -> XlsxReadResult:
        try:
            with zipfile.ZipFile(path) as archive:
                return self._read_archive(archive, material, context)
        except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError, ValueError):
            error = context.diagnostics.record_error(
                code="xlsx_workbook_read_failed",
                severity="error",
                phase="branch_analysis",
                branch="xlsx",
                message="XLSX workbook 容器或基础关系无法读取。",
                source_material_ids=[material.material_id],
                artifact_paths=[],
                suggested_action="检查 workbook 是否为完整且未加密的 .xlsx 文件。",
            )
            return XlsxReadResult(
                workbook=XlsxWorkbookRecord(
                    source_material_id=material.material_id,
                    source_ref=material.source_ref,
                    outcome="failed",
                    date_system=None,
                    sheets=[],
                    error_ids=[error.error_id],
                ),
                cells=(),
                errors=(error,),
                findings=(),
            )

    def _read_archive(
        self,
        archive: zipfile.ZipFile,
        material: RunInputMaterial,
        context: AnalysisContext,
    ) -> XlsxReadResult:
        names = set(archive.namelist())
        workbook_root = _xml(archive, "xl/workbook.xml")
        relationships = _read_relationships(
            _xml(archive, "xl/_rels/workbook.xml.rels"),
            names,
        )
        shared_strings = _read_shared_strings(archive, names)
        number_formats = _read_number_formats(archive, names)
        workbook_pr = workbook_root.find(_q("workbookPr"))
        date_system = (
            "1904" if workbook_pr is not None and _xml_bool(workbook_pr.get("date1904")) else "1900"
        )
        sheets_parent = workbook_root.find(_q("sheets"))
        if sheets_parent is None:
            raise ValueError("workbook sheets are missing")

        records: list[XlsxSheetRecord] = []
        cells: list[XlsxCellRecord] = []
        errors: list[ErrorRecord] = []
        findings: list[ToolFinding] = []
        for sheet_index, sheet in enumerate(sheets_parent.findall(_q("sheet")), start=1):
            context.stop_signal.check_boundary()
            name = (sheet.get("name") or "").strip()
            if not name:
                raise ValueError("sheet name is missing")
            visibility = sheet.get("state", "visible")
            if visibility not in {"visible", "hidden", "veryHidden"}:
                raise ValueError("sheet visibility is invalid")
            rel_id = sheet.get(f"{{{DOC_REL_NS}}}id")
            target = relationships.get(rel_id or "")
            if target is None:
                record, error = self._failed_sheet(material, context, sheet_index, name, visibility)
                records.append(record)
                errors.append(error)
                continue
            try:
                sheet_record, sheet_cells, sheet_findings = self._read_sheet(
                    archive,
                    target,
                    material,
                    context,
                    sheet_index,
                    name,
                    visibility,
                    shared_strings,
                    number_formats,
                    date_system,
                )
            except (KeyError, ET.ParseError, ValueError, InvalidOperation):
                sheet_record, error = self._failed_sheet(
                    material, context, sheet_index, name, visibility
                )
                records.append(sheet_record)
                errors.append(error)
                continue
            records.append(sheet_record)
            cells.extend(sheet_cells)
            findings.extend(sheet_findings)

        return XlsxReadResult(
            workbook=XlsxWorkbookRecord(
                source_material_id=material.material_id,
                source_ref=material.source_ref,
                outcome="read",
                date_system=date_system,
                sheets=records,
                error_ids=[],
            ),
            cells=tuple(cells),
            errors=tuple(errors),
            findings=tuple(findings),
        )

    def _failed_sheet(
        self,
        material: RunInputMaterial,
        context: AnalysisContext,
        sheet_index: int,
        name: str,
        visibility: str,
    ) -> tuple[XlsxSheetRecord, ErrorRecord]:
        error = context.diagnostics.record_error(
            code="xlsx_sheet_read_failed",
            severity="error",
            phase="branch_analysis",
            branch="xlsx",
            message=f"XLSX sheet #{sheet_index} ({name}) 无法读取。",
            source_material_ids=[material.material_id],
            artifact_paths=[],
            suggested_action="检查 sheet XML 与 workbook 关系后重新登记资料。",
        )
        return (
            XlsxSheetRecord(
                sheet_index=sheet_index,
                name=name,
                visibility=visibility,
                outcome="failed",
                used_range=None,
                merged_ranges=[],
                hidden_rows=[],
                hidden_columns=[],
                formula_count=None,
                serialized_cell_count=None,
                tool_finding_ids=[],
                error_ids=[error.error_id],
            ),
            error,
        )

    def _read_sheet(
        self,
        archive: zipfile.ZipFile,
        target: str,
        material: RunInputMaterial,
        context: AnalysisContext,
        sheet_index: int,
        sheet_name: str,
        visibility: str,
        shared_strings: tuple[str, ...],
        number_formats: tuple[str, ...],
        date_system: str,
    ) -> tuple[XlsxSheetRecord, list[XlsxCellRecord], list[ToolFinding]]:
        root = _xml(archive, target)
        hidden_rows = sorted(
            {
                int(row.get("r"))
                for row in root.findall(f".//{_q('row')}")
                if _xml_bool(row.get("hidden")) and row.get("r") is not None
            }
        )
        hidden_columns: set[int] = set()
        for column in root.findall(f".//{_q('col')}"):
            if not _xml_bool(column.get("hidden")):
                continue
            minimum = int(column.get("min", "0"))
            maximum = int(column.get("max", "0"))
            if minimum < 1 or maximum < minimum:
                raise ValueError("hidden column range is invalid")
            hidden_columns.update(range(minimum, maximum + 1))

        merged_ranges: list[str] = []
        merged_by_anchor: dict[str, str] = {}
        merged_non_anchors: set[str] = set()
        range_points: list[tuple[int, int]] = []
        merges = root.find(_q("mergeCells"))
        if merges is not None:
            for merge in merges.findall(_q("mergeCell")):
                normalized, start, end = _normalize_range(merge.get("ref"))
                if normalized not in merged_ranges:
                    merged_ranges.append(normalized)
                anchor = _cell_address(*start)
                merged_by_anchor[anchor] = normalized
                range_points.extend((start, end))
                for row in range(start[0], end[0] + 1):
                    for column in range(start[1], end[1] + 1):
                        address = _cell_address(row, column)
                        if address != anchor:
                            merged_non_anchors.add(address)

        cells: list[XlsxCellRecord] = []
        formula_count = 0
        formula_cache_missing = False
        for cell in root.findall(f".//{_q('c')}"):
            raw_address = cell.get("r")
            row, column, address = _parse_address(raw_address)
            if address in merged_non_anchors:
                continue
            formula_element = cell.find(_q("f"))
            formula = None
            if formula_element is not None:
                formula_count += 1
                formula = (formula_element.text or "").strip()
                if not formula:
                    raise ValueError("formula text is missing")
            style_index = int(cell.get("s", "0"))
            if style_index < 0 or style_index >= len(number_formats):
                raise ValueError("cell style index is invalid")
            number_format = number_formats[style_index]
            record = _cell_record(
                cell,
                material_id=material.material_id,
                sheet_index=sheet_index,
                sheet_name=sheet_name,
                row=row,
                column=column,
                address=address,
                formula=formula,
                number_format=number_format,
                shared_strings=shared_strings,
                date_system=date_system,
                merged_range=merged_by_anchor.get(address),
                row_hidden=row in hidden_rows,
                column_hidden=column in hidden_columns,
            )
            if record.value_origin == "formula_without_cache":
                formula_cache_missing = True
            cells.append(record)
            range_points.append((row, column))

        cells.sort(key=lambda item: (item.row, item.column))
        findings: list[ToolFinding] = []
        finding_ids: list[str] = []
        if formula_cache_missing:
            finding = context.diagnostics.record_finding(
                producer="xlsx_inspector",
                code="xlsx_formula_cache_missing",
                kind="coverage_gap",
                message=f"XLSX sheet #{sheet_index} 存在没有缓存值的公式。",
                source_material_ids=[material.material_id],
                evidence_ids=[],
                error_id=None,
            )
            findings.append(finding)
            finding_ids.append(finding.finding_id)

        used_range = _used_range(range_points)
        return (
            XlsxSheetRecord(
                sheet_index=sheet_index,
                name=sheet_name,
                visibility=visibility,
                outcome="read",
                used_range=used_range,
                merged_ranges=merged_ranges,
                hidden_rows=hidden_rows,
                hidden_columns=sorted(hidden_columns),
                formula_count=formula_count,
                serialized_cell_count=len(cells),
                tool_finding_ids=finding_ids,
                error_ids=[],
            ),
            cells,
            findings,
        )


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(archive.read(name))


def _q(local_name: str) -> str:
    return f"{{{MAIN_NS}}}{local_name}"


def _read_relationships(root: ET.Element, archive_names: set[str]) -> dict[str, str]:
    relationships: dict[str, str] = {}
    for relation in root.findall(f"{{{PKG_REL_NS}}}Relationship"):
        rel_id = relation.get("Id")
        target = relation.get("Target")
        if not rel_id or not target or relation.get("TargetMode") == "External":
            continue
        normalized = _normalize_archive_target("xl", target)
        if normalized in archive_names:
            relationships[rel_id] = normalized
    return relationships


def _normalize_archive_target(parent: str, target: str) -> str:
    if target.startswith("/"):
        normalized = posixpath.normpath(target.lstrip("/"))
    else:
        normalized = posixpath.normpath(posixpath.join(parent, target))
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("relationship target escapes workbook")
    return normalized


def _read_shared_strings(archive: zipfile.ZipFile, names: set[str]) -> tuple[str, ...]:
    if "xl/sharedStrings.xml" not in names:
        return ()
    root = _xml(archive, "xl/sharedStrings.xml")
    return tuple(
        "".join(node.text or "" for node in item.iter(_q("t"))).replace("\r\n", "\n")
        for item in root.findall(_q("si"))
    )


def _read_number_formats(archive: zipfile.ZipFile, names: set[str]) -> tuple[str, ...]:
    if "xl/styles.xml" not in names:
        return ("General",)
    root = _xml(archive, "xl/styles.xml")
    custom = {
        int(item.get("numFmtId", "-1")): item.get("formatCode", "General")
        for item in root.findall(f".//{_q('numFmt')}")
    }
    xfs = root.find(_q("cellXfs"))
    if xfs is None:
        return ("General",)
    return tuple(
        custom.get(
            int(xf.get("numFmtId", "0")), _builtin_number_format(int(xf.get("numFmtId", "0")))
        )
        for xf in xfs.findall(_q("xf"))
    ) or ("General",)


def _builtin_number_format(number_format_id: int) -> str:
    formats = {
        9: "0%",
        10: "0.00%",
        14: "mm-dd-yy",
        15: "d-mmm-yy",
        16: "d-mmm",
        17: "mmm-yy",
        18: "h:mm AM/PM",
        19: "h:mm:ss AM/PM",
        20: "h:mm",
        21: "h:mm:ss",
        22: "m/d/yy h:mm",
        45: "mm:ss",
        46: "[h]:mm:ss",
        47: "mmss.0",
    }
    return formats.get(number_format_id, "General")


def _cell_record(
    cell: ET.Element,
    *,
    material_id: str,
    sheet_index: int,
    sheet_name: str,
    row: int,
    column: int,
    address: str,
    formula: str | None,
    number_format: str,
    shared_strings: tuple[str, ...],
    date_system: str,
    merged_range: str | None,
    row_hidden: bool,
    column_hidden: bool,
) -> XlsxCellRecord:
    cell_type = cell.get("t")
    value_element = cell.find(_q("v"))
    value_text = value_element.text if value_element is not None else None
    inline = "".join(node.text or "" for node in cell.iter(_q("t")))
    if formula is not None and value_text is None:
        return XlsxCellRecord(
            source_material_id=material_id,
            sheet_index=sheet_index,
            sheet_name=sheet_name,
            address=address,
            row=row,
            column=column,
            value_kind="unavailable",
            raw_value=None,
            normalized_value=None,
            display_text=None,
            formula=formula,
            value_origin="formula_without_cache",
            number_format_code=None if number_format == "General" else number_format,
            merged_range=merged_range,
            row_hidden=row_hidden,
            column_hidden=column_hidden,
        )

    raw_value: str | None
    normalized: str | bool | None
    display: str | None
    value_kind: str
    if cell_type == "s":
        if value_text is None:
            raise ValueError("shared string index is missing")
        index = int(value_text)
        if index < 0 or index >= len(shared_strings):
            raise ValueError("shared string index is invalid")
        raw_value = shared_strings[index]
        normalized = raw_value
        display = raw_value
        value_kind = "text"
    elif cell_type == "inlineStr":
        raw_value = inline.replace("\r\n", "\n")
        normalized = raw_value
        display = raw_value
        value_kind = "text" if raw_value else "empty"
    elif cell_type == "b":
        if value_text not in {"0", "1"}:
            raise ValueError("boolean value is invalid")
        raw_value = value_text
        normalized = value_text == "1"
        display = "TRUE" if normalized else "FALSE"
        value_kind = "boolean"
    elif cell_type == "e":
        raw_value = value_text or ""
        normalized = raw_value
        display = raw_value
        value_kind = "error"
    elif cell_type in {"str", "d"}:
        raw_value = (value_text or "").replace("\r\n", "\n")
        normalized = raw_value
        display = raw_value
        value_kind = "text" if cell_type == "str" else _iso_value_kind(raw_value)
    elif value_text is None:
        raw_value = None
        normalized = None
        display = ""
        value_kind = "empty"
    else:
        raw_value = value_text
        value_kind, normalized, display = _numeric_value(value_text, number_format, date_system)

    return XlsxCellRecord(
        source_material_id=material_id,
        sheet_index=sheet_index,
        sheet_name=sheet_name,
        address=address,
        row=row,
        column=column,
        value_kind=value_kind,
        raw_value=raw_value,
        normalized_value=normalized,
        display_text=display,
        formula=formula,
        value_origin="formula_cache" if formula is not None else "literal",
        number_format_code=None if number_format == "General" else number_format,
        merged_range=merged_range,
        row_hidden=row_hidden,
        column_hidden=column_hidden,
    )


def _numeric_value(raw: str, number_format: str, date_system: str) -> tuple[str, str, str]:
    value = Decimal(raw)
    normalized_decimal = format(value, "f")
    format_lower = _strip_format_literals(number_format).lower()
    if "%" in format_lower:
        displayed = f"{format(value * 100, 'f')}%"
        return "percentage", displayed, displayed
    has_date = any(marker in format_lower for marker in ("y", "d"))
    has_time = any(marker in format_lower for marker in ("h", "s"))
    if has_date or has_time:
        epoch = datetime(1904, 1, 1) if date_system == "1904" else datetime(1899, 12, 30)
        moment = epoch + timedelta(days=float(value))
        if has_date and has_time:
            rendered = moment.isoformat(timespec="seconds")
            return "datetime", rendered, rendered
        if has_date:
            rendered = moment.date().isoformat()
            return "date", rendered, rendered
        rendered = moment.time().isoformat(timespec="seconds")
        return "time", rendered, rendered
    return "number", normalized_decimal, normalized_decimal


def _strip_format_literals(number_format: str) -> str:
    without_quotes = re.sub(r'"[^"]*"', "", number_format)
    return re.sub(r"\[[^\]]*\]", "", without_quotes)


def _iso_value_kind(raw: str) -> str:
    if "T" in raw:
        return "datetime"
    if ":" in raw:
        return "time"
    return "date"


def _parse_address(raw: str | None) -> tuple[int, int, str]:
    if raw is None:
        raise ValueError("cell address is missing")
    normalized = raw.replace("$", "").upper()
    match = CELL_REF.fullmatch(normalized)
    if match is None:
        raise ValueError("cell address is invalid")
    row = int(match.group(2))
    column = _column_number(match.group(1))
    return row, column, normalized


def _normalize_range(raw: str | None) -> tuple[str, tuple[int, int], tuple[int, int]]:
    if raw is None:
        raise ValueError("merge range is missing")
    normalized = raw.replace("$", "").upper()
    match = RANGE_REF.fullmatch(normalized)
    if match is None:
        raise ValueError("merge range is invalid")
    start_row, start_column, start_address = _parse_address(match.group(1))
    end_row, end_column, end_address = _parse_address(match.group(2) or match.group(1))
    if end_row < start_row or end_column < start_column:
        raise ValueError("merge range is reversed")
    return (
        f"{start_address}:{end_address}" if start_address != end_address else start_address,
        (start_row, start_column),
        (end_row, end_column),
    )


def _column_number(letters: str) -> int:
    value = 0
    for letter in letters:
        value = value * 26 + ord(letter) - ord("A") + 1
    return value


def _column_letters(column: int) -> str:
    letters = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _cell_address(row: int, column: int) -> str:
    return f"{_column_letters(column)}{row}"


def _used_range(points: list[tuple[int, int]]) -> str | None:
    if not points:
        return None
    rows = [item[0] for item in points]
    columns = [item[1] for item in points]
    start = _cell_address(min(rows), min(columns))
    end = _cell_address(max(rows), max(columns))
    return start if start == end else f"{start}:{end}"


def _xml_bool(value: str | None) -> bool:
    return value in {"1", "true", "True"}
