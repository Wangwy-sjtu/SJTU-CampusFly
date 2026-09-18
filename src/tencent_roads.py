"""Tencent Maps WebService walking-route geometry.

The Tencent walking endpoint plans one route between two coordinates.  It is
useful for replacing a corresponding annotated segment with provider geometry,
but it does not export a campus road network.  This module therefore returns
route and step geometry as an explicit overlay rather than silently adding
edges to the verified road graph.
"""

from __future__ import annotations

import copy
import math
from typing import Any, Mapping, Sequence

import requests


TENCENT_WALKING_URL = "https://apis.map.qq.com/ws/direction/v1/walking/"
COORDINATE_SYSTEM = "gcj02"


class TencentRoadError(ValueError):
    """A Tencent walking response cannot be used as route geometry."""


def _finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TencentRoadError(f"{label} 必须是数字。") from exc
    if not math.isfinite(number):
        raise TencentRoadError(f"{label} 不能是 NaN 或无穷大。")
    return number


def _point(value: Mapping[str, Any], label: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise TencentRoadError(f"{label} 必须是对象。")
    latitude = _finite(value.get("latitude", value.get("lat")), f"{label}.latitude")
    longitude = _finite(value.get("longitude", value.get("lng", value.get("lon"))), f"{label}.longitude")
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        raise TencentRoadError(f"{label} 坐标超出范围。")
    return {"latitude": latitude, "longitude": longitude}


def _lat_lng_text(value: Mapping[str, Any], label: str) -> str:
    point = _point(value, label)
    return f"{point['latitude']:.8f},{point['longitude']:.8f}"


def decode_tencent_polyline(values: Sequence[Any]) -> list[dict[str, float]]:
    """Decode Tencent's flattened delta polyline.

    The first pair is an absolute latitude/longitude in degrees.  Every
    subsequent pair is a delta in 1e-6 degrees, and the delta is accumulated
    from the previous point.  ``steps.polyline_idx`` indexes this flattened
    array, so callers can map step ranges back to points with ``// 2``.
    """

    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TencentRoadError("路线 polyline 必须是数值列表。")
    if len(values) < 4 or len(values) % 2:
        raise TencentRoadError("路线 polyline 长度无效。")
    numbers = [_finite(value, f"polyline[{index}]") for index, value in enumerate(values)]
    latitude = numbers[0]
    longitude = numbers[1]
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        raise TencentRoadError("路线 polyline 起点坐标超出范围。")
    points = [{"latitude": latitude, "longitude": longitude}]
    for index in range(2, len(numbers), 2):
        latitude += numbers[index] / 1_000_000.0
        longitude += numbers[index + 1] / 1_000_000.0
        if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
            raise TencentRoadError("路线 polyline 解码后坐标超出范围。")
        points.append({"latitude": latitude, "longitude": longitude})
    return points


def _step_index_pair(value: Any, polyline_value_count: int, label: str) -> tuple[int, int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise TencentRoadError(f"{label} 必须是两个扁平 polyline 索引。")
    try:
        start = int(value[0])
        end = int(value[1])
    except (TypeError, ValueError) as exc:
        raise TencentRoadError(f"{label} 索引无效。") from exc
    if start < 0 or end < start or end >= polyline_value_count:
        raise TencentRoadError(f"{label} 超出 polyline 范围。")
    return start, end


def _slice_step_geometry(
    geometry: Sequence[Mapping[str, float]],
    index_pair: tuple[int, int],
) -> list[dict[str, float]]:
    start, end = index_pair
    # polyline_idx points into the flattened [lat, lng, dlat, dlng, ...]
    # values.  Both endpoints can point at either member of a coordinate pair.
    first_point = start // 2
    last_point = end // 2
    return [copy.deepcopy(point) for point in geometry[first_point : last_point + 1]]


def _normalise_steps(
    raw_steps: Any,
    geometry: Sequence[Mapping[str, float]],
    polyline_value_count: int,
) -> list[dict[str, Any]]:
    if raw_steps is None:
        return []
    if not isinstance(raw_steps, list):
        raise TencentRoadError("路线 steps 必须是列表。")
    steps: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, Mapping):
            raise TencentRoadError(f"路线 steps[{index}] 必须是对象。")
        pair = _step_index_pair(raw.get("polyline_idx"), polyline_value_count, f"steps[{index}].polyline_idx")
        road_name = raw.get("road_name")
        if road_name is None:
            road_name = ""
        if not isinstance(road_name, str):
            road_name = str(road_name)
        distance = raw.get("distance", 0)
        duration = raw.get("duration")
        step: dict[str, Any] = {
            "road_name": road_name.strip(),
            "instruction": str(raw.get("instruction") or ""),
            "distance_m": _finite(distance, f"steps[{index}].distance"),
            "polyline_idx": [pair[0], pair[1]],
            "geometry": _slice_step_geometry(geometry, pair),
        }
        if duration is not None:
            step["duration"] = _finite(duration, f"steps[{index}].duration")
        for field in ("dir_desc", "act_desc", "type", "road_class"):
            if raw.get(field) is not None:
                step[field] = str(raw[field])
        steps.append(step)
    return steps


def parse_tencent_walking_response(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and convert a Tencent walking response to overlay JSON."""

    if not isinstance(payload, Mapping):
        raise TencentRoadError("腾讯步行接口响应不是 JSON 对象。")
    status = payload.get("status")
    if status != 0:
        message = str(payload.get("message") or "接口返回错误")
        raise TencentRoadError(f"腾讯步行接口未返回路线：{message[:120]}")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise TencentRoadError("腾讯步行接口缺少 result。")
    raw_routes = result.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        raise TencentRoadError("腾讯步行接口没有可用路线。")

    routes: list[dict[str, Any]] = []
    for route_index, raw_route in enumerate(raw_routes):
        if not isinstance(raw_route, Mapping):
            raise TencentRoadError(f"routes[{route_index}] 必须是对象。")
        raw_polyline = raw_route.get("polyline")
        geometry = decode_tencent_polyline(raw_polyline)
        route: dict[str, Any] = {
            "id": route_index,
            "mode": str(raw_route.get("mode") or "WALKING"),
            "distance_m": _finite(raw_route.get("distance"), f"routes[{route_index}].distance"),
            # Tencent documents route duration in minutes; retain that unit.
            "duration_min": _finite(raw_route.get("duration"), f"routes[{route_index}].duration"),
            "direction": str(raw_route.get("direction") or ""),
            "geometry": geometry,
            "steps": _normalise_steps(raw_route.get("steps", []), geometry, len(raw_polyline)),
        }
        routes.append(route)
    return {
        "provider": "tencent-webservice",
        "coordinate_system": COORDINATE_SYSTEM,
        "endpoint": "/ws/direction/v1/walking/",
        "route_count": len(routes),
        "routes": routes,
    }


def request_tencent_walking_routes(
    key: str,
    from_point: Mapping[str, Any],
    to_point: Mapping[str, Any],
    *,
    session: Any = requests,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Fetch one two-point walking plan without logging or returning the key."""

    if not isinstance(key, str) or not key.strip():
        raise TencentRoadError("腾讯地图 WebService Key 未配置。")
    origin = _point(from_point, "from")
    destination = _point(to_point, "to")
    try:
        response = session.get(
            TENCENT_WALKING_URL,
            params={
                "key": key.strip(),
                "from": _lat_lng_text(origin, "from"),
                "to": _lat_lng_text(destination, "to"),
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except TencentRoadError:
        raise
    except requests.RequestException as exc:
        # Exception strings may contain a fully rendered URL including key.
        raise TencentRoadError("腾讯步行路线请求失败，请检查网络或 Key 权限。") from exc
    except (TypeError, ValueError) as exc:
        raise TencentRoadError("腾讯步行接口返回了无法解析的响应。") from exc
    overlay = parse_tencent_walking_response(payload)
    overlay["from"] = origin
    overlay["to"] = destination
    return overlay


def overlay_route_names(overlay: Mapping[str, Any]) -> list[str]:
    """Return unique non-empty road names for concise UI status text."""

    names: list[str] = []
    for route in overlay.get("routes", []) if isinstance(overlay, Mapping) else []:
        if not isinstance(route, Mapping):
            continue
        for step in route.get("steps", []) if isinstance(route.get("steps", []), list) else []:
            if not isinstance(step, Mapping):
                continue
            name = str(step.get("road_name") or "").strip()
            if name and name not in names:
                names.append(name)
    return names

