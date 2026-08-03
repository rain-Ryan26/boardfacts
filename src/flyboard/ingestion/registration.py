from __future__ import annotations

import hashlib
import os
import posixpath
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

import fitz
from PIL import Image, UnidentifiedImageError

from flyboard.errors import ServiceError
from flyboard.models import (
    MaterialKind,
    MemberKind,
    ProjectMaterial,
    RegistrationFailureReason,
    RunInputMaterial,
    UnsupportedReasonCode,
)
from flyboard.repositories._filesystem import ensure_no_reparse_points, managed_child
from flyboard.repositories._sources import resolve_material_source

from .gerber_discovery import DesignIdentification, GerberFileDiscovery

_IMAGE_SUFFIXES = {".jpeg": "JPEG", ".jpg": "JPEG", ".png": "PNG"}
_SPREADSHEET_SUFFIXES = {".csv", ".ods", ".xls", ".xlsb", ".xlsm", ".xltx"}


@dataclass(frozen=True, slots=True)
class RegistrationFailure:
    input_ref: str
    reason: RegistrationFailureReason


@dataclass(frozen=True, slots=True)
class UnitMember:
    source: Path
    target_ref: str
    kind: MemberKind | None = None
    references: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LogicalUnit:
    input_ref: str
    display_name: str
    kind: MaterialKind
    members: tuple[UnitMember, ...]
    member_kinds: tuple[MemberKind, ...] = ()
    unsupported_reason: UnsupportedReasonCode | None = None
    directory_source: bool = False


@dataclass(frozen=True, slots=True)
class StagedUnit:
    input_ref: str
    display_name: str
    kind: MaterialKind
    staging_root: Path
    fingerprint: str
    member_kinds: tuple[MemberKind, ...] = ()
    unsupported_reason: UnsupportedReasonCode | None = None
    directory_source: bool = False


