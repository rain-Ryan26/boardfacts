from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from gerbonara import graphic_primitives as primitives
from gerbonara.apertures import CircleAperture, ExcellonTool, RectangleAperture
from gerbonara.graphic_objects import Arc, Flash, Line
from gerbonara.utils import MM
from shapely import affinity, get_num_coordinates, set_precision
from shapely.geometry import GeometryCollection, LineString, Point, Polygon, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

from flyboard.models import LayerDefinition, LayerRole

from .models import (
    OUTPUT_KEYS,
    GerberCheck,
    GerberExpectedOutputMissing,
    GerberOutput,
    GerberResult,
    GerberToolError,
)
from .parser import ParsedJob, ParsedMember, layer_id

_GRID = 0.000001
_CHORD_ERROR = 0.001
_MAX_ARC_SEGMENTS = 4_096
_MAX_JOB_SOURCE_OBJECTS = 100_000
_MAX_JOB_ARC_SEGMENTS = 250_000
_MAX_JOB_POLYGON_INPUT_COORDINATES = 2_000_000
_MAX_SPACING_COMPONENTS = 200_000
_MAX_SPACING_NEAREST_RESULTS = 200_000
_SPACING_QUERY_BATCH_SIZE = 2_048
_MAX_ANNULAR_CANDIDATE_RELATIONS = 1_000_000

_COPPER_ROLES = {
    LayerRole.TOP_COPPER,
    LayerRole.INNER_COPPER,
    LayerRole.BOTTOM_COPPER,
}
_GERBER_MEASURE_KEYS = {
    "geometry.job_bounds_mm",
    "geometry.min_trace_width_mm",
    "geometry.min_copper_spacing_mm",
    "geometry.min_annular_ring_mm",
}
_DRILL_MEASURE_KEYS = {
    "drill.used_hole_size_summary",
    "drill.used_hole_quantity_summary",
    "drill.slot_size_summary",
    "drill.min_used_hole_diameter_mm",
    "geometry.min_annular_ring_mm",
}


@dataclass(frozen=True, slots=True)
class _LayerGeometry:
    geometry: object
    complete: bool


@dataclass(frozen=True, slots=True)
class _Hole:
    x: float
    y: float
    diameter: float
    source_file: str
    plated: bool | None


@dataclass(frozen=True, slots=True)
class _Pad:
    x: float
    y: float
    shape: str
    width: float
    height: float
    source_file: str


class _Coverage:
    def __init__(self) -> None:
        self.files_by_key: dict[str, set[str]] = defaultdict(set)
        self.messages_by_key: dict[str, set[str]] = defaultdict(set)
        self.events: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    def affect(
        self,
        keys,
        source_file: str,
        message: str,
        *,
        code: str | None = "gerber_capability_unsupported",
    ) -> None:
        for key in keys:
            self.files_by_key[key].add(source_file)
            self.messages_by_key[key].add(message)
            if code is not None:
                self.events[(code, source_file, message)].add(key)

    def has(self, key: str) -> bool:
        return bool(self.files_by_key.get(key))

    def checks(self) -> list[GerberCheck]:
        result: list[GerberCheck] = []
        for (code, source_file, message), event_keys in sorted(self.events.items()):
            keys = [key for key in OUTPUT_KEYS if key in event_keys and self.has(key)]
            if not keys:
                continue
            result.append(
                GerberCheck(
                    code=code,
                    kind="coverage_gap",
                    message=message,
                    affected_output_keys=keys,
                    source_files=[source_file],
                )
            )
        return result


class _GeometryBudgetExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class _GeometryBudget:
    arc_segments: int = 0
    polygon_input_coordinates: int = 0
    exhausted: bool = False

    def ensure_available(self) -> None:
        if self.exhausted:
            raise _GeometryBudgetExceeded

    def claim_arc_segments(self, count: int) -> None:
        self.ensure_available()
        if self.arc_segments + count > _MAX_JOB_ARC_SEGMENTS:
            self.exhausted = True
            raise _GeometryBudgetExceeded
        self.arc_segments += count

    def claim_polygon_input_coordinates(self, count: int) -> None:
        self.ensure_available()
        if self.polygon_input_coordinates + count > _MAX_JOB_POLYGON_INPUT_COORDINATES:
            self.exhausted = True
            raise _GeometryBudgetExceeded
        self.polygon_input_coordinates += count


