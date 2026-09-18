"""Verified road graph primitives for the Minhang route editor.

The graph is deliberately separate from the freehand route model.  A graph
edge is an ordered polyline whose endpoints are explicit nodes.  Two lines
that merely cross on a map are not connected unless the data set declares a
shared node.  This keeps snapping and route planning from inventing a bridge
or a junction.
"""

from __future__ import annotations

import copy
import heapq
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from src.utils import ValidationError, haversine_distance


GRAPH_VERSION = 1
DEFAULT_SNAP_THRESHOLD_M = 35.0
NODE_MATCH_TOLERANCE_M = 5.0


class RoadGraphError(ValidationError):
    """A graph cannot be used safely for snapping or route planning."""


class AmbiguousRoadError(RoadGraphError):
    """A click is close to more than one road and needs a user choice."""

    def __init__(self, candidates: Iterable["SnapCandidate"]) -> None:
        self.candidates = tuple(candidates)
        names = ", ".join(candidate.edge_id for candidate in self.candidates)
        super().__init__(f"路口附近存在多条候选道路：{names}。请点选具体道路。")


class RoadSnapError(RoadGraphError):
    """A stroke leaves the verified graph or jumps between disconnected edges."""


@dataclass(frozen=True)
class SnapCandidate:
    edge_id: str
    distance_m: float
    latitude: float
    longitude: float
    segment_index: int
    fraction: float


def _finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RoadGraphError(f"{label} 必须是数字。") from exc
    if not math.isfinite(number):
        raise RoadGraphError(f"{label} 不能是 NaN 或无穷大。")
    return number


def _point(value: Mapping[str, Any], label: str = "点") -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise RoadGraphError(f"{label} 必须是对象。")
    return {
        "latitude": _finite(value.get("latitude"), f"{label}.latitude"),
        "longitude": _finite(value.get("longitude"), f"{label}.longitude"),
    }


def _point_distance(first: Mapping[str, Any], second: Mapping[str, Any]) -> float:
    return haversine_distance(
        float(first["latitude"]),
        float(first["longitude"]),
        float(second["latitude"]),
        float(second["longitude"]),
    )


def _local_xy(point: Mapping[str, Any], origin: Mapping[str, Any]) -> tuple[float, float]:
    """Return metres east/north around an origin for short road segments."""

    earth_radius = 6_378_137.0
    lat0 = math.radians(float(origin["latitude"]))
    x = math.radians(float(point["longitude"]) - float(origin["longitude"])) * earth_radius * math.cos(lat0)
    y = math.radians(float(point["latitude"]) - float(origin["latitude"])) * earth_radius
    return x, y


def _interpolate(first: Mapping[str, Any], second: Mapping[str, Any], fraction: float) -> dict[str, float]:
    fraction = max(0.0, min(1.0, float(fraction)))
    return {
        "latitude": float(first["latitude"]) + (float(second["latitude"]) - float(first["latitude"])) * fraction,
        "longitude": float(first["longitude"]) + (float(second["longitude"]) - float(first["longitude"])) * fraction,
    }


def _project_to_segment(
    point: Mapping[str, Any],
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> tuple[float, dict[str, float], float]:
    origin = point
    px, py = _local_xy(point, origin)
    ax, ay = _local_xy(first, origin)
    bx, by = _local_xy(second, origin)
    dx, dy = bx - ax, by - ay
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-9:
        fraction = 0.0
        projected = dict(first)
    else:
        fraction = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_squared))
        projected = _interpolate(first, second, fraction)
    distance = _point_distance(point, projected)
    return distance, projected, fraction


def _edge_length(edge: Mapping[str, Any]) -> float:
    geometry = edge["geometry"]
    return sum(_point_distance(first, second) for first, second in zip(geometry, geometry[1:]))


