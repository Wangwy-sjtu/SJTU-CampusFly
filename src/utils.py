# --- START OF FILE src/utils.py ---
"""Small, dependency-free helpers shared by the route editor and uploader.

The original project mixed validation, UI dialogs and request logging.  Keeping
these helpers free of Qt makes the route and upload algorithms testable in an
offline environment too.
"""

import datetime
import math
import os
import re
import sys
import time
from typing import Any, Callable, Mapping


EARTH_RADIUS_METERS = 6_371_000
TRACK_POINT_DECIMAL_PLACES = 7


def get_base_path() -> str:
    """Return the directory containing the development app or packaged exe."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_user_data_path() -> str:
    """Keep installed user settings outside replaceable application files."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "SJTU-CampusFly")
    return get_base_path()


class SportsUploaderError(Exception):
    """Expected application failure shown to the user."""


class ValidationError(SportsUploaderError, ValueError):
    """A route or run parameter is not usable."""


def is_finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def validate_coordinate(latitude: Any, longitude: Any) -> tuple[float, float]:
    if not is_finite_number(latitude) or not is_finite_number(longitude):
        raise ValidationError("路线坐标必须是有限数字，不能使用 NaN 或无穷值。")
    lat = float(latitude)
    lon = float(longitude)
    if not -90 <= lat <= 90:
        raise ValidationError(f"纬度超出范围: {lat}")
    if not -180 <= lon <= 180:
        raise ValidationError(f"经度超出范围: {lon}")
    return lat, lon


def validate_run_parameters(speed_mps: Any, interval_seconds: Any) -> tuple[float, float]:
    if not is_finite_number(speed_mps) or not is_finite_number(interval_seconds):
        raise ValidationError("速度和采样间隔必须是有限数字，不能使用 NaN 或无穷值。")
    speed = float(speed_mps)
    interval = float(interval_seconds)
    if speed <= 0:
        raise ValidationError("跑步速度必须大于 0 米/秒。")
    if interval <= 0:
        raise ValidationError("采样间隔必须大于 0 秒。")
    if interval > 3600:
        raise ValidationError("采样间隔不能超过 3600 秒。")
    return speed, interval


def validate_route(route: Mapping[str, Any], *, require_path: bool = True) -> None:
    """Validate the canonical route shape without changing it.

    A route keeps strokes separate.  The generator may sample within a stroke,
    but it must never invent an edge between two independent strokes.
    """
    if not isinstance(route, Mapping):
        raise ValidationError("路线数据必须是对象。")
    coordinate_system = route.get("coordinate_system")
    if not isinstance(coordinate_system, str) or not coordinate_system.strip():
        raise ValidationError("路线必须显式记录坐标系。")
    strokes = route.get("strokes")
    if not isinstance(strokes, list):
        raise ValidationError("路线 strokes 必须是列表。")

    point_count = 0
    for stroke_index, stroke in enumerate(strokes):
        if not isinstance(stroke, list):
            raise ValidationError(f"第 {stroke_index + 1} 笔路线格式无效。")
        if not stroke:
            continue
        for point in stroke:
            if not isinstance(point, Mapping):
                raise ValidationError("路线点必须是对象。")
            validate_coordinate(point.get("latitude"), point.get("longitude"))
            point_count += 1

    if require_path and point_count < 2:
        raise ValidationError("路线至少需要两个有效点。")
    if require_path and not any(len(stroke) >= 2 for stroke in strokes):
        raise ValidationError("路线至少需要一笔包含两个点的连续轨迹。")


_SECRET_PATTERNS = (
    re.compile(r"(?i)(cookie\s*[:=]\s*)([^\s;]+(?:\s*;\s*[^\s;]+)*)"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)((?:token|jsessionid|keepalive)\s*[:=]\s*)([^\s,;]+)"),
)


def redact_secrets(value: Any) -> str:
    """Return log-safe text with credentials and bearer tokens replaced."""
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1<redacted>", text)
    return text


def log_output(message: Any, level: str = "info", callback: Callable | None = None) -> None:
    safe_message = redact_secrets(message)
    if callback:
        callback(safe_message, level)
        return
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}][{str(level).upper()}] {safe_message}")


def haversine_distance(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> float:
    """Return the great-circle distance between two latitude/longitude points."""
    lat1, lon1 = validate_coordinate(lat1, lon1)
    lat2, lon2 = validate_coordinate(lat2, lon2)
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))
    return EARTH_RADIUS_METERS * c


def get_current_epoch_ms() -> int:
    return int(time.time() * 1000)