def build_gerber_result(job: ParsedJob, definition: LayerDefinition) -> GerberResult:
    if (
        definition.project_id != job.project_id
        or definition.run_id != job.run_id
        or definition.job_material_id != job.job_material_id
        or definition.confirmed_at is None
    ):
        raise ValueError("Gerber definition identity or confirmation is invalid")
    expected = [(item.relative_path, layer_id(item.relative_path)) for item in job.layer_members]
    actual = [(item.file_path, item.layer_id) for item in definition.layers]
    if actual != expected:
        raise ValueError("Gerber definition does not exactly cover preparation layers")

    roles = {item.file_path: item.role for item in definition.layers}
    copper_indexes = {item.file_path: item.copper_layer_index for item in definition.layers}
    active = [item for item in job.layer_members if roles[item.relative_path] != LayerRole.IGNORED]
    coverage = _Coverage()
    for member in active:
        if member.parser_warnings:
            keys = _GERBER_MEASURE_KEYS if member.detected_type == "gerber" else _DRILL_MEASURE_KEYS
            coverage.affect(
                keys,
                member.relative_path,
                "Gerbonara 对该文件给出规范或解析 warning，相关测量已显式降级。",
            )

    gerber_members = [item for item in active if item.detected_type == "gerber"]
    drill_members = [item for item in active if item.detected_type == "drill"]
    unknown_gerber = [
        item for item in gerber_members if roles[item.relative_path] == LayerRole.UNKNOWN
    ]
    unknown_drill = [
        item for item in drill_members if roles[item.relative_path] == LayerRole.UNKNOWN
    ]
    known_copper = [item for item in gerber_members if roles[item.relative_path] in _COPPER_ROLES]
    known_drill = [
        item
        for item in drill_members
        if roles[item.relative_path] in {LayerRole.DRILL, LayerRole.ROUTE}
    ]

    if unknown_gerber:
        files = [item.relative_path for item in unknown_gerber]
        coverage.affect(
            {
                "layers.copper_layer_count",
                "layers.solder_mask_sides",
                "layers.silkscreen_sides",
                "geometry.min_trace_width_mm",
                "geometry.min_copper_spacing_mm",
                "geometry.min_annular_ring_mm",
            },
            files[0],
            "存在已显式确认但层角色尚未明确的 Gerber 层，角色相关能力无法完整判断。",
        )
        for source_file in files[1:]:
            coverage.affect(
                {
                    "layers.copper_layer_count",
                    "layers.solder_mask_sides",
                    "layers.silkscreen_sides",
                    "geometry.min_trace_width_mm",
                    "geometry.min_copper_spacing_mm",
                    "geometry.min_annular_ring_mm",
                },
                source_file,
                "存在已显式确认但层角色尚未明确的 Gerber 层。",
            )
    if unknown_drill:
        for member in unknown_drill:
            coverage.affect(
                _DRILL_MEASURE_KEYS,
                member.relative_path,
                "存在已显式确认但层角色尚未明确的 Drill 层，孔槽相关能力无法完整判断。",
            )

    errors: list[GerberToolError] = []
    outputs: dict[str, GerberOutput] = {}
    outputs.update(_layer_outputs(active, roles, copper_indexes, coverage))
    copper_geometry_paths = {member.relative_path for member in [*known_copper, *unknown_gerber]}
    geometry_keys_by_path = {}
    for member in gerber_members:
        affected = {"geometry.job_bounds_mm"}
        if member.relative_path in copper_geometry_paths:
            affected.update({"geometry.min_copper_spacing_mm", "geometry.min_annular_ring_mm"})
        geometry_keys_by_path[member.relative_path] = affected
    source_object_count = sum(
        len(member.parsed.objects)
        for member in job.layer_members
        if member.detected_type in {"gerber", "drill"} and member.parsed is not None
    )
    source_budget_exceeded = source_object_count > _MAX_JOB_SOURCE_OBJECTS
    if source_budget_exceeded:
        layer_geometries = {}
        for member in gerber_members:
            coverage.affect(
                geometry_keys_by_path[member.relative_path],
                member.relative_path,
                "单 job 规范化源对象总数超过首版 100,000 个准入上限。",
                code="gerber_geometry_budget_exceeded",
            )
    else:
        layer_geometries = _geometries(
            gerber_members,
            coverage,
            errors,
            geometry_keys_by_path,
            _GeometryBudget(),
        )
    outputs["geometry.job_bounds_mm"] = _bounds_output(gerber_members, layer_geometries, coverage)
    outputs["geometry.min_trace_width_mm"] = _trace_width_output(
        known_copper, unknown_gerber, coverage
    )
    outputs["geometry.min_copper_spacing_mm"] = _spacing_output(
        known_copper, unknown_gerber, layer_geometries, coverage
    )
    holes, routes = _drill_objects(known_drill, unknown_drill, coverage)
    outputs.update(_drill_outputs(known_drill, unknown_drill, holes, routes, coverage))
    if source_budget_exceeded:
        relevant = bool(known_copper or unknown_gerber) and bool(known_drill or unknown_drill)
        annular_output = (
            _candidate_output("geometry.min_annular_ring_mm", None, coverage)
            if relevant
            else _not_applicable("geometry.min_annular_ring_mm")
        )
        breakout = []
    else:
        pads = _copper_pads(known_copper, coverage)
        annular_output, breakout = _annular_output(
            known_copper,
            known_drill,
            unknown_gerber,
            unknown_drill,
            holes,
            pads,
            coverage,
        )
    outputs["geometry.min_annular_ring_mm"] = annular_output
    outputs.update(_plating_outputs(job, holes, coverage))

    for output in outputs.values():
        if not output.applicable:
            coverage.files_by_key.pop(output.key, None)
            coverage.messages_by_key.pop(output.key, None)
    checks = coverage.checks()
    if breakout:
        checks.append(
            GerberCheck(
                code="gerber_annular_breakout",
                kind="engineering_warning",
                message="至少一个唯一 pad-drill 关联形成确定 breakout，公共环宽投影为 0。",
                affected_output_keys=["geometry.min_annular_ring_mm"],
                source_files=breakout,
            )
        )
    missing_keys = [key for key in OUTPUT_KEYS if key not in outputs]
    if missing_keys:
        raise GerberExpectedOutputMissing(",".join(missing_keys))
    canonical = [outputs[key] for key in OUTPUT_KEYS]
    return GerberResult(
        schema_version="1.0",
        run_id=job.run_id,
        job_material_id=job.job_material_id,
        outputs=canonical,
        checks=checks,
        errors=errors,
    )


