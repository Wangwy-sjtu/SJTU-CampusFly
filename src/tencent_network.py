"""Collect and deduplicate Tencent walking-route samples.

The official walking API answers two-point routing requests.  This module
keeps those requests resumable and turns only the returned step geometries into
an explicitly partial candidate graph.  It never clips a route at the campus
boundary, inserts a straight connector, or joins lines merely because they
cross in the map plane.
"""

from __future__ import annotations

import copy
import datetime as _datetime
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from src.tencent_roads import TencentRoadError, request_tencent_walking_routes
from src.utils import haversine_distance


NETWORK_VERSION = 1
TENCENT_DIRECTION_ENDPOINT = "/ws/direction/v1/walking/"
DEFAULT_WORKING_BOUNDS = {
    "south": 31.0182,
    "west": 121.4179,
    "north": 31.0384,
    "east": 121.4467,
}
DEDUP_TOLERANCE_M = 12.0
# RoadGraph validates that an edge endpoint is within 5 m of its node.  Keep
# the clustering tolerance below that contract so a candidate graph can be
# loaded for review without silently stretching an edge.
NODE_TOLERANCE_M = 4.0
MIN_EDGE_LENGTH_M = 4.0


def utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).replace(microsecond=0).isoformat()


def _point(value: Mapping[str, Any]) -> dict[str, float]:
    return {
        "latitude": float(value["latitude"]),
        "longitude": float(value["longitude"]),
    }


def _point_key(value: Mapping[str, Any]) -> tuple[float, float]:
    point = _point(value)
    return round(point["latitude"], 7), round(point["longitude"], 7)


