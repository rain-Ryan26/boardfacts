import zipfile
from datetime import datetime, timezone

from flyboard.models import (
    BranchCode,
    ErrorRecord,
    MaterialKind,
    PhaseCode,
    RunInputMaterial,
    Severity,
    ToolFinding,
    ToolFindingKind,
)
from flyboard.xlsx_analysis.reader import XlsxReader

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


class ReaderDiagnostics:
    def __init__(self) -> None:
        self.error_count = 0
        self.finding_count = 0

    def record_error(self, **values):
        self.error_count += 1
        return ErrorRecord(
            error_id=f"err_00000000-0000-4000-8000-{self.error_count:012x}",
            code=values["code"],
            severity=Severity(values["severity"]),
            phase=PhaseCode(values["phase"]) if values["phase"] is not None else None,
            branch=BranchCode(values["branch"]) if values["branch"] is not None else None,
            message=values["message"],
            source_material_ids=values["source_material_ids"],
            artifact_paths=values["artifact_paths"],
            suggested_action=values["suggested_action"],
        )

    def record_finding(self, **values):
        self.finding_count += 1
        return ToolFinding(
            finding_id=f"finding_00000000-0000-4000-8000-{self.finding_count:012x}",
            producer=values["producer"],
            code=values["code"],
            kind=ToolFindingKind(values["kind"]),
            message=values["message"],
            source_material_ids=values["source_material_ids"],
            evidence_ids=values["evidence_ids"],
            error_id=values["error_id"],
        )


class ReaderStopSignal:
    @staticmethod
    def check_boundary() -> None:
        return None


class ReaderContext:
    def __init__(self) -> None:
        self.diagnostics = ReaderDiagnostics()
        self.stop_signal = ReaderStopSignal()


def material() -> RunInputMaterial:
    return RunInputMaterial(
        material_id="mat_1",
        material_order=1,
        kind=MaterialKind.SPREADSHEET,
        source_ref="sources/mat_1/input.xlsx",
        sha256="0" * 64,
        registered_at=NOW,
    )


def write_combination_workbook(path) -> None:
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <workbookPr date1904="0"/>
  <sheets>
    <sheet name="组合" sheetId="1" r:id="rId1"/>
    <sheet name="空表" sheetId="2" state="hidden" r:id="rId2"/>
    <sheet name="损坏表" sheetId="3" state="veryHidden" r:id="rId3"/>
  </sheets>
</workbook>"""
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/>
</Relationships>"""
    shared_strings = """<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="1" uniqueCount="1">
  <si><t>成品要求</t></si>
</sst>"""
    sheet1 = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:D2"/>
  <cols><col min="2" max="2" hidden="1"/></cols>
  <sheetData>
    <row r="1">
      <c r="A1" t="s"><v>0</v></c>
      <c r="B1" t="inlineStr"><is><t>合并区从属值</t></is></c>
    </row>
    <row r="2" hidden="1">
      <c r="A2"><f>1+1</f><v>2</v></c>
      <c r="B2"><v>7</v></c>
      <c r="C2"><f>A2+B2</f></c>
      <c r="D2"/>
    </row>
  </sheetData>
  <mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells>
</worksheet>"""
    sheet2 = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData/>
</worksheet>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/sharedStrings.xml", shared_strings)
        archive.writestr("xl/worksheets/sheet1.xml", sheet1)
        archive.writestr("xl/worksheets/sheet2.xml", sheet2)
        archive.writestr("xl/worksheets/sheet3.xml", "<worksheet")


def test_reader_covers_canonical_features_and_keeps_partial_sheet_failure(tmp_path) -> None:
    workbook_path = tmp_path / "combination.xlsx"
    write_combination_workbook(workbook_path)

    result = XlsxReader().read(workbook_path, material(), ReaderContext())

    assert result.workbook.outcome == "read"
    assert [sheet.visibility for sheet in result.workbook.sheets] == [
        "visible",
        "hidden",
        "veryHidden",
    ]
    assert [sheet.outcome for sheet in result.workbook.sheets] == ["read", "read", "failed"]

    combined, empty, failed = result.workbook.sheets
    assert combined.used_range == "A1:D2"
    assert combined.merged_ranges == ["A1:B1"]
    assert combined.hidden_rows == [2]
    assert combined.hidden_columns == [2]
    assert combined.formula_count == 2
    assert combined.serialized_cell_count == 5
    assert empty.used_range is None
    assert empty.serialized_cell_count == 0
    assert failed.error_ids == [result.errors[0].error_id]
    assert [error.code for error in result.errors] == ["xlsx_sheet_read_failed"]

    by_address = {cell.address: cell for cell in result.cells}
    assert set(by_address) == {"A1", "A2", "B2", "C2", "D2"}
    assert by_address["A1"].display_text == "成品要求"
    assert by_address["A1"].merged_range == "A1:B1"
    assert by_address["A2"].value_origin == "formula_cache"
    assert by_address["A2"].row_hidden is True
    assert by_address["B2"].column_hidden is True
    assert by_address["C2"].value_origin == "formula_without_cache"
    assert by_address["C2"].value_kind == "unavailable"
    assert by_address["D2"].value_kind == "empty"
    assert [finding.code for finding in result.findings] == ["xlsx_formula_cache_missing"]