def _layer_outputs(active, roles, copper_indexes, coverage):
    copper_count = len(
        {
            copper_indexes[item.relative_path]
            for item in active
            if roles[item.relative_path] in _COPPER_ROLES
        }
    )
    solder = _sides(active, roles, LayerRole.TOP_SOLDER_MASK, LayerRole.BOTTOM_SOLDER_MASK)
    silk = _sides(active, roles, LayerRole.TOP_SILKSCREEN, LayerRole.BOTTOM_SILKSCREEN)
    return {
        "layers.copper_layer_count": _role_output(
            "layers.copper_layer_count", copper_count if copper_count else None, coverage
        ),
        "layers.solder_mask_sides": _role_output(
            "layers.solder_mask_sides", solder or None, coverage
        ),
        "layers.silkscreen_sides": _role_output("layers.silkscreen_sides", silk or None, coverage),
    }


def _sides(active, roles, top_role, bottom_role):
    values = []
    if any(roles[item.relative_path] == top_role for item in active):
        values.append("TOP")
    if any(roles[item.relative_path] == bottom_role for item in active):
        values.append("BOT")
    return values


def _role_output(key, value, coverage):
    if value is None and not coverage.has(key):
        return _not_applicable(key)
    return _candidate_output(key, value, coverage)


def _bounds_output(members, geometries, coverage):
    if not members:
        return _not_applicable("geometry.job_bounds_mm")
    values = []
    sources = []
    for member in members:
        layer = geometries.get(member.relative_path)
        if layer is None or layer.geometry.is_empty:
            continue
        numbers = tuple(float(item) for item in layer.geometry.bounds)
        if len(numbers) != 4 or not all(
            math.isfinite(item) and abs(item) <= 1_000_000 for item in numbers
        ):
            coverage.affect(
                {"geometry.job_bounds_mm"},
                member.relative_path,
                "该 Gerber 层最终可见图像的 bounds 非有限或超过首版坐标边界。",
            )
            continue
        values.append(numbers)
        sources.append(member.relative_path)
    if not values and not coverage.has("geometry.job_bounds_mm"):
        return _not_applicable("geometry.job_bounds_mm")
    value = None
    if values:
        value = {
            "min_x": _round(min(item[0] for item in values)),
            "min_y": _round(min(item[1] for item in values)),
            "max_x": _round(max(item[2] for item in values)),
            "max_y": _round(max(item[3] for item in values)),
        }
    return _candidate_output("geometry.job_bounds_mm", value, coverage, sources)