class RoadGraph:
    """Validated node/edge graph with conservative snapping and planning."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise RoadGraphError("路网数据必须是对象。")
        self._payload = copy.deepcopy(dict(payload))
        self.version = int(payload.get("version", GRAPH_VERSION))
        self.metadata = copy.deepcopy(dict(payload.get("metadata") or {}))
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self._normalise()
        # The validated dictionaries now own the graph. Do not retain a second
        # complete copy of its geometry and provider provenance.
        del self._payload

    @classmethod
    def from_file(cls, path: str) -> "RoadGraph":
        import json

        with open(path, "r", encoding="utf-8") as handle:
            return cls(json.load(handle))

    def _normalise(self) -> None:
        raw_nodes = self._payload.get("nodes") or []
        if isinstance(raw_nodes, Mapping):
            raw_nodes = list(raw_nodes.values())
        if not isinstance(raw_nodes, list):
            raise RoadGraphError("路网 nodes 必须是列表。")
        for index, raw in enumerate(raw_nodes):
            if not isinstance(raw, Mapping):
                raise RoadGraphError(f"路网节点 {index} 必须是对象。")
            node_id = str(raw.get("id", "")).strip()
            if not node_id or node_id in self.nodes:
                raise RoadGraphError(f"路网节点 ID 无效或重复：{node_id!r}。")
            point = _point(raw, f"节点 {node_id}")
            node = dict(raw)
            node.update({"id": node_id, **point})
            self.nodes[node_id] = node

        raw_edges = self._payload.get("edges") or []
        if isinstance(raw_edges, Mapping):
            raw_edges = list(raw_edges.values())
        if not isinstance(raw_edges, list):
            raise RoadGraphError("路网 edges 必须是列表。")
        for index, raw in enumerate(raw_edges):
            if not isinstance(raw, Mapping):
                raise RoadGraphError(f"路网边 {index} 必须是对象。")
            edge_id = str(raw.get("id", "")).strip()
            if not edge_id or edge_id in self.edges:
                raise RoadGraphError(f"路网边 ID 无效或重复：{edge_id!r}。")
            start = str(raw.get("start", "")).strip()
            end = str(raw.get("end", "")).strip()
            if start not in self.nodes or end not in self.nodes:
                raise RoadGraphError(f"路网边 {edge_id} 的端点未定义：{start!r}->{end!r}。")
            raw_geometry = raw.get("geometry")
            if not isinstance(raw_geometry, list) or len(raw_geometry) < 2:
                raise RoadGraphError(f"路网边 {edge_id} 必须有至少两个有序点。")
            geometry = [_point(item, f"边 {edge_id} 第 {item_index + 1} 点") for item_index, item in enumerate(raw_geometry)]
            if _point_distance(geometry[0], self.nodes[start]) > NODE_MATCH_TOLERANCE_M:
                raise RoadGraphError(f"路网边 {edge_id} 的首点未落在起点节点上。")
            if _point_distance(geometry[-1], self.nodes[end]) > NODE_MATCH_TOLERANCE_M:
                raise RoadGraphError(f"路网边 {edge_id} 的末点未落在终点节点上。")
            length_m = _edge_length({"geometry": geometry})
            if length_m <= 1.0:
                raise RoadGraphError(f"路网边 {edge_id} 长度过短。")
            edge = dict(raw)
            edge.update({
                "id": edge_id,
                "start": start,
                "end": end,
                "geometry": geometry,
                "one_way": bool(raw.get("one_way", False)),
                "length_m": length_m,
            })
            self.edges[edge_id] = edge

        self._adjacency: dict[str, list[tuple[str, str, bool]]] = {node_id: [] for node_id in self.nodes}
        for edge_id, edge in self.edges.items():
            self._adjacency[edge["start"]].append((edge_id, edge["end"], True))
            if not edge["one_way"]:
                self._adjacency[edge["end"]].append((edge_id, edge["start"], False))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "metadata": copy.deepcopy(self.metadata),
            "nodes": copy.deepcopy(list(self.nodes.values())),
            "edges": copy.deepcopy(list(self.edges.values())),
        }

    def edge_length_m(self, edge_id: str) -> float:
        return float(self.edges[edge_id]["length_m"])

    def nearest_edges(
        self,
        point: Mapping[str, Any],
        max_distance_m: float = DEFAULT_SNAP_THRESHOLD_M,
    ) -> list[SnapCandidate]:
        query = _point(point, "查询点")
        threshold = _finite(max_distance_m, "吸附阈值")
        if threshold <= 0:
            raise RoadGraphError("吸附阈值必须为正数。")
        candidates: list[SnapCandidate] = []
        for edge_id, edge in self.edges.items():
            best: SnapCandidate | None = None
            for segment_index, (first, second) in enumerate(zip(edge["geometry"], edge["geometry"][1:])):
                distance, projected, fraction = _project_to_segment(query, first, second)
                candidate = SnapCandidate(
                    edge_id=edge_id,
                    distance_m=distance,
                    latitude=projected["latitude"],
                    longitude=projected["longitude"],
                    segment_index=segment_index,
                    fraction=fraction,
                )
                if best is None or candidate.distance_m < best.distance_m:
                    best = candidate
            if best is not None and best.distance_m <= threshold:
                candidates.append(best)
        return sorted(candidates, key=lambda item: (item.distance_m, item.edge_id))

    def snap_point(
        self,
        point: Mapping[str, Any],
        max_distance_m: float = DEFAULT_SNAP_THRESHOLD_M,
        ambiguous_margin_m: float = 4.0,
    ) -> SnapCandidate:
        candidates = self.nearest_edges(point, max_distance_m=max_distance_m)
        if not candidates:
            raise RoadSnapError(f"点不在已标注道路 {max_distance_m:.0f} 米范围内，未执行跳接。")
        if len(candidates) > 1 and candidates[1].distance_m - candidates[0].distance_m <= ambiguous_margin_m:
            raise AmbiguousRoadError(candidates[:4])
        return candidates[0]

    def edges_share_node(self, first_edge_id: str, second_edge_id: str) -> bool:
        first = self.edges[first_edge_id]
        second = self.edges[second_edge_id]
        return bool({first["start"], first["end"]} & {second["start"], second["end"]})

    def connected_node(self, first_edge_id: str, second_edge_id: str) -> str | None:
        first = self.edges[first_edge_id]
        second = self.edges[second_edge_id]
        shared = {first["start"], first["end"]} & {second["start"], second["end"]}
        return next(iter(shared), None)

    def snap_stroke(
        self,
        points: Iterable[Mapping[str, Any]],
        max_distance_m: float = DEFAULT_SNAP_THRESHOLD_M,
        min_spacing_m: float = 1.0,
    ) -> tuple[list[dict[str, float]], list[str]]:
        raw_points = [_point(point, "笔画点") for point in points]
        if len(raw_points) < 2:
            raise RoadSnapError("道路画笔至少需要两个点。")
        snapped: list[dict[str, float]] = []
        edge_ids: list[str] = []
        for index, point in enumerate(raw_points):
            candidates = self.nearest_edges(point, max_distance_m=max_distance_m)
            if not candidates:
                raise RoadSnapError(f"第 {index + 1} 个点超出已标注道路吸附阈值，未执行跳接。")
            if len(candidates) > 1 and candidates[1].distance_m - candidates[0].distance_m <= 4.0:
                if edge_ids and any(item.edge_id == edge_ids[-1] for item in candidates[:4]):
                    candidate = next(item for item in candidates if item.edge_id == edge_ids[-1])
                else:
                    raise AmbiguousRoadError(candidates[:4])
            else:
                candidate = candidates[0]
            if edge_ids and candidate.edge_id != edge_ids[-1] and not self.edges_share_node(edge_ids[-1], candidate.edge_id):
                raise RoadSnapError(
                    f"笔画在第 {index + 1} 个点从 {edge_ids[-1]} 跳到不相邻的 {candidate.edge_id}；"
                    "请经过已标注路口后继续。"
                )
            edge_ids.append(candidate.edge_id)
            next_point = {"latitude": candidate.latitude, "longitude": candidate.longitude}
            if not snapped or _point_distance(snapped[-1], next_point) >= min_spacing_m:
                snapped.append(next_point)
        if len(snapped) < 2:
            raise RoadSnapError("吸附后笔画长度不足。")
        return snapped, edge_ids

    def _neighbours(self, node_id: str) -> Iterable[tuple[str, str, bool]]:
        return self._adjacency.get(node_id, ())

    def shortest_path(
        self,
        start_node_id: str,
        end_node_id: str,
        *,
        blocked_edges: Iterable[str] = (),
    ) -> tuple[list[str], list[str], float]:
        if start_node_id not in self.nodes or end_node_id not in self.nodes:
            raise RoadGraphError("路径起点或终点不在当前已核验路网中。")
        distances, previous = self._shortest_paths(start_node_id, end_node_id, blocked_edges)
        if end_node_id not in distances:
            raise RoadGraphError("当前已核验路网中不存在连接这两个节点的路径。")
        edge_ids, node_ids = self._reconstruct_path(start_node_id, end_node_id, previous)
        return edge_ids, node_ids, distances[end_node_id]

    def _shortest_paths(
        self,
        start_node_id: str,
        end_node_id: str | None = None,
        blocked_edges: Iterable[str] = (),
    ) -> tuple[dict[str, float], dict[str, tuple[str, str, bool]]]:
        """One Dijkstra traversal; omit the endpoint to plan all destinations."""
        blocked = {str(edge_id) for edge_id in blocked_edges}
        distances = {start_node_id: 0.0}
        previous: dict[str, tuple[str, str, bool]] = {}
        queue: list[tuple[float, str]] = [(0.0, start_node_id)]
        while queue:
            distance, node_id = heapq.heappop(queue)
            if distance > distances.get(node_id, math.inf) + 1e-6:
                continue
            if node_id == end_node_id:
                break
            for edge_id, neighbour, forward in self._neighbours(node_id):
                if edge_id in blocked:
                    continue
                candidate_distance = distance + self.edge_length_m(edge_id)
                if candidate_distance < distances.get(neighbour, math.inf):
                    distances[neighbour] = candidate_distance
                    previous[neighbour] = (node_id, edge_id, forward)
                    heapq.heappush(queue, (candidate_distance, neighbour))
        return distances, previous

    @staticmethod
    def _reconstruct_path(
        start_node_id: str,
        end_node_id: str,
        previous: Mapping[str, tuple[str, str, bool]],
    ) -> tuple[list[str], list[str]]:
        edge_ids: list[str] = []
        node_ids: list[str] = [end_node_id]
        current = end_node_id
        while current != start_node_id:
            previous_node, edge_id, _forward = previous[current]
            edge_ids.append(edge_id)
            node_ids.append(previous_node)
            current = previous_node
        edge_ids.reverse(); node_ids.reverse()
        return edge_ids, node_ids

    def path_points(self, edge_ids: Iterable[str], start_node_id: str | None = None) -> list[dict[str, float]]:
        edge_list = list(edge_ids)
        if not edge_list:
            return []
        first_edge = self.edges[edge_list[0]]
        current_node = start_node_id or first_edge["start"]
        if current_node not in (first_edge["start"], first_edge["end"]):
            raise RoadGraphError("路径起点不是第一条边的端点。")
        points: list[dict[str, float]] = []
        for edge_id in edge_list:
            edge = self.edges[edge_id]
            if current_node == edge["start"]:
                geometry = edge["geometry"]
                current_node = edge["end"]
            elif current_node == edge["end"] and not edge["one_way"]:
                geometry = list(reversed(edge["geometry"]))
                current_node = edge["start"]
            else:
                raise RoadGraphError(f"路径边 {edge_id} 与前一条边不连通。")
            if points and _point_distance(points[-1], geometry[0]) <= NODE_MATCH_TOLERANCE_M:
                points.extend(copy.deepcopy(geometry[1:]))
            else:
                points.extend(copy.deepcopy(geometry))
        return points

    def path_for_target_distance(
        self,
        start_node_id: str,
        target_distance_m: float,
        *,
        return_to_start: bool = False,
    ) -> dict[str, Any]:
        target = _finite(target_distance_m, "目标距离")
        if target <= 0:
            raise RoadGraphError("目标距离必须为正数。")
        if start_node_id not in self.nodes:
            raise RoadGraphError("规划起点不在当前已核验路网中。")
        distances, previous = self._shortest_paths(start_node_id)
        candidates = []
        for node_id in self.nodes:
            if node_id == start_node_id or node_id not in distances:
                continue
            distance = distances[node_id]
            if not return_to_start:
                candidates.append((abs(distance - target), distance, node_id, None))
                continue
            outbound, _ = self._reconstruct_path(start_node_id, node_id, previous)
            for edge_id, neighbour, _forward in self._neighbours(node_id):
                if neighbour == start_node_id and edge_id not in outbound:
                    total = distance + self.edge_length_m(edge_id)
                    candidates.append((abs(total - target), total, node_id, edge_id))
        if not candidates:
            raise RoadGraphError("当前已核验路网无法规划出目标路线。")
        _error, actual, end_node_id, closing_edge = min(candidates, key=lambda item: (item[0], item[1]))
        edge_ids, node_ids = self._reconstruct_path(start_node_id, end_node_id, previous)
        if closing_edge is not None:
            edge_ids.append(closing_edge)
            node_ids.append(start_node_id)
        return {
            "edge_ids": edge_ids,
            "node_ids": node_ids,
            "distance_m": actual,
            "target_distance_m": target,
            "error_m": actual - target,
            "return_to_start": bool(return_to_start),
        }

    def expand_laps(
        self,
        edge_ids: Iterable[str],
        laps: int,
        *,
        start_node_id: str | None = None,
    ) -> list[str]:
        try:
            count = int(laps)
        except (TypeError, ValueError) as exc:
            raise RoadGraphError("圈数必须是正整数。") from exc
        if count <= 0:
            raise RoadGraphError("圈数必须是正整数。")
        if isinstance(laps, str) and laps.strip() != str(count):
            raise RoadGraphError("圈数必须是正整数。")
        sequence = list(edge_ids)
        if not sequence:
            raise RoadGraphError("闭合基础路线不能为空。")
        first = self.edges[sequence[0]]
        if start_node_id is None:
            start_node_id = first["start"]
        if start_node_id == first["start"]:
            current = first["end"]
        elif start_node_id == first["end"] and not first["one_way"]:
            current = first["start"]
        else:
            raise RoadGraphError("闭合基础路线的起点不是第一条边的可行端点。")
        for edge_id in sequence[1:]:
            edge = self.edges[edge_id]
            if current == edge["start"]:
                current = edge["end"]
            elif current == edge["end"] and not edge["one_way"]:
                current = edge["start"]
            else:
                raise RoadGraphError("基础路线的边顺序不连续。")
        if current != start_node_id:
            raise RoadGraphError("当前基础路线未闭合，不能增加圈数；请补齐回到起点的道路。")
        return sequence * count
