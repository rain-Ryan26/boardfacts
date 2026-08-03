from __future__ import annotations

import hashlib
import math
import sys
import threading
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from gerbonara.excellon import ExcellonFile
from gerbonara.rs274x import GerberFile
from gerbonara.utils import MM
from shapely import geos_version_string

from flyboard.ingestion import GerberFileDiscovery, material_sha256_from_source
from flyboard.models import MemberKind, RunInputSnapshot
from flyboard.repositories import ProjectRepository

_CACHE_VERSION = (
    f"py{sys.version_info.major}.{sys.version_info.minor}"
    f"|gerbonara=1.6.3|shapely=2.1.2|geos={geos_version_string}"
    "|grid=0.000001|arc=0.001/4096/250000|budget=100000/2000000/200000/1000000|spacing_batch=2048"
)


class GerberPreparationError(Exception):
    def __init__(self, code: str, message: str, *, source_file: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.source_file = source_file


@dataclass(frozen=True, slots=True)
class ParsedMember:
    relative_path: str
    detected_type: Literal["gerber", "drill", "gtd"]
    sha256: str
    parsed: object | None
    parser_warnings: tuple[str, ...]
    metadata_text: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedJob:
    project_id: str
    run_id: str
    job_material_id: str
    snapshot_sha256: str
    source_ref: str
    members: tuple[ParsedMember, ...]
    union_bounds: tuple[tuple[float, float], tuple[float, float]]

    @property
    def layer_members(self) -> tuple[ParsedMember, ...]:
        return tuple(item for item in self.members if item.detected_type != "gtd")


class RunGerberCache:
    """Process-local role-neutral parsed cache; it is neither an artifact nor recovery state."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._jobs: dict[tuple[str, str, str, str], ParsedJob] = {}

    def put(self, job: ParsedJob) -> None:
        key = (job.run_id, job.job_material_id, job.snapshot_sha256, _CACHE_VERSION)
        with self._guard:
            self._jobs[key] = job

    def get(self, run_id: str, job_material_id: str, snapshot_sha256: str) -> ParsedJob | None:
        key = (run_id, job_material_id, snapshot_sha256, _CACHE_VERSION)
        with self._guard:
            return self._jobs.get(key)

    def discard_run(self, run_id: str) -> None:
        with self._guard:
            self._jobs = {key: job for key, job in self._jobs.items() if key[0] != run_id}


class GerberJobParser:
    def __init__(
        self,
        projects: ProjectRepository,
        *,
        discovery: GerberFileDiscovery | None = None,
    ) -> None:
        self._projects = projects
        self._discovery = discovery or GerberFileDiscovery()

    def parse(
        self,
        snapshot: RunInputSnapshot,
        job_material_id: str,
    ) -> ParsedJob:
        material = next(
            (item for item in snapshot.materials if item.material_id == job_material_id), None
        )
        if material is None or job_material_id not in snapshot.groups.gerber_jobs:
            raise GerberPreparationError(
                "gerber_input_parse_failed", "Gerber job 不属于本次输入快照。"
            )
        try:
            source_root = self._projects.resolve_material_source(snapshot.project_id, material)
        except Exception as exc:
            raise GerberPreparationError(
                "gerber_input_parse_failed", "Gerber job 的受管来源无法读取。"
            ) from exc
        if not source_root.is_dir():
            raise GerberPreparationError(
                "gerber_input_parse_failed", "Gerber job 的受管来源不是目录。"
            )
        try:
            actual_sha256 = material_sha256_from_source(source_root, material)
        except Exception as exc:
            raise GerberPreparationError(
                "gerber_input_parse_failed", "Gerber job 的当前内容身份无法复验。"
            ) from exc
        if actual_sha256 != material.sha256:
            raise GerberPreparationError(
                "gerber_input_parse_failed", "Gerber job 内容已偏离本次输入快照。"
            )

        try:
            paths = sorted(
                (item for item in source_root.rglob("*") if item.is_file()),
                key=lambda item: item.relative_to(source_root).as_posix().casefold(),
            )
        except OSError as exc:
            raise GerberPreparationError(
                "gerber_input_parse_failed", "Gerber job 成员枚举失败。"
            ) from exc
        if not paths:
            raise GerberPreparationError("gerber_input_parse_failed", "Gerber job 没有可处理成员。")

        members: list[ParsedMember] = []
        bounds: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for path in paths:
            relative = path.relative_to(source_root).as_posix()
            try:
                content = path.read_bytes()
                identity = self._discovery.identify(path)
            except OSError as exc:
                raise GerberPreparationError(
                    "gerber_input_parse_failed",
                    "Gerber job 成员读取失败。",
                    source_file=relative,
                ) from exc
            if identity.member_kind is None:
                raise GerberPreparationError(
                    "gerber_input_parse_failed",
                    "Gerber job 成员无法按已登记格式重新识别。",
                    source_file=relative,
                )
            digest = hashlib.sha256(content).hexdigest()
            if identity.member_kind == MemberKind.GTD:
                members.append(
                    ParsedMember(
                        relative_path=relative,
                        detected_type="gtd",
                        sha256=digest,
                        parsed=None,
                        parser_warnings=(),
                        metadata_text=content.decode("utf-8", errors="replace"),
                    )
                )
                continue
            parsed, warning_messages = self._parse_graphic(path, identity.member_kind, relative)
            detected_type: Literal["gerber", "drill"] = (
                "gerber" if identity.member_kind == MemberKind.GERBER else "drill"
            )
            member = ParsedMember(
                relative_path=relative,
                detected_type=detected_type,
                sha256=digest,
                parsed=parsed,
                parser_warnings=warning_messages,
            )
            members.append(member)
            current_bounds = parsed.bounding_box(MM, default=None)
            if current_bounds is not None and _valid_bounds(current_bounds):
                bounds.append(_float_bounds(current_bounds))

        if not any(item.detected_type != "gtd" for item in members):
            raise GerberPreparationError(
                "gerber_input_parse_failed", "Gerber job 没有 Gerber 或 Drill 物理层。"
            )
        union_bounds = _union_bounds(bounds) if bounds else ((0.0, 0.0), (1.0, 1.0))
        return ParsedJob(
            project_id=snapshot.project_id,
            run_id=snapshot.run_id,
            job_material_id=job_material_id,
            snapshot_sha256=material.sha256,
            source_ref=material.source_ref,
            members=tuple(members),
            union_bounds=union_bounds,
        )

    @staticmethod
    def _parse_graphic(path: Path, kind: MemberKind, relative: str):
        try:
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always", SyntaxWarning)
                parsed = (
                    GerberFile.open(path) if kind == MemberKind.GERBER else ExcellonFile.open(path)
                )
        except Exception as exc:
            raise GerberPreparationError(
                "gerber_input_parse_failed",
                "Gerbonara 无法建立安全的有序对象流。",
                source_file=relative,
            ) from exc
        messages = tuple(dict.fromkeys(str(item.message) for item in captured))
        return parsed, messages


def render_layer_svg(job: ParsedJob, member: ParsedMember) -> bytes:
    if member.parsed is None:
        raise GerberPreparationError(
            "gerber_preview_failed",
            "Gerber preview 缺少 role-neutral 解析对象。",
            source_file=member.relative_path,
        )
    try:
        content = str(
            member.parsed.to_svg(
                arg_unit=MM,
                svg_unit=MM,
                force_bounds=job.union_bounds,
                fg="black",
                bg=None,
            )
        ).encode("utf-8")
        root = ET.fromstring(content)
        if not root.tag.endswith("svg"):
            raise ValueError("preview root is not svg")
        view_box = root.attrib.get("viewBox", "").split()
        if len(view_box) != 4 or not all(math.isfinite(float(item)) for item in view_box):
            raise ValueError("preview viewBox is invalid")
        expected = _view_box(job.union_bounds)
        actual = tuple(float(item) for item in view_box)
        if any(abs(left - right) > 1e-6 for left, right in zip(actual, expected)):
            raise ValueError("preview does not use the job union bounds")
    except Exception as exc:
        raise GerberPreparationError(
            "gerber_preview_failed",
            "Gerber layer SVG 生成或统一 viewBox 校验失败。",
            source_file=member.relative_path,
        ) from exc
    return content


def layer_id(file_path: str) -> str:
    return f"layer_{hashlib.sha256(file_path.encode('utf-8')).hexdigest()[:20]}"


def _view_box(bounds):
    (min_x, min_y), (max_x, max_y) = bounds
    return (min_x, min_y, max_x - min_x, max_y - min_y)


def _valid_bounds(bounds) -> bool:
    try:
        (min_x, min_y), (max_x, max_y) = bounds
        values = (float(min_x), float(min_y), float(max_x), float(max_y))
    except (TypeError, ValueError):
        return False
    return all(math.isfinite(item) and abs(item) <= 1_000_000 for item in values)


def _float_bounds(bounds):
    (min_x, min_y), (max_x, max_y) = bounds
    return ((float(min_x), float(min_y)), (float(max_x), float(max_y)))


def _union_bounds(values):
    min_x = min(item[0][0] for item in values)
    min_y = min(item[0][1] for item in values)
    max_x = max(item[1][0] for item in values)
    max_y = max(item[1][1] for item in values)
    if max_x == min_x:
        max_x += 1.0
    if max_y == min_y:
        max_y += 1.0
    return ((min_x, min_y), (max_x, max_y))
