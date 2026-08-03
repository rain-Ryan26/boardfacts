from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from flyboard.errors import ServiceError

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_WINDOWS_RESERVED = {
    "AUX",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "CON",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
    "NUL",
    "PRN",
}


def _unsafe_windows_name(value: str) -> bool:
    stem = value.split(".", 1)[0].upper()
    return value.endswith((".", " ")) or stem in _WINDOWS_RESERVED


def validate_identifier(value: str, *, code: str = "invalid_managed_path") -> str:
    if (
        not value
        or value in {".", ".."}
        or _SAFE_ID.fullmatch(value) is None
        or _unsafe_windows_name(value)
    ):
        raise ServiceError(code)
    return value


def validate_relative_locator(value: str, *, code: str = "invalid_managed_path") -> tuple[str, ...]:
    if not value or "\\" in value or value.startswith("/") or ":" in value:
        raise ServiceError(code)
    parts = tuple(value.split("/"))
    if any(part in {"", ".", ".."} or _unsafe_windows_name(part) for part in parts):
        raise ServiceError(code)
    return parts


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def ensure_no_reparse_points(root: Path, target: Path, *, include_target: bool = True) -> None:
    root_abs = Path(os.path.abspath(root))
    target_abs = Path(os.path.abspath(target))
    try:
        relative = target_abs.relative_to(root_abs)
    except ValueError as exc:
        raise ServiceError("managed_path_escape") from exc
    current = root_abs
    if _is_reparse_point(current):
        raise ServiceError("managed_path_reparse_point")
    parts = relative.parts if include_target else relative.parts[:-1]
    for part in parts:
        current /= part
        if _is_reparse_point(current):
            raise ServiceError("managed_path_reparse_point")


def managed_child(root: Path, *parts: str, locator: bool = False) -> Path:
    validated: list[str] = []
    for part in parts:
        validated.extend(
            validate_relative_locator(part) if locator else (validate_identifier(part),)
        )
    target = root.joinpath(*validated)
    ensure_no_reparse_points(root, target)
    return target


def json_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    payload = (
        value.model_dump(mode="json", exclude_none=False) if isinstance(value, BaseModel) else value
    )
    return (
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def atomic_replace_bytes(path: Path, content: bytes) -> None:
    ensure_no_reparse_points(path.parent, path, include_target=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_no_reparse_points(path.parent, path.parent)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def read_json_object(path: Path, *, missing_code: str, invalid_code: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ServiceError(missing_code) from exc
    except OSError as exc:
        raise ServiceError(invalid_code) from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceError(invalid_code) from exc
    if type(value) is not dict:
        raise ServiceError(invalid_code)
    return value