def _trace_width_output(known_copper, unknown_gerber, coverage):
    relevant = [*known_copper, *unknown_gerber]
    if not relevant:
        return _not_applicable("geometry.min_trace_width_mm")
    candidates = []
    sources = []
    for member in relevant:
        for raw in member.parsed.objects:
            if not isinstance(raw, (Line, Arc)):
                continue
            aperture = raw.aperture
            if isinstance(aperture, CircleAperture):
                diameter = float(aperture.unit.convert_to(MM, aperture.diameter))
                if math.isfinite(diameter) and diameter > 0:
                    candidates.append(diameter)
                    sources.append(member.relative_path)
            else:
                coverage.affect(
                    {"geometry.min_trace_width_mm"},
                    member.relative_path,
                    "存在非圆形或 macro draw aperture，最小线宽覆盖不完整。",
                )
    if not candidates and not coverage.has("geometry.min_trace_width_mm"):
        return _not_applicable("geometry.min_trace_width_mm")
    value = _round(min(candidates)) if candidates else None
    return _candidate_output(
        "geometry.min_trace_width_mm", value, coverage, _stable_sources(sources, relevant)
    )


def _geometries(members, coverage, errors, affected_by_path, budget):
    result = {}
    for member in members:
        affected = affected_by_path[member.relative_path]
        try:
            layer = _layer_geometry(member, budget)
            result[member.relative_path] = layer
            if not layer.complete:
                coverage.affect(
                    affected,
                    member.relative_path,
                    "该层存在无法稳定 polygonize 的对象，相关几何覆盖不完整。",
                )
        except _GeometryBudgetExceeded:
            coverage.affect(
                affected,
                member.relative_path,
                "Gerber 几何处理超过首版确定性预算。",
                code="gerber_geometry_budget_exceeded",
            )
            result[member.relative_path] = _LayerGeometry(GeometryCollection(), False)
        except Exception:
            coverage.affect(
                affected,
                member.relative_path,
                "支持集合内的 polygonization 或 topology 处理失败。",
                code=None,
            )
            errors.append(
                GerberToolError(
                    code="gerber_geometry_processing_failed",
                    severity="error",
                    message="Gerber polygonization 或 topology 处理失败。",
                    affected_output_keys=[key for key in OUTPUT_KEYS if key in affected],
                    source_files=[member.relative_path],
                    artifact_paths=[],
                    suggested_action="检查该 Gerber 层的图形对象和极性顺序。",
                )
            )
            result[member.relative_path] = _LayerGeometry(GeometryCollection(), False)
    return result


def _layer_geometry(member: ParsedMember, budget: _GeometryBudget) -> _LayerGeometry:
    budget.ensure_available()
    final = GeometryCollection()
    batch = []
    batch_dark = None

    def flush(current, shapes, dark):
        if not shapes:
            return current
        combined = unary_union(shapes)
        combined = set_precision(combined, _GRID)
        return current.union(combined) if dark else current.difference(combined)

    complete = True
    for obj in member.parsed.objects:
        for primitive in obj.to_primitives(MM):
            shape = _primitive_shape(primitive, budget)
            if shape is None:
                complete = False
                continue
            dark = bool(primitive.polarity_dark)
            if batch_dark is not None and dark != batch_dark:
                final = flush(final, batch, batch_dark)
                batch = []
            batch_dark = dark
            batch.append(shape)
    final = flush(final, batch, batch_dark)
    return _LayerGeometry(set_precision(final, _GRID), complete)


