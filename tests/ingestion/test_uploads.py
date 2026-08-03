from __future__ import annotations

import io
import stat
import zipfile

import pytest

from flyboard.errors import ServiceError
from flyboard.ingestion import (
    MaterialStager,
    MaterialUploadStager,
    UploadedFileSource,
    UploadLimits,
)
from flyboard.models import MaterialKind


def _upload(path: str, content: bytes) -> UploadedFileSource:
    return UploadedFileSource(path, io.BytesIO(content))


def _archive(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def _gerber() -> bytes:
    return b"%FSLAX24Y24*%\n%MOMM*%\n%ADD10C,0.100*%\nD10*\nX0Y0D02*\nX1000Y1000D01*\nM02*\n"


def test_uploaded_directory_reuses_existing_classification_and_root_job(tmp_path) -> None:
    stager = MaterialUploadStager(MaterialStager(tmp_path / "storage"))

    result = stager.stage(
        "prj_test",
        "req_test",
        "directory",
        [
            _upload("top.gtl", _gerber()),
            _upload("notes.txt", b"customer note"),
        ],
    )

    assert [unit.input_ref for unit in result.units] == [".", "notes.txt"]
    assert result.units[0].kind == MaterialKind.GERBER_JOB
    assert result.units[1].kind == MaterialKind.UNSUPPORTED


def test_single_uploaded_design_file_keeps_single_file_job_identity(tmp_path) -> None:
    stager = MaterialUploadStager(MaterialStager(tmp_path / "storage"))

    result = stager.stage(
        "prj_test",
        "req_test",
        "files",
        [_upload("top.gtl", _gerber())],
    )

    assert len(result.units) == 1
    assert result.units[0].input_ref == "top.gtl"
    assert result.units[0].display_name == "top.gtl"
    assert result.units[0].kind == MaterialKind.GERBER_JOB


def test_zip_is_transport_only_and_expanded_files_enter_material_stager(tmp_path) -> None:
    stager = MaterialUploadStager(MaterialStager(tmp_path / "storage"))
    content = _archive([("docs/note.txt", b"note"), ("board/top.gtl", _gerber())])

    result = stager.stage(
        "prj_test",
        "req_test",
        "archive",
        [_upload("customer.zip", content)],
    )

    assert [unit.input_ref for unit in result.units] == ["board", "docs/note.txt"]
    assert result.units[0].kind == MaterialKind.GERBER_JOB
    assert all(not unit.display_name.endswith(".zip") for unit in result.units)


def test_upload_manifest_rejects_unsafe_paths_collisions_and_wrong_modes(tmp_path) -> None:
    stager = MaterialUploadStager(MaterialStager(tmp_path / "storage"))

    for mode, files in [
        ("directory", [_upload("../outside.txt", b"x")]),
        ("directory", [_upload("A.txt", b"a"), _upload("a.TXT", b"b")]),
        ("files", [_upload("customer.zip", _archive([("note.txt", b"note")]))]),
        ("archive", [_upload("one.zip", b"x"), _upload("two.zip", b"y")]),
    ]:
        with pytest.raises(ServiceError, match="invalid_upload_manifest"):
            stager.stage("prj_test", "req_test", mode, files)

    assert not (tmp_path / "storage" / "staging" / "prj_test").exists()


def test_upload_manifest_enforces_the_explicit_input_file_limit(tmp_path) -> None:
    limits = UploadLimits(max_input_files=1)
    stager = MaterialUploadStager(MaterialStager(tmp_path / "storage"), limits=limits)

    with pytest.raises(ServiceError, match="invalid_upload_manifest"):
        stager.stage(
            "prj_test",
            "req_test",
            "files",
            [_upload("one.txt", b"one"), _upload("two.txt", b"two")],
        )

    assert not (tmp_path / "storage" / "staging" / "prj_test").exists()


def test_zip_rejects_traversal_and_cleans_request_staging(tmp_path) -> None:
    stager = MaterialUploadStager(MaterialStager(tmp_path / "storage"))
    content = _archive([("../outside.txt", b"escape")])

    with pytest.raises(ServiceError, match="archive_unsafe"):
        stager.stage(
            "prj_test",
            "req_test",
            "archive",
            [_upload("unsafe.zip", content)],
        )

    assert not (tmp_path / "storage" / "staging" / "prj_test" / "req_test").exists()
    assert not (tmp_path / "outside.txt").exists()


def test_zip_rejects_symlink_member_and_cleans_staging(tmp_path) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        link = zipfile.ZipInfo("outside-link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../outside.txt")
    stager = MaterialUploadStager(MaterialStager(tmp_path / "storage"))

    with pytest.raises(ServiceError, match="archive_unsafe"):
        stager.stage(
            "prj_test",
            "req_test",
            "archive",
            [_upload("unsafe.zip", output.getvalue())],
        )

    assert not (tmp_path / "storage" / "staging" / "prj_test" / "req_test").exists()


def test_zip_enforces_declared_and_actual_expanded_limits(tmp_path) -> None:
    limits = UploadLimits(max_expanded_bytes=3, max_file_bytes=1024, max_compression_ratio=1000)
    stager = MaterialUploadStager(MaterialStager(tmp_path / "storage"), limits=limits)

    with pytest.raises(ServiceError, match="archive_limit_exceeded"):
        stager.stage(
            "prj_test",
            "req_test",
            "archive",
            [_upload("large.zip", _archive([("note.txt", b"four")]))],
        )

    assert not (tmp_path / "storage" / "staging" / "prj_test" / "req_test").exists()


def test_zip_explicit_directory_order_does_not_change_extraction(tmp_path) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("docs/note.txt", b"note")
        archive.writestr("docs/", b"")
    stager = MaterialUploadStager(MaterialStager(tmp_path / "storage"))

    result = stager.stage(
        "prj_test",
        "req_test",
        "archive",
        [_upload("ordered.zip", output.getvalue())],
    )

    assert [unit.input_ref for unit in result.units] == ["docs/note.txt"]
