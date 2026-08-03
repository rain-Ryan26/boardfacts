from __future__ import annotations

import shutil
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Iterable

from flyboard.errors import ServiceError
from flyboard.repositories._filesystem import ensure_no_reparse_points

from .registration import MaterialStager, StagingResult


class UploadMode(StrEnum):
    FILES = "files"
    DIRECTORY = "directory"
    ARCHIVE = "archive"


@dataclass(frozen=True, slots=True)
class UploadLimits:
    max_upload_bytes: int = 256 * 1024 * 1024
    max_input_files: int = 1_000
    max_file_bytes: int = 256 * 1024 * 1024
    max_expanded_bytes: int = 1024 * 1024 * 1024
    max_archive_members: int = 5_000
    max_depth: int = 16
    max_compression_ratio: int = 100
    max_relative_path_chars: int = 512
    chunk_bytes: int = 1024 * 1024

    @property
    def max_multipart_body_bytes(self) -> int:
        return self.max_upload_bytes + 1024 * 1024


DEFAULT_UPLOAD_LIMITS = UploadLimits()


@dataclass(frozen=True, slots=True)
class UploadedFileSource:
    relative_path: str
    stream: BinaryIO


@dataclass(frozen=True, slots=True)
class _ArchiveMember:
    info: zipfile.ZipInfo
    parts: tuple[str, ...]
    is_directory: bool


_WINDOWS_RESERVED = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_WINDOWS_ILLEGAL = frozenset('<>:"|?*')
_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


