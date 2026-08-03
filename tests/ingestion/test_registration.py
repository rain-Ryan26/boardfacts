from __future__ import annotations

import zipfile

import fitz
import pytest
from PIL import Image

from flyboard.ingestion import MaterialStager
from flyboard.models import MaterialKind, UnsupportedReasonCode


def make_pdf(path) -> None:
    document = fitz.open()
    document.new_page()
    document.save(path)
    document.close()


def make_xlsx(path) -> None:
    with zipfile.ZipFile(path, "w") as workbook:
        workbook.writestr("[Content_Types].xml", "<Types/>")
        workbook.writestr("xl/workbook.xml", "<workbook/>")


def make_gerber(path) -> None:
    path.write_text(
        "G04 minimal*\n%FSLAX24Y24*%\n%MOMM*%\n%ADD10C,0.100*%\nD10*\n"
        "X000000Y000000D02*\nX010000Y010000D01*\nM02*\n",
        encoding="ascii",
    )


def test_stager_classifies_supported_and_invalid_files(tmp_path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    make_pdf(inputs / "drawing.pdf")
    make_xlsx(inputs / "form.xlsx")
    Image.new("RGB", (2, 2), "red").save(inputs / "board.png")
    (inputs / "bad.pdf").write_text("not pdf", encoding="utf-8")

    stager = MaterialStager(tmp_path / "storage")
    result = stager.stage("prj_test", "req_test", inputs)
    by_ref = {unit.input_ref: unit for unit in result.units}
    assert by_ref["drawing.pdf"].kind == MaterialKind.PDF
    assert by_ref["form.xlsx"].kind == MaterialKind.SPREADSHEET
    assert by_ref["board.png"].kind == MaterialKind.IMAGE
    assert by_ref["bad.pdf"].unsupported_reason == UnsupportedReasonCode.PDF_INVALID
    assert result.failures == ()


def test_stager_groups_root_gerber_once_and_keeps_other_files_separate(tmp_path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    make_gerber(inputs / "top.gtl")
    (inputs / "notes.txt").write_text("notes", encoding="utf-8")

    result = MaterialStager(tmp_path / "storage").stage("prj_test", "req_test", inputs)
    assert [unit.input_ref for unit in result.units] == [".", "notes.txt"]
    assert result.units[0].kind == MaterialKind.GERBER_JOB
    assert [kind.value for kind in result.units[0].member_kinds] == ["gerber"]
    assert result.units[1].kind == MaterialKind.UNSUPPORTED


def test_content_first_discovery_recognizes_drill_with_txt_extension(tmp_path) -> None:
    source = tmp_path / "holes.txt"
    source.write_text(
        "M48\nMETRIC,TZ\nT01C0.800\n%\nT01\nX1.000Y1.000\nM30\n",
        encoding="ascii",
    )
    result = MaterialStager(tmp_path / "storage").stage("prj_test", "req_test", source)
    assert len(result.units) == 1
    assert result.units[0].kind == MaterialKind.GERBER_JOB
    assert [kind.value for kind in result.units[0].member_kinds] == ["drill"]


def test_gtd_requires_content_markers_and_links_within_its_job(tmp_path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    make_gerber(inputs / "top.gtl")
    (inputs / "job.gtd").write_text(
        "{Program demo}\n{Layers {Layer 1 {File top.gtl}}}", encoding="utf-8"
    )
    (inputs / "invalid.gtd").write_text("plain text", encoding="utf-8")

    result = MaterialStager(tmp_path / "storage").stage("prj_test", "req_test", inputs)
    by_ref = {unit.input_ref: unit for unit in result.units}
    assert by_ref["."].kind == MaterialKind.GERBER_JOB
    assert [kind.value for kind in by_ref["."].member_kinds] == ["gerber", "gtd"]
    assert by_ref["invalid.gtd"].unsupported_reason == (
        UnsupportedReasonCode.DESIGN_FILE_UNRECOGNIZED
    )


def test_logical_unit_target_collision_fails_before_copy(tmp_path, monkeypatch) -> None:
    from flyboard.ingestion.registration import LogicalUnit, UnitMember

    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    unit = LogicalUnit(
        input_ref=".",
        display_name="job",
        kind=MaterialKind.GERBER_JOB,
        members=(
            UnitMember(first, "Layer.GBR"),
            UnitMember(second, "layer.gbr"),
        ),
        directory_source=True,
    )
    monkeypatch.setattr("os.path.normcase", lambda value: value.casefold())
    target = tmp_path / "unit"

    with pytest.raises(FileExistsError):
        MaterialStager._copy_unit(unit, target)
    assert not target.exists()
