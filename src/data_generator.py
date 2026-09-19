"""Generate one upload payload from a user-drawn route snapshot."""

from __future__ import annotations

import math
import uuid
from bisect import bisect_left
from typing import Any, Callable, Mapping, Sequence

from src.route_model import (
    make_point,
    normalise_route,
    route_from_legacy_config,
    rounded_point,
)
from src.utils import (
    TRACK_POINT_DECIMAL_PLACES,
    SportsUploaderError,
    ValidationError,
    get_current_epoch_ms,
    haversine_distance,
    log_output,
    validate_run_parameters,
    validate_route,
)


MAX_SAMPLES_PER_STROKE = 100_000


def _sample_count(distance: float, speed: float, interval: float) -> float:
    step = speed * interval
    count = distance / step if step > 0 else math.inf
    if not math.isfinite(count) or count + 2 > MAX_SAMPLES_PER_STROKE:
        raise ValidationError("采样点过多，请增大采样间隔或缩短路线（每笔最多 100000 点）。")
    return count


def _as_lat_lng(point: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    canonical = rounded_point(point)
    lat = canonical["latitude"]
    lon = canonical["longitude"]
    return {
        "latLng": {"latitude": lat, "longitude": lon},
        "location": f"{lon:.{TRACK_POINT_DECIMAL_PLACES}f},{lat:.{TRACK_POINT_DECIMAL_PLACES}f}",
        "step": 0,
    }


def interpolate_points(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    speed_mps: float,
    interval_seconds: float,
) -> tuple[list[dict[str, Any]], float, int]:
    """Compatibility helper for one straight segment.

    It includes both endpoints and reports duration as the segment travel time;
    no extra sampling period is added to the result.
    """
    speed, interval = validate_run_parameters(speed_mps, interval_seconds)
    start = make_point(start_lat, start_lon)
    end = make_point(end_lat, end_lon)
    distance = haversine_distance(
        start["latitude"], start["longitude"], end["latitude"], end["longitude"]
    )
    duration = distance / speed if distance else 0.0
    if distance == 0:
        return [_as_lat_lng(start)], distance, math.ceil(duration)

    steps = max(1, math.ceil(_sample_count(distance, speed, interval)))
    points: list[dict[str, Any]] = []
    for index in range(steps + 1):
        fraction = index / steps
        points.append(_as_lat_lng({
            "latitude": start["latitude"] + fraction * (end["latitude"] - start["latitude"]),
            "longitude": start["longitude"] + fraction * (end["longitude"] - start["longitude"]),
        }))
    return points, distance, math.ceil(duration)


def _stroke_cumulative_distances(stroke: Sequence[Mapping[str, Any]]) -> tuple[list[float], float]:
    cumulative = [0.0]
    for first, second in zip(stroke, stroke[1:]):
        edge = haversine_distance(
            first["latitude"], first["longitude"],
            second["latitude"], second["longitude"],
        )
        cumulative.append(cumulative[-1] + edge)
    return cumulative, cumulative[-1]


def _point_at_distance(stroke: Sequence[Mapping[str, Any]], cumulative: Sequence[float], distance: float) -> dict[str, float]:
    if distance <= 0:
        return rounded_point(stroke[0])
    if distance >= cumulative[-1]:
        return rounded_point(stroke[-1])
    index = max(0, bisect_left(cumulative, distance) - 1)
    edge = cumulative[index + 1] - cumulative[index]
    fraction = (distance - cumulative[index]) / edge if edge > 0 else 0.0
    first, second = stroke[index], stroke[index + 1]
    return rounded_point({
        "latitude": first["latitude"] + fraction * (second["latitude"] - first["latitude"]),
        "longitude": first["longitude"] + fraction * (second["longitude"] - first["longitude"]),
    })


def _sample_stroke(
    stroke: Sequence[Mapping[str, Any]], speed: float, interval: float,
    stop_check_cb: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], float, list[float]]:
    if not stroke:
        return [], 0.0, []
    cumulative, total_distance = _stroke_cumulative_distances(stroke)
    if total_distance == 0:
        return [_as_lat_lng(stroke[0])], 0.0, [0.0]

    # Include every original vertex, so sampling never cuts a user-drawn corner.
    positions = set(cumulative)
    step_distance = speed * interval
    count = math.floor(_sample_count(total_distance, speed, interval))
    if len(positions) + count > MAX_SAMPLES_PER_STROKE:
        raise ValidationError("采样点过多，请增大采样间隔或缩短路线（每笔最多 100000 点）。")
    positions.update(index * step_distance for index in range(1, count + 1))
    positions.add(total_distance)
    ordered_positions = sorted(positions)
    sampled = []
    for index, distance in enumerate(ordered_positions):
        if index % 256 == 0 and stop_check_cb and stop_check_cb():
            raise SportsUploaderError("任务已停止。")
        sampled.append(_as_lat_lng(_point_at_distance(stroke, cumulative, distance)))
    return sampled, total_distance, ordered_positions


def _with_times(
    sampled: Sequence[dict[str, Any]],
    local_distances: Sequence[float],
    distance_before: float,
    speed: float,
    start_epoch_ms: int,
) -> list[dict[str, Any]]:
    if not sampled:
        return []
    # Coordinates are sampled by cumulative arc length; timestamps use the same
    # arc length so preview totals and upload totals cannot diverge.
    result: list[dict[str, Any]] = []
    for item, local_distance in zip(sampled, local_distances):
        point = dict(item)
        point["locatetime"] = int(round(start_epoch_ms + (distance_before + local_distance) / speed * 1000))
        result.append(point)
    return result