def query_id(from_point: Mapping[str, Any], to_point: Mapping[str, Any]) -> str:
    """Stable cache id derived only from coordinates, never from the Key."""

    payload = json.dumps(
        {"from": _point_key(from_point), "to": _point_key(to_point)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def geometry_length_m(geometry: Sequence[Mapping[str, Any]]) -> float:
    return sum(
        haversine_distance(
            first["latitude"], first["longitude"],
            second["latitude"], second["longitude"],
        )
        for first, second in zip(geometry, geometry[1:])
    )


def _local_xy(point: Mapping[str, Any], origin: Mapping[str, Any]) -> tuple[float, float]:
    lat_scale = 111_320.0
    lon_scale = lat_scale * math.cos(math.radians(float(origin["latitude"])))
    return (
        (float(point["longitude"]) - float(origin["longitude"])) * lon_scale,
        (float(point["latitude"]) - float(origin["latitude"])) * lat_scale,
    )


def point_to_geometry_distance_m(point: Mapping[str, Any], geometry: Sequence[Mapping[str, Any]]) -> float:
    if not geometry:
        return float("inf")
    if len(geometry) == 1:
        return haversine_distance(
            point["latitude"], point["longitude"],
            geometry[0]["latitude"], geometry[0]["longitude"],
        )
    origin = geometry[0]
    px, py = _local_xy(point, origin)
    best = float("inf")
    for first, second in zip(geometry, geometry[1:]):
        ax, ay = _local_xy(first, origin)
        bx, by = _local_xy(second, origin)
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-9:
            distance = math.hypot(px - ax, py - ay)
        else:
            fraction = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
            distance = math.hypot(px - (ax + fraction * dx), py - (ay + fraction * dy))
        best = min(best, distance)
    return best


def _dedupe_geometry(geometry: Iterable[Mapping[str, Any]], min_spacing_m: float = 1.0) -> list[dict[str, float]]:
    result: list[dict[str, float]] = []
    for raw in geometry:
        point = _point(raw)
        if not result or haversine_distance(
            result[-1]["latitude"], result[-1]["longitude"],
            point["latitude"], point["longitude"],
        ) >= min_spacing_m:
            result.append(point)
    return result


def _geometry_overlap_ratio(
    candidate: Sequence[Mapping[str, Any]],
    existing: Sequence[Mapping[str, Any]],
    tolerance_m: float = DEDUP_TOLERANCE_M,
) -> float:
    """Estimate the fraction of candidate length already covered by a line."""

    if len(candidate) < 2 or len(existing) < 2:
        return 0.0
    covered = 0.0
    for first, second in zip(candidate, candidate[1:]):
        segment_length = haversine_distance(
            first["latitude"], first["longitude"],
            second["latitude"], second["longitude"],
        )
        if segment_length <= 0:
            continue
        samples = max(2, int(math.ceil(segment_length / 10.0)) + 1)
        hits = 0
        for index in range(samples):
            fraction = index / (samples - 1)
            sample = {
                "latitude": first["latitude"] + (second["latitude"] - first["latitude"]) * fraction,
                "longitude": first["longitude"] + (second["longitude"] - first["longitude"]) * fraction,
            }
            if point_to_geometry_distance_m(sample, existing) <= tolerance_m:
                hits += 1
        if hits / samples >= 0.7:
            covered += segment_length
    total = geometry_length_m(candidate)
    return covered / total if total else 0.0


def _within_bounds(geometry: Sequence[Mapping[str, Any]], bounds: Mapping[str, Any]) -> bool:
    return all(
        float(bounds["south"]) <= float(point["latitude"]) <= float(bounds["north"])
        and float(bounds["west"]) <= float(point["longitude"]) <= float(bounds["east"])
        for point in geometry
    )


def _safe_overlay(overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only the parsed route fields; no request URL or key is retained."""

    return copy.deepcopy(dict(overlay))


def empty_cache() -> dict[str, Any]:
    return {
        "version": NETWORK_VERSION,
        "provider": "tencent-webservice",
        "endpoint": TENCENT_DIRECTION_ENDPOINT,
        "coordinate_system": "gcj02",
        "queries": [],
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def load_query_cache(path: str | os.PathLike[str]) -> dict[str, Any]:
    cache_path = Path(path)
    if not cache_path.exists():
        return empty_cache()
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TencentRoadError(f"腾讯路线缓存无法读取：{cache_path.name}") from exc
    if not isinstance(payload, dict):
        raise TencentRoadError("腾讯路线缓存必须是 JSON 对象。")
    payload.setdefault("queries", [])
    if not isinstance(payload["queries"], list):
        raise TencentRoadError("腾讯路线缓存 queries 必须是列表。")
    return payload


def save_query_cache(cache: Mapping[str, Any], path: str | os.PathLike[str]) -> None:
    cache_path = Path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(cache_path.name + ".tmp")
    payload = copy.deepcopy(dict(cache))
    payload["updated_at"] = utc_now()
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temp_path.replace(cache_path)


def collect_query_batch(
    key: str,
    specifications: Sequence[Mapping[str, Any]],
    *,
    cache_path: str | os.PathLike[str],
    max_requests: int = 10,
    delay_seconds: float = 1.0,
    timeout: float = 15.0,
    sleep_fn=time.sleep,
) -> dict[str, Any]:
    """Collect a bounded serial batch with cache reuse and resumable writes."""

    if max_requests <= 0:
        return {
            "requested": 0,
            "cache_hits": 0,
            "successes": 0,
            "failures": 0,
            "new_records": 0,
            "stopped_on_provider_limit": False,
        }
    cache = load_query_cache(cache_path)
    records = cache["queries"]
    known = {str(record.get("query_id")): record for record in records if isinstance(record, Mapping)}
    stats = {
        "requested": 0,
        "cache_hits": 0,
        "successes": 0,
        "failures": 0,
        "new_records": 0,
        "stopped_on_provider_limit": False,
    }
    for specification in specifications:
        if stats["requested"] >= max_requests:
            break
        from_point = _point(specification["from"])
        to_point = _point(specification["to"])
        identifier = query_id(from_point, to_point)
        if identifier in known:
            stats["cache_hits"] += 1
            continue
        stats["requested"] += 1
        record: dict[str, Any] = {
            "query_id": identifier,
            "label": str(specification.get("label") or identifier),
            "region": str(specification.get("region") or "unspecified"),
            "from": from_point,
            "to": to_point,
            "requested_at": utc_now(),
        }
        try:
            overlay = request_tencent_walking_routes(
                key,
                from_point,
                to_point,
                timeout=timeout,
            )
            record["status"] = "success"
            record["overlay"] = _safe_overlay(overlay)
            stats["successes"] += 1
            backoff = delay_seconds
        except TencentRoadError as exc:
            # Do not keep exception text: it may contain provider-specific
            # details or a rendered URL from a future HTTP client.
            record["status"] = "failure"
            record["error"] = "腾讯步行路线请求失败或响应不可用。"
            stats["failures"] += 1
            backoff = min(max(delay_seconds * 2.0, 1.0), 8.0)
            detail = str(exc).lower()
            # A route-specific miss is safe to record and continue.  A clear
            # quota, permission, or rate-limit response must stop the batch so
            # the collector never works around a provider restriction.
            provider_limit_tokens = (
                "quota", "limit", "rate", "频率", "限流", "限额", "额度", "配额",
                "权限", "permission", "key无效", "key invalid", "余额",
            )
            if any(token in detail for token in provider_limit_tokens):
                stats["stopped_on_provider_limit"] = True
        records.append(record)
        known[identifier] = record
        stats["new_records"] += 1
        save_query_cache(cache, cache_path)
        if stats["stopped_on_provider_limit"]:
            break
        if sleep_fn and stats["requested"] < max_requests:
            sleep_fn(max(0.0, backoff))
    return stats


def _new_node_id(index: int) -> str:
    return f"tencent_node_{index:04d}"


def _new_edge_id(index: int) -> str:
    return f"tencent_edge_{index:04d}"


def _find_or_add_node(nodes: list[dict[str, Any]], point: Mapping[str, Any]) -> str:
    for node in nodes:
        if haversine_distance(
            node["latitude"], node["longitude"],
            point["latitude"], point["longitude"],
        ) <= NODE_TOLERANCE_M:
            return str(node["id"])
    node = {
        "id": _new_node_id(len(nodes)),
        "latitude": float(point["latitude"]),
        "longitude": float(point["longitude"]),
        "kind": "provider_step_endpoint",
        "source": "Tencent WebService direction/v1/walking",
        "verification_status": "provider_route_partial",
        "coordinate_system": "gcj02",
    }
    nodes.append(node)
    return str(node["id"])


def build_candidate_graph(
    cache: Mapping[str, Any],
    *,
    bounds: Mapping[str, Any] | None = None,
    collected_at: str | None = None,
) -> dict[str, Any]:
    """Build a partial graph from returned steps without crossing-line joins."""

    working_bounds = dict(bounds or DEFAULT_WORKING_BOUNDS)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    out_of_scope = 0
    failed_records = 0
    successful_records = 0
    query_ids: list[str] = []

    for record in cache.get("queries", []) if isinstance(cache, Mapping) else []:
        if not isinstance(record, Mapping):
            continue
        if record.get("status") != "success" or not isinstance(record.get("overlay"), Mapping):
            if record.get("status") == "failure":
                failed_records += 1
            continue
        successful_records += 1
        query_identifier = str(record.get("query_id") or "")
        if query_identifier:
            query_ids.append(query_identifier)
        overlay = record["overlay"]
        for route in overlay.get("routes", []) if isinstance(overlay.get("routes", []), list) else []:
            if not isinstance(route, Mapping):
                continue
            route_geometry = route.get("geometry", [])
            if not isinstance(route_geometry, list) or len(route_geometry) < 2:
                continue
            if not _within_bounds(route_geometry, working_bounds):
                out_of_scope += 1
                continue
            raw_steps = route.get("steps", [])
            if not isinstance(raw_steps, list):
                continue
            for step_index, step in enumerate(raw_steps):
                if not isinstance(step, Mapping):
                    continue
                geometry = _dedupe_geometry(step.get("geometry", []))
                if len(geometry) < 2 or not _within_bounds(geometry, working_bounds):
                    continue
                length = geometry_length_m(geometry)
                if length < MIN_EDGE_LENGTH_M:
                    continue
                road_name = str(step.get("road_name") or "").strip()
                source = {
                    "query_id": query_identifier,
                    "route_id": int(route.get("id", 0)),
                    "step_index": step_index,
                    "road_name": road_name,
                }
                matched = None
                for edge in edges:
                    candidate_ratio = _geometry_overlap_ratio(geometry, edge["geometry"])
                    existing_ratio = _geometry_overlap_ratio(edge["geometry"], geometry)
                    if candidate_ratio >= 0.75 and existing_ratio >= 0.50:
                        matched = edge
                        break
                if matched is not None:
                    matched.setdefault("sources", []).append(source)
                    if road_name and road_name not in matched.setdefault("road_names", []):
                        matched["road_names"].append(road_name)
                    if length > float(matched.get("length_m", 0.0)):
                        # A longer overlapping response may extend one end of
                        # the same road. Keep its real geometry and move the
                        # explicit endpoint nodes with it; never retain a
                        # stale node from the shorter line.
                        matched["geometry"] = geometry
                        matched["length_m"] = length
                        matched["start"] = _find_or_add_node(nodes, geometry[0])
                        matched["end"] = _find_or_add_node(nodes, geometry[-1])
                    continue
                start_node = _find_or_add_node(nodes, geometry[0])
                end_node = _find_or_add_node(nodes, geometry[-1])
                edges.append({
                    "id": _new_edge_id(len(edges)),
                    "name": road_name or "腾讯步行路线未命名路段",
                    "road_names": [road_name] if road_name else [],
                    "start": start_node,
                    "end": end_node,
                    "one_way": False,
                    "geometry": geometry,
                    "length_m": length,
                    "source": "Tencent WebService direction/v1/walking",
                    "verification_status": "provider_route_partial",
                    "coordinate_system": "gcj02",
                    "sources": [source],
                })

    unique_length = sum(float(edge.get("length_m", 0.0)) for edge in edges)
    return {
        "version": NETWORK_VERSION,
        "metadata": {
            "graph_id": "tencent_walking_sampled_candidate_v1",
            "campus": "sjtu_minhang",
            "coordinate_system": "gcj02",
            "provider": "tencent-webservice",
            "endpoint": TENCENT_DIRECTION_ENDPOINT,
            "verification_status": "provider_route_partial",
            "collected_at": collected_at or utc_now(),
            "working_bounds": working_bounds,
            "query_count": len(query_ids),
            "successful_query_count": successful_records,
            "failed_query_count": failed_records,
            "out_of_scope_route_count": out_of_scope,
            "edge_count": len(edges),
            "node_count": len(nodes),
            "unique_length_m_estimate": unique_length,
            "source_query_ids": query_ids,
            "coverage_note": (
                "由两点步行路线 steps 的真实 polyline 去重形成的候选路网；"
                "仅端点聚类为连接节点，几何相交不会自动连通；"
                "边界外路线整条保留在缓存但不进入图，不截断也不补直线；"
                "不等同交大闵行完整道路数据，需叠图和现场核验后再启用。"
            ),
        },
        "nodes": nodes,
        "edges": edges,
    }


def graph_summary(graph: Mapping[str, Any]) -> dict[str, Any]:
    metadata = graph.get("metadata", {}) if isinstance(graph, Mapping) else {}
    return {
        "nodes": len(graph.get("nodes", [])) if isinstance(graph, Mapping) and isinstance(graph.get("nodes", []), list) else 0,
        "edges": len(graph.get("edges", [])) if isinstance(graph, Mapping) and isinstance(graph.get("edges", []), list) else 0,
        "unique_length_m_estimate": float(metadata.get("unique_length_m_estimate", 0.0)),
        "successful_queries": int(metadata.get("successful_query_count", 0)),
        "failed_queries": int(metadata.get("failed_query_count", 0)),
        "out_of_scope_routes": int(metadata.get("out_of_scope_route_count", 0)),
    }