def _primitive_shape(item, budget: _GeometryBudget):
    shape = None
    if isinstance(item, primitives.Line):
        if item.width <= 0:
            return None
        shape = LineString([(item.x1, item.y1), (item.x2, item.y2)]).buffer(
            item.width / 2, cap_style="round"
        )
    elif isinstance(item, primitives.Circle):
        if item.r <= 0:
            return None
        shape = Point(item.x, item.y).buffer(item.r)
    elif isinstance(item, primitives.Rectangle):
        if item.w <= 0 or item.h <= 0:
            return None
        rectangle = box(
            item.x - item.w / 2, item.y - item.h / 2, item.x + item.w / 2, item.y + item.h / 2
        )
        shape = affinity.rotate(rectangle, item.rotation, origin=(item.x, item.y))
    elif isinstance(item, primitives.Arc):
        points = _arc_points(item, budget)
        shape = LineString(points).buffer(item.width / 2, cap_style="round")
    elif isinstance(item, primitives.ArcPoly):
        if any(center is not None for center in item.arc_centers):
            return None
        shape = Polygon(item.outline)
        if not shape.is_valid or shape.is_empty:
            return None
    else:
        return None
    budget.claim_polygon_input_coordinates(int(get_num_coordinates(shape)))
    return shape


def _arc_points(item, budget: _GeometryBudget):
    radius = math.hypot(item.x1 - item.cx, item.y1 - item.cy)
    if radius <= 0 or item.width <= 0:
        raise ValueError("invalid arc")
    start = math.atan2(item.y1 - item.cy, item.x1 - item.cx)
    end = math.atan2(item.y2 - item.cy, item.x2 - item.cx)
    if item.clockwise:
        while end >= start:
            end -= 2 * math.pi
    else:
        while end <= start:
            end += 2 * math.pi
    sweep = end - start
    if item.x1 == item.x2 and item.y1 == item.y2:
        sweep = -2 * math.pi if item.clockwise else 2 * math.pi
    max_angle = 2 * math.acos(max(-1.0, 1 - _CHORD_ERROR / radius))
    segments = max(1, math.ceil(abs(sweep) / max(max_angle, 1e-12)))
    if segments > _MAX_ARC_SEGMENTS:
        budget.exhausted = True
        raise _GeometryBudgetExceeded
    budget.claim_arc_segments(segments)
    return [
        (
            item.cx + radius * math.cos(start + sweep * index / segments),
            item.cy + radius * math.sin(start + sweep * index / segments),
        )
        for index in range(segments + 1)
    ]


def _spacing_output(known_copper, unknown_gerber, geometries, coverage):
    relevant = [*known_copper, *unknown_gerber]
    if not relevant:
        return _not_applicable("geometry.min_copper_spacing_mm")
    candidates = []
    sources = []
    component_count = 0
    nearest_result_count = 0
    budget_exceeded = False
    for member in relevant:
        layer = geometries.get(member.relative_path)
        if layer is None:
            continue
        geometry = layer.geometry
        components = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
        components = [
            item for item in components if item.geom_type == "Polygon" and not item.is_empty
        ]
        if component_count + len(components) > _MAX_SPACING_COMPONENTS:
            coverage.affect(
                {"geometry.min_copper_spacing_mm"},
                member.relative_path,
                "铜间距最终组件数超过首版确定性预算。",
                code="gerber_geometry_budget_exceeded",
            )
            break
        component_count += len(components)
        if len(components) < 2:
            continue
        tree = STRtree(components)
        for start in range(0, len(components), _SPACING_QUERY_BATCH_SIZE):
            batch = components[start : start + _SPACING_QUERY_BATCH_SIZE]
            _, distances = tree.query_nearest(
                batch,
                exclusive=True,
                all_matches=False,
                return_distance=True,
            )
            result_count = len(distances)
            if nearest_result_count + result_count > _MAX_SPACING_NEAREST_RESULTS:
                coverage.affect(
                    {"geometry.min_copper_spacing_mm"},
                    member.relative_path,
                    "铜间距最近邻结果数超过首版确定性预算。",
                    code="gerber_geometry_budget_exceeded",
                )
                budget_exceeded = True
                break
            if result_count:
                candidates.append(min(float(item) for item in distances))
                sources.append(member.relative_path)
                nearest_result_count += result_count
        if budget_exceeded:
            break
    if not candidates and not coverage.has("geometry.min_copper_spacing_mm"):
        return _not_applicable("geometry.min_copper_spacing_mm")
    value = _round(min(candidates)) if candidates else None
    return _candidate_output(
        "geometry.min_copper_spacing_mm", value, coverage, _stable_sources(sources, relevant)
    )


