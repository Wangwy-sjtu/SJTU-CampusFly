"""Canonical route representation used by the editor, preview and uploader."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.utils import (
    TRACK_POINT_DECIMAL_PLACES,
    ValidationError,
    haversine_distance,
    validate_coordinate,
    validate_route,
)


ROUTE_VERSION = 1
DEFAULT_CAMPUS = "sjtu_minhang"
DEFAULT_COORDINATE_SYSTEM = "wgs84"
MAX_STROKE_GAP_METERS = 25.0


def make_point(latitude: Any, longitude: Any) -> dict[str, float]:
    lat, lon = validate_coordinate(latitude, longitude)
    return {"latitude": lat, "longitude": lon}


def _point_from_any(value: Any) -> dict[str, float]:
    if isinstance(value, Mapping):
        latitude = value.get("latitude", value.get("lat"))
        longitude = value.get("longitude", value.get("lon", value.get("lng")))
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        # GeoJSON-like pairs are longitude, latitude.
        longitude, latitude = value[0], value[1]
    else:
        raise ValidationError("路线点格式无效。")
    return make_point(latitude, longitude)


def normalise_route(route: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy of a route in the stable JSON representation.

    Empty strokes are removed, while repeated points and closed loops are
    retained so that a save/load round trip does not change route semantics.
    """
    if route is None:
        route = {}
    if not isinstance(route, Mapping):
        raise ValidationError("路线数据必须是对象。")

    raw_strokes = route.get("strokes", [])
    if not isinstance(raw_strokes, list):
        raise ValidationError("路线 strokes 必须是列表。")

    strokes: list[list[dict[str, float]]] = []
    for raw_stroke in raw_strokes:
        if isinstance(raw_stroke, Mapping):
            raw_points = raw_stroke.get("points", [])
        else:
            raw_points = raw_stroke
        if not isinstance(raw_points, list):
            raise ValidationError("路线笔画必须是点列表。")
        if raw_points:
            strokes.append([_point_from_any(point) for point in raw_points])

    result = {
        "version": int(route.get("version", ROUTE_VERSION)),
        "campus": str(route.get("campus", DEFAULT_CAMPUS)),
        "coordinate_system": str(route.get("coordinate_system", DEFAULT_COORDINATE_SYSTEM)),
        "provider": str(route.get("provider", "offline-grid")),
        "strokes": strokes,
    }
    # Optional graph provenance is part of the route snapshot when a user
    # chooses verified roads.  Keep it through save/load and normalization so
    # preview and upload consume the same edge sequence.
    graph_meta = route.get("road_graph")
    if isinstance(graph_meta, Mapping):
        migrated_graph = copy.deepcopy(dict(graph_meta))
        # ``select`` was the removed click-to-select editor mode.  Keep the
        # route geometry and graph provenance, but load the snapshot into the
        # remaining node-snap editor mode.
        if migrated_graph.get("mode") == "select":
            migrated_graph["mode"] = "snap"
        result["road_graph"] = migrated_graph
    source_meta = route.get("route_source")
    if isinstance(source_meta, Mapping):
        # Keep only provider provenance that is safe to save with a route.
        # Geometry itself is already represented by ``strokes``; arbitrary
        # response fields must not be copied into route files.
        source: dict[str, Any] = {}
        for field in ("provider", "endpoint", "coordinate_system", "route_id"):
            if source_meta.get(field) is not None:
                source[field] = str(source_meta[field])
        for field in ("distance_m", "duration_min"):
            if source_meta.get(field) is not None:
                try:
                    value = float(source_meta[field])
                except (TypeError, ValueError) as exc:
                    raise ValidationError(f"路线 route_source.{field} 无效。") from exc
                if not math.isfinite(value):
                    raise ValidationError(f"路线 route_source.{field} 无效。")
                source[field] = value
        names = source_meta.get("road_names")
        if isinstance(names, list):
            source["road_names"] = [str(name).strip() for name in names if str(name).strip()]
        if source:
            result["route_source"] = source
    validate_route(result, require_path=False)
    return result