def _track_for_points(points: Sequence[dict[str, Any]], stop_check_cb: Callable[[], bool] | None = None) -> dict[str, Any] | None:
    if not points:
        return None
    if stop_check_cb and stop_check_cb():
        raise SportsUploaderError("任务已停止。")
    distance = 0.0
    for first, second in zip(points, points[1:]):
        p1, p2 = first["latLng"], second["latLng"]
        distance += haversine_distance(
            p1["latitude"], p1["longitude"], p2["latitude"], p2["longitude"]
        )
    start_ms = points[0]["locatetime"]
    end_ms = points[-1]["locatetime"]
    return {
        "counts": len(points),
        "distance": distance,
        "duration": max(0, math.ceil((end_ms - start_ms) / 1000)),
        "points": list(points),
        "status": "normal",
        "trid": str(uuid.uuid4()),
        "tstate": "0",
        "stime": start_ms // 1000,
        "etime": end_ms // 1000,
    }


def split_track_into_segments(
    all_points_with_time: Sequence[dict[str, Any]],
    total_duration_sec: float | None = None,
    min_segment_points: int = 5,
    stop_check_cb: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Compatibility wrapper with deterministic normal status.

    The old implementation randomly labelled chunks as stop/invalid and
    discarded the edge between chunks.  A route is now kept as one normal
    track; multi-stroke routes call this once per stroke in the generator.
    """
    track = _track_for_points(all_points_with_time, stop_check_cb=stop_check_cb)
    return [track] if track else []


def _pace_minutes_per_km(distance_m: float, duration_s: float, rules: Mapping[str, Any]) -> int:
    if distance_m <= 0 or duration_s <= 0:
        return 0
    pace_seconds = duration_s / (distance_m / 1000)
    minimum = rules.get("spmin")
    maximum = rules.get("spmax")
    if isinstance(minimum, (int, float)) and math.isfinite(float(minimum)):
        pace_seconds = max(pace_seconds, float(minimum))
    if isinstance(maximum, (int, float)) and math.isfinite(float(maximum)):
        pace_seconds = min(pace_seconds, float(maximum))
    return max(1, round(pace_seconds / 60))


def generate_running_data_payload(
    config: Mapping[str, Any],
    required_signpoints: Sequence[Mapping[str, Any]] | None,
    point_rules_data: Mapping[str, Any] | None,
    log_cb: Callable | None = None,
    stop_check_cb: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], float, int]:
    """Generate tracks from one immutable route snapshot.

    ``required_signpoints`` is intentionally not inserted into the route: the
    user's drawn order and geometry are the source of truth.  It remains an
    argument for compatibility with the school API response.
    """
    speed, interval = validate_run_parameters(
        config.get("RUNNING_SPEED_MPS"), config.get("INTERVAL_SECONDS")
    )
    route = normalise_route(config.get("ROUTE") if config.get("ROUTE") is not None else route_from_legacy_config(config))
    validate_route(route, require_path=True)
    if not required_signpoints and point_rules_data:
        log_output("未插入服务器打卡点，上传路线保持用户手绘顺序。", "info", log_cb)

    start_epoch_ms = config.get("START_TIME_EPOCH_MS")
    if start_epoch_ms is None:
        start_epoch_ms = get_current_epoch_ms()
    if not isinstance(start_epoch_ms, (int, float)) or not math.isfinite(float(start_epoch_ms)):
        raise ValidationError("开始时间必须是有限的毫秒时间戳。")
    start_epoch_ms = int(start_epoch_ms)

    tracks: list[dict[str, Any]] = []
    distance_before = 0.0
    for stroke in route["strokes"]:
        if stop_check_cb and stop_check_cb():
            raise SportsUploaderError("任务已停止。")
        sampled, stroke_distance, local_distances = _sample_stroke(stroke, speed, interval, stop_check_cb)
        timed = _with_times(sampled, local_distances, distance_before, speed, start_epoch_ms)
        track = _track_for_points(timed, stop_check_cb=stop_check_cb)
        if track:
            tracks.append(track)
        distance_before += stroke_distance

    total_distance = distance_before
    total_duration = math.ceil(total_distance / speed) if total_distance > 0 else 0
    rules = dict((point_rules_data or {}).get("rules", {}))
    # The live rule endpoint omits id. The original client used 6 then mapped
    # that fallback to 9. Keep explicit server IDs, but match its absent-ID path.
    run_id = rules.get("id")
    if run_id is None:
        run_id = config.get("RULE_ID", 9)
        log_output(f"学校规则未含编号，采用兼容编号 {run_id}。", "info", log_cb)
    sp_avg = _pace_minutes_per_km(total_distance, total_distance / speed if speed else 0, rules)
    payload = [{
        "fravg": 0,
        "id": run_id,
        "sid": str(uuid.uuid4()),
        "signpoints": [],
        "spavg": sp_avg,
        "state": "0",
        "tracks": tracks,
        "userId": config.get("USER_ID", ""),
    }]
    return payload, total_distance, total_duration