def _drill_objects(known_drill, unknown_drill, coverage):
    holes = []
    routes = []
    for member in [*known_drill, *unknown_drill]:
        for raw in member.parsed.objects:
            obj = raw.converted(MM)
            tool = getattr(obj, "aperture", None)
            if not isinstance(tool, ExcellonTool):
                coverage.affect(
                    _DRILL_MEASURE_KEYS,
                    member.relative_path,
                    "Drill 对象没有稳定圆形 tool 语义。",
                )
                continue
            diameter = float(tool.diameter)
            if not math.isfinite(diameter) or diameter <= 0:
                coverage.affect(
                    _DRILL_MEASURE_KEYS,
                    member.relative_path,
                    "Drill tool 直径无效。",
                )
                continue
            if isinstance(obj, Flash):
                holes.append(
                    _Hole(
                        x=float(obj.x),
                        y=float(obj.y),
                        diameter=diameter,
                        source_file=member.relative_path,
                        plated=tool.plated,
                    )
                )
            elif isinstance(obj, Line):
                length = math.hypot(obj.x2 - obj.x1, obj.y2 - obj.y1) + diameter
                routes.append((diameter, length, member.relative_path))
            else:
                coverage.affect(
                    {"drill.slot_size_summary"},
                    member.relative_path,
                    "存在首版不支持的复合或弧形 route。",
                )
    return holes, routes


def _drill_outputs(known_drill, unknown_drill, holes, routes, coverage):
    relevant = [*known_drill, *unknown_drill]
    if not relevant:
        return {
            key: _not_applicable(key)
            for key in (
                "drill.used_hole_size_summary",
                "drill.used_hole_quantity_summary",
                "drill.slot_size_summary",
                "drill.min_used_hole_diameter_mm",
            )
        }
    diameters = sorted({_round(item.diameter) for item in holes})
    counts = Counter(_round(item.diameter) for item in holes)
    size_sources = _stable_sources([item.source_file for item in holes], relevant)
    if holes or coverage.has("drill.used_hole_size_summary"):
        size_output = _candidate_output(
            "drill.used_hole_size_summary", diameters or None, coverage, size_sources
        )
        quantity_output = _candidate_output(
            "drill.used_hole_quantity_summary",
            [{"diameter_mm": diameter, "quantity": counts[diameter]} for diameter in diameters]
            or None,
            coverage,
            size_sources,
        )
        minimum_output = _candidate_output(
            "drill.min_used_hole_diameter_mm",
            diameters[0] if diameters else None,
            coverage,
            size_sources,
        )
    else:
        size_output = _not_applicable("drill.used_hole_size_summary")
        quantity_output = _not_applicable("drill.used_hole_quantity_summary")
        minimum_output = _not_applicable("drill.min_used_hole_diameter_mm")
    slot_counts = Counter((_round(width), _round(length)) for width, length, _ in routes)
    slot_sources = _stable_sources([item[2] for item in routes], relevant)
    if routes or coverage.has("drill.slot_size_summary"):
        slot_output = _candidate_output(
            "drill.slot_size_summary",
            [
                {"width_mm": width, "length_mm": length, "quantity": quantity}
                for (width, length), quantity in sorted(slot_counts.items())
            ]
            or None,
            coverage,
            slot_sources,
        )
    else:
        slot_output = _not_applicable("drill.slot_size_summary")
    return {
        "drill.used_hole_size_summary": size_output,
        "drill.used_hole_quantity_summary": quantity_output,
        "drill.slot_size_summary": slot_output,
        "drill.min_used_hole_diameter_mm": minimum_output,
    }