def route_from_legacy_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Migrate old START_*/END_* settings into one two-point stroke."""
    if config.get("ROUTE") is not None:
        return normalise_route(config["ROUTE"])
    try:
        start = make_point(config["START_LATITUDE"], config["START_LONGITUDE"])
        end = make_point(config["END_LATITUDE"], config["END_LONGITUDE"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(f"旧路线配置无法迁移: {exc}") from exc
    return {
        "version": ROUTE_VERSION,
        "campus": DEFAULT_CAMPUS,
        "coordinate_system": str(config.get("COORDINATE_SYSTEM", DEFAULT_COORDINATE_SYSTEM)),
        "provider": "legacy-config",
        "strokes": [[start, end]],
    }


def clone_route(route: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(normalise_route(route))


def route_points(route: Mapping[str, Any]) -> list[dict[str, float]]:
    canonical = normalise_route(route)
    return [point for stroke in canonical["strokes"] for point in stroke]


def route_endpoints(route: Mapping[str, Any]) -> tuple[dict[str, float] | None, dict[str, float] | None]:
    canonical = normalise_route(route)
    nonempty = [stroke for stroke in canonical["strokes"] if stroke]
    if not nonempty:
        return None, None
    return copy.deepcopy(nonempty[0][0]), copy.deepcopy(nonempty[-1][-1])


def route_distance_m(route: Mapping[str, Any]) -> float:
    canonical = normalise_route(route)
    distance = 0.0
    for stroke in canonical["strokes"]:
        for first, second in zip(stroke, stroke[1:]):
            distance += haversine_distance(
                first["latitude"],
                first["longitude"],
                second["latitude"],
                second["longitude"],
            )
    return distance


def route_metrics(route: Mapping[str, Any], speed_mps: float) -> dict[str, Any]:
    distance_m = route_distance_m(route)
    duration_s = distance_m / float(speed_mps) if speed_mps > 0 else math.inf
    start, end = route_endpoints(route)
    return {
        "distance_m": distance_m,
        "duration_s": duration_s,
        "start": start,
        "end": end,
        "stroke_count": len(normalise_route(route)["strokes"]),
    }


def validate_stroke_continuity(
    route: Mapping[str, Any],
    *,
    max_gap_m: float = MAX_STROKE_GAP_METERS,
) -> None:
    """Reject disconnected pen strokes before an upload can be prepared.

    Separate strokes remain useful for preview and editing.  A large gap
    between strokes must be resolved by the user, however, because the server
    payload has no safe way to infer a connecting run segment.
    """
    canonical = normalise_route(route)
    if not math.isfinite(float(max_gap_m)) or max_gap_m < 0:
        raise ValidationError("笔画间距阈值必须是非负有限数字。")
    nonempty = [stroke for stroke in canonical["strokes"] if stroke]
    for index, (previous, current) in enumerate(zip(nonempty, nonempty[1:]), start=1):
        gap = haversine_distance(
            previous[-1]["latitude"], previous[-1]["longitude"],
            current[0]["latitude"], current[0]["longitude"],
        )
        if gap > max_gap_m:
            raise ValidationError(
                f"第 {index} 笔与第 {index + 1} 笔相距约 {gap:.1f} 米；"
                "请从上一笔末端附近续画，或清空后重画连续路线。"
            )


def simplify_stroke(stroke: Iterable[Mapping[str, Any]], min_spacing_m: float = 1.0) -> list[dict[str, float]]:
    """Remove tiny pointer jitter while preserving corners and endpoints."""
    points = [_point_from_any(point) for point in stroke]
    if len(points) <= 2:
        return points
    if not math.isfinite(min_spacing_m) or min_spacing_m < 0:
        raise ValidationError("去抖距离必须是非负有限数字。")

    kept = [points[0]]
    for point in points[1:-1]:
        if haversine_distance(
            kept[-1]["latitude"], kept[-1]["longitude"],
            point["latitude"], point["longitude"],
        ) >= min_spacing_m:
            kept.append(point)
    if points[-1] != kept[-1]:
        kept.append(points[-1])
    return kept


def simplify_route(route: Mapping[str, Any], min_spacing_m: float = 1.0) -> dict[str, Any]:
    canonical = normalise_route(route)
    canonical["strokes"] = [
        simplify_stroke(stroke, min_spacing_m=min_spacing_m)
        for stroke in canonical["strokes"]
    ]
    canonical["strokes"] = [stroke for stroke in canonical["strokes"] if stroke]
    return canonical


def save_route_file(route: Mapping[str, Any], path: str | Path) -> None:
    canonical = normalise_route(route)
    validate_route(canonical, require_path=False)
    Path(path).write_text(
        json.dumps(canonical, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_route_file(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"路线文件无法读取: {exc}") from exc
    route = normalise_route(payload)
    validate_route(route, require_path=True)
    return route


def rounded_point(point: Mapping[str, Any]) -> dict[str, float]:
    lat, lon = validate_coordinate(point.get("latitude"), point.get("longitude"))
    return {
        "latitude": float(f"{lat:.{TRACK_POINT_DECIMAL_PLACES}f}"),
        "longitude": float(f"{lon:.{TRACK_POINT_DECIMAL_PLACES}f}"),
    }