@dataclass(frozen=True, slots=True)
class StagingResult:
    request_root: Path
    units: tuple[StagedUnit, ...]
    failures: tuple[RegistrationFailure, ...]


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _job_digest(files: Iterable[tuple[str, Path]]) -> str:
    digest = hashlib.sha256()
    for relative, path in sorted(files):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_file_digest(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def fingerprint_staged(unit: LogicalUnit, staging_root: Path) -> str:
    if unit.directory_source:
        return _job_digest(
            (member.target_ref, staging_root / member.target_ref) for member in unit.members
        )
    member = unit.members[0]
    digest = hashlib.sha256()
    digest.update(unit.kind.value.encode("ascii"))
    digest.update(b"\0")
    digest.update(member.target_ref.casefold().encode("utf-8"))
    digest.update(b"\0")
    digest.update(_file_digest(staging_root / member.target_ref).encode("ascii"))
    return digest.hexdigest()


def _managed_source_files(project_root: Path, source: Path) -> list[tuple[str, Path]]:
    ensure_no_reparse_points(project_root, source)
    files: list[tuple[str, Path]] = []
    for current, directories, names in os.walk(source, followlinks=False):
        current_path = Path(current)
        ensure_no_reparse_points(project_root, current_path)
        directories.sort(key=str.casefold)
        names.sort(key=str.casefold)
        for name in (*directories, *names):
            child = current_path / name
            if _is_reparse(child):
                raise ServiceError("managed_path_reparse_point")
        files.extend(
            (path.relative_to(source).as_posix(), path)
            for path in (current_path / name for name in names)
            if path.is_file()
        )
    return files


def material_fingerprint(project_root: Path, material: ProjectMaterial) -> str:
    source = resolve_material_source(project_root, material)
    if source.is_dir():
        return _job_digest(_managed_source_files(project_root, source))
    digest = hashlib.sha256()
    digest.update(material.kind.value.encode("ascii"))
    digest.update(b"\0")
    digest.update(source.name.casefold().encode("utf-8"))
    digest.update(b"\0")
    digest.update(_file_digest(source).encode("ascii"))
    return digest.hexdigest()


def material_sha256_from_source(
    source: Path,
    material: ProjectMaterial | RunInputMaterial,
) -> str:
    """Hash a source already resolved by the repository's managed resolver."""
    if source.is_dir():
        return _job_digest(_managed_source_files(source, source))
    return _file_digest(source)


def material_sha256(project_root: Path, material: ProjectMaterial) -> str:
    source = resolve_material_source(project_root, material)
    return material_sha256_from_source(source, material)


class MaterialStager:
    def __init__(self, storage_root: Path, discovery: GerberFileDiscovery | None = None) -> None:
        self.storage_root = storage_root
        self.staging_root = storage_root / "staging"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        ensure_no_reparse_points(storage_root, self.staging_root)
        self.discovery = discovery or GerberFileDiscovery()

    def stage(self, project_id: str, request_id: str, input_path: Path) -> StagingResult:
        request_root = self.create_request_root(project_id, request_id)
        return self.stage_prepared(request_root, input_path)

    def create_request_root(self, project_id: str, request_id: str) -> Path:
        request_root = managed_child(self.staging_root, project_id, request_id)
        if request_root.exists():
            raise ServiceError("staging_request_conflict")
        request_root.mkdir(parents=True)
        return request_root

    def stage_prepared(self, request_root: Path, input_path: Path) -> StagingResult:
        try:
            candidates, initial_failures, single_file = self._enumerate(input_path)
            units, failures = self._classify(candidates, initial_failures, single_file)
            staged: list[StagedUnit] = []
            for index, unit in enumerate(units, start=1):
                unit_root = request_root / f"unit_{index:04d}"
                try:
                    self._copy_unit(unit, unit_root)
                    staged.append(
                        StagedUnit(
                            input_ref=unit.input_ref,
                            display_name=unit.display_name,
                            kind=unit.kind,
                            staging_root=unit_root,
                            fingerprint=fingerprint_staged(unit, unit_root),
                            member_kinds=unit.member_kinds,
                            unsupported_reason=unit.unsupported_reason,
                            directory_source=unit.directory_source,
                        )
                    )
                except _SourceChanged:
                    shutil.rmtree(unit_root, ignore_errors=True)
                    failures.append(
                        RegistrationFailure(
                            unit.input_ref,
                            RegistrationFailureReason.SOURCE_CHANGED_DURING_REGISTRATION,
                        )
                    )
                except OSError:
                    shutil.rmtree(unit_root, ignore_errors=True)
                    failures.append(
                        RegistrationFailure(
                            unit.input_ref, RegistrationFailureReason.SOURCE_COPY_FAILED
                        )
                    )
            return StagingResult(request_root, tuple(staged), tuple(failures))
        except Exception:
            shutil.rmtree(request_root, ignore_errors=True)
            raise

    def _enumerate(
        self, input_path: Path
    ) -> tuple[list[tuple[str, Path]], list[RegistrationFailure], bool]:
        path = input_path.expanduser()
        if not path.exists():
            raise ServiceError("input_path_not_found")
        if path.is_file():
            ref = path.name
            if _is_reparse(path):
                return (
                    [],
                    [RegistrationFailure(ref, RegistrationFailureReason.SOURCE_UNREADABLE)],
                    True,
                )
            return [(ref, path)], [], True
        if not path.is_dir() or _is_reparse(path):
            raise ServiceError("input_path_invalid")
        candidates: list[tuple[str, Path]] = []
        failures: list[RegistrationFailure] = []
        for current, directories, files in os.walk(path, followlinks=False):
            current_path = Path(current)
            safe_directories: list[str] = []
            for name in sorted(directories, key=str.casefold):
                child = current_path / name
                if _is_reparse(child):
                    failures.append(
                        RegistrationFailure(
                            child.relative_to(path).as_posix(),
                            RegistrationFailureReason.SOURCE_UNREADABLE,
                        )
                    )
                else:
                    safe_directories.append(name)
            directories[:] = safe_directories
            for name in sorted(files, key=str.casefold):
                child = current_path / name
                ref = child.relative_to(path).as_posix()
                if _is_reparse(child) or not child.is_file():
                    failures.append(
                        RegistrationFailure(ref, RegistrationFailureReason.SOURCE_UNREADABLE)
                    )
                else:
                    candidates.append((ref, child))
        candidates.sort(key=lambda item: item[0])
        failures.sort(key=lambda item: item.input_ref)
        return candidates, failures, False

    def _classify(
        self,
        candidates: list[tuple[str, Path]],
        failures: list[RegistrationFailure],
        single_file: bool,
    ) -> tuple[list[LogicalUnit], list[RegistrationFailure]]:
        ordinary: list[LogicalUnit] = []
        design: list[tuple[str, Path, DesignIdentification]] = []
        for ref, path in candidates:
            try:
                unit, identification = self._classify_file(ref, path)
            except OSError:
                failures.append(
                    RegistrationFailure(ref, RegistrationFailureReason.SOURCE_UNREADABLE)
                )
                continue
            except Exception:
                failures.append(
                    RegistrationFailure(ref, RegistrationFailureReason.CLASSIFICATION_FAILED)
                )
                continue
            if identification is not None and identification.member_kind is not None:
                design.append((ref, path, identification))
            elif unit is not None:
                ordinary.append(unit)
        if single_file and design:
            ref, path, identification = design[0]
            units = [self._build_job(ref, ((ref, path, identification),), boundary=None)]
        else:
            units = self._group_directory_jobs(design)
        units.extend(ordinary)
        units.sort(key=lambda item: (item.input_ref != ".", item.input_ref))
        failures.sort(key=lambda item: item.input_ref)
        return units, failures

    def _classify_file(
        self, ref: str, path: Path
    ) -> tuple[LogicalUnit | None, DesignIdentification | None]:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            try:
                with fitz.open(path) as document:
                    if not document.is_pdf:
                        raise ValueError("not PDF")
                return self._file_unit(ref, path, MaterialKind.PDF), None
            except (fitz.FileDataError, ValueError, RuntimeError):
                return self._unsupported(ref, path, UnsupportedReasonCode.PDF_INVALID), None
        if suffix == ".xlsx":
            try:
                with zipfile.ZipFile(path) as workbook:
                    names = set(workbook.namelist())
                    if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                        raise ValueError("missing workbook parts")
                return self._file_unit(ref, path, MaterialKind.SPREADSHEET), None
            except (zipfile.BadZipFile, KeyError, ValueError):
                return self._unsupported(ref, path, UnsupportedReasonCode.XLSX_INVALID), None
        if suffix in _IMAGE_SUFFIXES:
            try:
                with Image.open(path) as image:
                    actual = image.format
                    image.verify()
                if actual != _IMAGE_SUFFIXES[suffix]:
                    return self._unsupported(
                        ref, path, UnsupportedReasonCode.IMAGE_FORMAT_MISMATCH
                    ), None
                return self._file_unit(ref, path, MaterialKind.IMAGE), None
            except UnidentifiedImageError:
                return self._unsupported(ref, path, UnsupportedReasonCode.IMAGE_DECODE_FAILED), None
            except (OSError, SyntaxError):
                return self._unsupported(ref, path, UnsupportedReasonCode.IMAGE_DECODE_FAILED), None
        if suffix in _SPREADSHEET_SUFFIXES:
            return self._unsupported(ref, path, UnsupportedReasonCode.UNSUPPORTED_FILE_FORMAT), None
        identification = self.discovery.identify(path)
        if identification.member_kind is not None:
            return None, identification
        if identification.design_looking:
            return self._unsupported(
                ref, path, UnsupportedReasonCode.DESIGN_FILE_UNRECOGNIZED
            ), None
        return self._unsupported(ref, path, UnsupportedReasonCode.UNSUPPORTED_FILE_FORMAT), None

    @staticmethod
    def _file_unit(ref: str, path: Path, kind: MaterialKind) -> LogicalUnit:
        return LogicalUnit(ref, path.name, kind, (UnitMember(path, path.name),))

    @staticmethod
    def _unsupported(ref: str, path: Path, reason: UnsupportedReasonCode) -> LogicalUnit:
        return LogicalUnit(
            ref,
            path.name,
            MaterialKind.UNSUPPORTED,
            (UnitMember(path, path.name),),
            unsupported_reason=reason,
        )

    def _group_directory_jobs(
        self, design: list[tuple[str, Path, DesignIdentification]]
    ) -> list[LogicalUnit]:
        grouped: dict[str, list[tuple[str, Path, DesignIdentification]]] = {}
        for item in design:
            ref = PurePosixPath(item[0])
            boundary = "." if len(ref.parts) == 1 else ref.parts[0]
            grouped.setdefault(boundary, []).append(item)
        jobs = [
            self._build_job(boundary, tuple(members), boundary=boundary)
            for boundary, members in grouped.items()
        ]
        return sorted(jobs, key=lambda item: (item.input_ref != ".", item.input_ref))

    def _build_job(
        self,
        input_ref: str,
        members: tuple[tuple[str, Path, DesignIdentification], ...],
        *,
        boundary: str | None,
    ) -> LogicalUnit:
        known_refs = {ref for ref, _, _ in members}
        ambiguous = False
        unit_members: list[UnitMember] = []
        for ref, path, identification in sorted(members):
            target = (
                path.name
                if boundary in {None, "."}
                else PurePosixPath(ref).relative_to(boundary).as_posix()
            )
            unit_members.append(
                UnitMember(path, target, identification.member_kind, identification.references)
            )
            for reference in identification.references:
                normalized = posixpath.normpath(posixpath.join(posixpath.dirname(ref), reference))
                if normalized.startswith("../") or normalized not in known_refs:
                    ambiguous = True
        member_kinds = tuple(
            kind for kind in MemberKind if any(item.kind == kind for item in unit_members)
        )
        display_name = Path(input_ref).name if input_ref != "." else "root-gerber-job"
        if boundary is None:
            display_name = members[0][1].name
        return LogicalUnit(
            input_ref=input_ref,
            display_name=display_name,
            kind=MaterialKind.UNSUPPORTED if ambiguous else MaterialKind.GERBER_JOB,
            members=tuple(unit_members),
            member_kinds=() if ambiguous else member_kinds,
            unsupported_reason=(UnsupportedReasonCode.GERBER_JOB_AMBIGUOUS if ambiguous else None),
            directory_source=True,
        )

    @staticmethod
    def _copy_unit(unit: LogicalUnit, unit_root: Path) -> None:
        targets = [
            (member, unit_root.joinpath(*member.target_ref.split("/"))) for member in unit.members
        ]
        normalized_targets = [os.path.normcase(os.path.abspath(target)) for _, target in targets]
        if len(normalized_targets) != len(set(normalized_targets)):
            raise FileExistsError("logical unit members collide in managed storage")
        unit_root.mkdir(parents=False, exist_ok=False)
        for member, target in targets:
            before = member.source.stat()
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(member.source, target)
            after = member.source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise _SourceChanged
            if before.st_size != target.stat().st_size:
                raise _SourceChanged


class _SourceChanged(Exception):
    pass
