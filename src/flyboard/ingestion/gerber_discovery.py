from __future__ import annotations

import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path

from gerbonara.excellon import ExcellonFile
from gerbonara.rs274x import GerberFile

from flyboard.models import MemberKind

_GERBER_SUFFIXES = {
    ".art",
    ".cmp",
    ".gbl",
    ".gbo",
    ".gbp",
    ".gbr",
    ".gbs",
    ".ger",
    ".gko",
    ".gm1",
    ".gml",
    ".gtl",
    ".gto",
    ".gtp",
    ".gts",
    ".pho",
    ".sol",
}
_DRILL_SUFFIXES = {".drl", ".drd", ".exc", ".nc", ".tap", ".xln", ".xnc"}
_GTD_SUFFIXES = {".gbrjob", ".gtd"}
_DESIGN_SUFFIXES = _GERBER_SUFFIXES | _DRILL_SUFFIXES | _GTD_SUFFIXES
_GERBER_MARKERS = (
    re.compile(r"%FS", re.IGNORECASE),
    re.compile(r"%MO", re.IGNORECASE),
    re.compile(r"%ADD", re.IGNORECASE),
    re.compile(r"\bD0?[123]\*?", re.IGNORECASE),
    re.compile(r"\bM02\*?", re.IGNORECASE),
)
_DRILL_MARKERS = (
    re.compile(r"(^|\n)\s*M48\b", re.IGNORECASE),
    re.compile(r"\bFMAT\b", re.IGNORECASE),
    re.compile(r"(^|\n)\s*T\d+\s*C[0-9.]+", re.IGNORECASE),
    re.compile(r"(^|\n)\s*X[-+]?[0-9.]+Y[-+]?[0-9.]+", re.IGNORECASE),
)
_GTD_MARKERS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (r"\{\s*Program\b", r"\{\s*Layers\b", r"\{\s*Layer\b", r"\{\s*File\b")
)
_GTD_FILE_RE = re.compile(r"\{\s*File\b\s*([^{}]*)\}", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DesignIdentification:
    member_kind: MemberKind | None
    design_looking: bool
    references: tuple[str, ...] = ()


class GerberFileDiscovery:
    """The single content-first lightweight Gerber/Drill/GTD identification capability."""

    @staticmethod
    def is_design_candidate(path: Path) -> bool:
        return path.suffix.lower() in _DESIGN_SUFFIXES

    def identify(self, path: Path) -> DesignIdentification:
        suffix = path.suffix.lower()
        try:
            text = path.read_bytes()[: 256 * 1024].decode("utf-8", errors="replace")
        except OSError:
            raise
        gtd_marker_count = sum(bool(pattern.search(text)) for pattern in _GTD_MARKERS)
        if suffix in _GTD_SUFFIXES or gtd_marker_count >= 3:
            try:
                if suffix == ".gbrjob":
                    references = self._references(text, json_required=True)
                    if not references:
                        raise ValueError("Gerber Job has no file references")
                else:
                    if gtd_marker_count == 0:
                        raise ValueError("GTD content markers are missing")
                    references = self._references(text, json_required=False)
                return DesignIdentification(MemberKind.GTD, True, references)
            except (UnicodeError, ValueError, json.JSONDecodeError):
                return DesignIdentification(None, True)

        gerber_looking = (
            suffix in _GERBER_SUFFIXES
            or sum(bool(pattern.search(text)) for pattern in _GERBER_MARKERS) >= 2
        )
        if gerber_looking:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    GerberFile.open(path)
                return DesignIdentification(MemberKind.GERBER, True)
            except Exception:
                return DesignIdentification(None, True)

        drill_matches = [bool(pattern.search(text)) for pattern in _DRILL_MARKERS]
        drill_looking = suffix in _DRILL_SUFFIXES or (drill_matches[0] and any(drill_matches[1:]))
        if drill_looking:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    ExcellonFile.open(path)
                return DesignIdentification(MemberKind.DRILL, True)
            except Exception:
                return DesignIdentification(None, True)
        return DesignIdentification(None, False)

    @staticmethod
    def _references(content: str, *, json_required: bool) -> tuple[str, ...]:
        values: list[str] = []
        if json_required:
            parsed = json.loads(content)
            if type(parsed) is not dict:
                raise ValueError("Gerber Job root must be an object")

            def visit(value: object) -> None:
                if isinstance(value, str):
                    if Path(value.replace("\\", "/")).suffix.lower() in _DESIGN_SUFFIXES:
                        values.append(value)
                elif isinstance(value, list):
                    for item in value:
                        visit(item)
                elif isinstance(value, dict):
                    for item in value.values():
                        visit(item)

            visit(parsed)
        else:
            values.extend(
                match.group(1).strip().strip('"').strip("'")
                for match in _GTD_FILE_RE.finditer(content)
                if match.group(1).strip()
            )
        return tuple(dict.fromkeys(value.replace("\\", "/") for value in values))