def _copper_pads(known_copper, coverage):
    pads = []
    for member in known_copper:
        for raw in member.parsed.objects:
            if not isinstance(raw, Flash):
                continue
            obj = raw.converted(MM)
            aperture = obj.aperture
            if isinstance(aperture, CircleAperture):
                diameter = float(aperture.unit.convert_to(MM, aperture.diameter))
                pads.append(_Pad(obj.x, obj.y, "circle", diameter, diameter, member.relative_path))
            elif isinstance(aperture, RectangleAperture):
                pads.append(
                    _Pad(
                        obj.x,
                        obj.y,
                        "rectangle",
                        float(aperture.unit.convert_to(MM, aperture.w)),
                        float(aperture.unit.convert_to(MM, aperture.h)),
                        member.relative_path,
                    )
                )
            else:
                coverage.affect(
                    {"geometry.min_annular_ring_mm"},
                    member.relative_path,
                    "存在首版无法唯一关联的 macro 或复杂 pad。",
                )
    return pads


def _annular_output(
    known_copper,
    known_drill,
    unknown_gerber,
    unknown_drill,
    holes,
    pads,
    coverage,
):
    relevant = bool(known_copper or unknown_gerber) and bool(known_drill or unknown_drill)
    if not relevant:
        return _not_applicable("geometry.min_annular_ring_mm"), []
    candidates = []
    sources = []
    breakout = []
    candidate_relations = 0
    budget_exceeded = False
    pads_by_layer = defaultdict(list)
    for pad in pads:
        pads_by_layer[pad.source_file].append(pad)
    pad_indexes = {}
    for member in known_copper:
        layer_file = member.relative_path
        layer_pads = pads_by_layer[layer_file]
        pad_indexes[layer_file] = (
            layer_pads,
            STRtree(
                [
                    box(
                        pad.x - pad.width / 2,
                        pad.y - pad.height / 2,
                        pad.x + pad.width / 2,
                        pad.y + pad.height / 2,
                    )
                    for pad in layer_pads
                ]
            )
            if layer_pads
            else None,
        )
    for hole in holes:
        for member in known_copper:
            layer_file = member.relative_path
            layer_pads, tree = pad_indexes[layer_file]
            indexes = (
                []
                if tree is None
                else sorted(int(index) for index in tree.query(Point(hole.x, hole.y)))
            )
            if candidate_relations + len(indexes) > _MAX_ANNULAR_CANDIDATE_RELATIONS:
                coverage.affect(
                    {"geometry.min_annular_ring_mm"},
                    layer_file,
                    "环宽 pad-hole 候选关系数超过首版确定性预算。",
                    code="gerber_geometry_budget_exceeded",
                )
                budget_exceeded = True
                break
            candidate_relations += len(indexes)
            matching = [
                layer_pads[index]
                for index in indexes
                if _pad_contains(layer_pads[index], hole.x, hole.y)
            ]
            if len(matching) == 1:
                ring = _ring(matching[0], hole)
                candidates.append(max(0.0, ring))
                sources.extend([layer_file, hole.source_file])
                if ring < 0:
                    breakout.extend([layer_file, hole.source_file])
            elif len(matching) > 1:
                coverage.affect(
                    {"geometry.min_annular_ring_mm"},
                    layer_file,
                    "同一孔中心存在多个可行 pad，环宽关联不唯一。",
                )
            else:
                coverage.affect(
                    {"geometry.min_annular_ring_mm"},
                    layer_file,
                    "普通孔在该铜层没有可唯一关联的支持 pad，环宽覆盖不完整。",
                )
        if budget_exceeded:
            break
    if not candidates and not coverage.has("geometry.min_annular_ring_mm"):
        return _not_applicable("geometry.min_annular_ring_mm"), []
    value = _round(min(candidates)) if candidates else None
    members = [*known_copper, *known_drill, *unknown_gerber, *unknown_drill]
    return (
        _candidate_output(
            "geometry.min_annular_ring_mm", value, coverage, _stable_sources(sources, members)
        ),
        _stable_sources(breakout, members),
    )