class MaterialUploadStager:
    """Turns browser-owned bytes into one local input root for MaterialStager."""

    def __init__(
        self,
        material_stager: MaterialStager,
        *,
        limits: UploadLimits = DEFAULT_UPLOAD_LIMITS,
    ) -> None:
        self.material_stager = material_stager
        self.limits = limits

    def stage(
        self,
        project_id: str,
        request_id: str,
        input_mode: str,
        files: Iterable[UploadedFileSource],
    ) -> StagingResult:
        try:
            mode = UploadMode(input_mode)
        except ValueError as exc:
            raise ServiceError("invalid_upload_manifest") from exc
        uploads = tuple(files)
        normalized = self._validate_manifest(mode, uploads)
        request_root = self.material_stager.create_request_root(project_id, request_id)
        try:
            incoming = request_root / "incoming"
            incoming.mkdir()
            written = self._write_uploads(incoming, uploads, normalized)
            if mode == UploadMode.ARCHIVE:
                expanded = request_root / "expanded"
                expanded.mkdir()
                self._extract_archive(written[0], expanded)
                input_path = expanded
            elif mode == UploadMode.FILES and len(written) == 1:
                input_path = written[0]
            else:
                input_path = incoming
            return self.material_stager.stage_prepared(request_root, input_path)
        except Exception:
            shutil.rmtree(request_root, ignore_errors=True)
            raise

    def _validate_manifest(
        self,
        mode: UploadMode,
        uploads: tuple[UploadedFileSource, ...],
    ) -> tuple[tuple[str, ...], ...]:
        if not uploads or len(uploads) > self.limits.max_input_files:
            raise ServiceError("invalid_upload_manifest")
        if mode == UploadMode.ARCHIVE and len(uploads) != 1:
            raise ServiceError("invalid_upload_manifest")
        normalized = tuple(self._safe_parts(item.relative_path) for item in uploads)
        if mode in {UploadMode.FILES, UploadMode.ARCHIVE} and any(
            len(parts) != 1 for parts in normalized
        ):
            raise ServiceError("invalid_upload_manifest")
        suffixes = tuple(Path(parts[-1]).suffix.casefold() for parts in normalized)
        if mode == UploadMode.FILES and any(suffix == ".zip" for suffix in suffixes):
            raise ServiceError("invalid_upload_manifest")
        if mode == UploadMode.ARCHIVE and suffixes[0] != ".zip":
            raise ServiceError("invalid_upload_manifest")
        self._validate_collisions((parts, False) for parts in normalized)
        return normalized

    def _safe_parts(self, value: str) -> tuple[str, ...]:
        if (
            not value
            or len(value) > self.limits.max_relative_path_chars
            or "\\" in value
            or value.startswith("/")
        ):
            raise ServiceError("invalid_upload_manifest")
        normalized = unicodedata.normalize("NFC", value)
        parts = tuple(normalized.split("/"))
        if len(parts) > self.limits.max_depth:
            raise ServiceError("invalid_upload_manifest")
        for part in parts:
            if (
                not part
                or part in {".", ".."}
                or len(part) > 255
                or part.endswith((".", " "))
                or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED
                or any(character in _WINDOWS_ILLEGAL for character in part)
                or any(ord(character) < 32 or ord(character) == 127 for character in part)
            ):
                raise ServiceError("invalid_upload_manifest")
        return parts

    @staticmethod
    def _collision_key(parts: tuple[str, ...]) -> str:
        return "/".join(unicodedata.normalize("NFC", part).casefold() for part in parts)

    def _validate_collisions(self, entries: Iterable[tuple[tuple[str, ...], bool]]) -> None:
        kinds: dict[str, bool] = {}
        for parts, is_directory in entries:
            key = self._collision_key(parts)
            if key in kinds:
                raise ServiceError("invalid_upload_manifest")
            kinds[key] = is_directory
        for key, is_directory in kinds.items():
            segments = key.split("/")
            for index in range(1, len(segments)):
                parent = "/".join(segments[:index])
                if parent in kinds and not kinds[parent]:
                    raise ServiceError("invalid_upload_manifest")
            if not is_directory and any(other.startswith(f"{key}/") for other in kinds):
                raise ServiceError("invalid_upload_manifest")

    def _write_uploads(
        self,
        incoming: Path,
        uploads: tuple[UploadedFileSource, ...],
        normalized: tuple[tuple[str, ...], ...],
    ) -> tuple[Path, ...]:
        total_bytes = 0
        written: list[Path] = []
        try:
            for upload, parts in zip(uploads, normalized, strict=True):
                target = incoming.joinpath(*parts)
                ensure_no_reparse_points(incoming, target)
                target.parent.mkdir(parents=True, exist_ok=True)
                ensure_no_reparse_points(incoming, target.parent)
                file_bytes = 0
                with target.open("xb") as destination:
                    while chunk := upload.stream.read(self.limits.chunk_bytes):
                        file_bytes += len(chunk)
                        total_bytes += len(chunk)
                        if file_bytes > self.limits.max_file_bytes:
                            raise ServiceError("upload_too_large")
                        if total_bytes > self.limits.max_upload_bytes:
                            raise ServiceError("upload_too_large")
                        destination.write(chunk)
                written.append(target)
        except ServiceError:
            raise
        except OSError as exc:
            raise ServiceError("upload_storage_failed") from exc
        return tuple(written)

    def _extract_archive(self, archive_path: Path, destination: Path) -> None:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = self._inspect_archive(archive)
                total_actual = 0
                for member in members:
                    target = destination.joinpath(*member.parts)
                    ensure_no_reparse_points(destination, target)
                    if member.is_directory:
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    ensure_no_reparse_points(destination, target.parent)
                    member_actual = 0
                    with archive.open(member.info, "r") as source, target.open("xb") as output:
                        while chunk := source.read(self.limits.chunk_bytes):
                            member_actual += len(chunk)
                            total_actual += len(chunk)
                            if member_actual > self.limits.max_file_bytes:
                                raise ServiceError("archive_limit_exceeded")
                            if total_actual > self.limits.max_expanded_bytes:
                                raise ServiceError("archive_limit_exceeded")
                            output.write(chunk)
        except ServiceError:
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, ValueError) as exc:
            raise ServiceError("archive_invalid") from exc
        except OSError as exc:
            raise ServiceError("upload_storage_failed") from exc

    def _inspect_archive(self, archive: zipfile.ZipFile) -> tuple[_ArchiveMember, ...]:
        infos = archive.infolist()
        if not infos:
            raise ServiceError("archive_invalid")
        if len(infos) > self.limits.max_archive_members:
            raise ServiceError("archive_limit_exceeded")
        members: list[_ArchiveMember] = []
        declared_total = 0
        for info in infos:
            raw_name = (
                info.filename[:-1]
                if info.is_dir() and info.filename.endswith("/")
                else info.filename
            )
            try:
                parts = self._safe_parts(raw_name)
            except ServiceError as exc:
                raise ServiceError("archive_unsafe") from exc
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if info.flag_bits & 0x1:
                raise ServiceError("archive_unsafe")
            if info.compress_type not in _ALLOWED_COMPRESSION:
                raise ServiceError("archive_unsafe")
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ServiceError("archive_unsafe")
            if info.is_dir() != (file_type == stat.S_IFDIR) and file_type != 0:
                raise ServiceError("archive_unsafe")
            if not info.is_dir():
                declared_total += info.file_size
                if info.file_size > self.limits.max_file_bytes:
                    raise ServiceError("archive_limit_exceeded")
                if info.file_size and (
                    info.compress_size == 0
                    or info.file_size / info.compress_size > self.limits.max_compression_ratio
                ):
                    raise ServiceError("archive_limit_exceeded")
            members.append(_ArchiveMember(info, parts, info.is_dir()))
        if declared_total > self.limits.max_expanded_bytes:
            raise ServiceError("archive_limit_exceeded")
        try:
            self._validate_collisions((member.parts, member.is_directory) for member in members)
        except ServiceError as exc:
            raise ServiceError("archive_unsafe") from exc
        if not any(not member.is_directory for member in members):
            raise ServiceError("archive_invalid")
        return tuple(members)