def _pad_contains(pad, x, y):
    dx = x - pad.x
    dy = y - pad.y
    if pad.shape == "circle":
        return math.hypot(dx, dy) <= pad.width / 2
    return abs(dx) <= pad.width / 2 and abs(dy) <= pad.height / 2


def _ring(pad, hole):
    radius = hole.diameter / 2
    if pad.shape == "circle":
        return (pad.width - hole.diameter) / 2 - math.hypot(hole.x - pad.x, hole.y - pad.y)
    nearest = min(
        pad.width / 2 - abs(hole.x - pad.x),
        pad.height / 2 - abs(hole.y - pad.y),
    )
    return nearest - radius


def _plating_outputs(job, holes, coverage):
    del coverage
    explicit_plated = [item for item in holes if item.plated is True]
    explicit_non_plated = [item for item in holes if item.plated is False]
    gtd_text = "\n".join(
        item.metadata_text or "" for item in job.members if item.detected_type == "gtd"
    ).casefold()
    non_plated_pattern = r"\b(?:npth|non[-_\s]*plated)\b"
    gtd_non_plated = re.search(non_plated_pattern, gtd_text) is not None
    plated_only_text = re.sub(non_plated_pattern, " ", gtd_text)
    gtd_plated = re.search(r"\b(?:pth|plated)\b", plated_only_text) is not None
    plated_sources = [item.source_file for item in explicit_plated]
    non_plated_sources = [item.source_file for item in explicit_non_plated]
    if gtd_plated:
        plated_sources.extend(
            item.relative_path for item in job.members if item.detected_type == "gtd"
        )
    if gtd_non_plated:
        non_plated_sources.extend(
            item.relative_path for item in job.members if item.detected_type == "gtd"
        )
    ordered = list(job.members)
    return {
        "drill.plating_summary.plated": (
            GerberOutput(
                key="drill.plating_summary.plated",
                applicable=True,
                value="存在明确 PTH/金属化孔证据",
                unit=None,
                coverage="complete",
                source_files=_stable_sources(plated_sources, ordered),
            )
            if plated_sources
            else _not_applicable("drill.plating_summary.plated")
        ),
        "drill.plating_summary.non_plated": (
            GerberOutput(
                key="drill.plating_summary.non_plated",
                applicable=True,
                value="存在明确 NPTH/非金属化孔证据",
                unit=None,
                coverage="complete",
                source_files=_stable_sources(non_plated_sources, ordered),
            )
            if non_plated_sources
            else _not_applicable("drill.plating_summary.non_plated")
        ),
    }


def _candidate_output(key, value, coverage, sources=None):
    incomplete = coverage.has(key)
    state = (
        "partial" if value is not None and incomplete else "none" if value is None else "complete"
    )
    return GerberOutput(
        key=key,
        applicable=True,
        value=value,
        unit=(
            "mm"
            if key
            in {
                "geometry.job_bounds_mm",
                "geometry.min_trace_width_mm",
                "geometry.min_copper_spacing_mm",
                "drill.used_hole_size_summary",
                "drill.slot_size_summary",
                "drill.min_used_hole_diameter_mm",
                "geometry.min_annular_ring_mm",
            }
            else None
        ),
        coverage=state,
        source_files=list(sources or sorted(coverage.files_by_key.get(key, ()))),
    )


def _not_applicable(key):
    return GerberOutput(
        key=key,
        applicable=False,
        value=None,
        unit=(
            "mm"
            if key
            in {
                "geometry.job_bounds_mm",
                "geometry.min_trace_width_mm",
                "geometry.min_copper_spacing_mm",
                "drill.used_hole_size_summary",
                "drill.slot_size_summary",
                "drill.min_used_hole_diameter_mm",
                "geometry.min_annular_ring_mm",
            }
            else None
        ),
        coverage="complete",
        source_files=[],
    )


def _round(value):
    quantized = Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return float(quantized)


def _stable_sources(paths, members):
    selected = set(paths)
    return [item.relative_path for item in members if item.relative_path in selected]
