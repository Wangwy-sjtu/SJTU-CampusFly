"""Orchestrate rule lookup, deterministic generation and one upload attempt."""

from __future__ import annotations

import copy
import datetime
import time
from typing import Any, Callable, Mapping

from src.api_client import (
    get_authorization_token_and_rules,
    upload_running_data,
)
from src.data_generator import generate_running_data_payload
from src.route_model import clone_route, route_from_legacy_config, validate_stroke_continuity
from src.utils import (
    SportsUploaderError,
    ValidationError,
    log_output,
    validate_run_parameters,
    validate_route,
)


def _interruptible_wait(
    seconds: float,
    progress_callback: Callable[[int, int, str], None] | None,
    stop_check_cb: Callable[[], bool] | None,
) -> bool:
    if seconds <= 0:
        return True
    end = time.monotonic() + seconds
    while True:
        if stop_check_cb and stop_check_cb():
            return False
        remaining = end - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(0.2, remaining))


def _validate_upload_config(config: Mapping[str, Any]) -> None:
    validate_run_parameters(config.get("RUNNING_SPEED_MPS"), config.get("INTERVAL_SECONDS"))
    if str(config.get("API_MODE", "real")).lower() != "mock":
        if not str(config.get("COOKIE", "")).strip() or not str(config.get("USER_ID", "")).strip():
            raise ValidationError("真实接口模式需要 Cookie 和用户 ID；编辑路线无需登录。")
    route = config.get("ROUTE")
    if route is None:
        route = route_from_legacy_config(config)
    validate_route(route, require_path=True)
    validate_stroke_continuity(route)


def run_sports_upload(
    config: Mapping[str, Any],
    progress_callback: Callable[[int, int, str], None] | None = None,
    log_cb: Callable[[str, str], None] | None = None,
    stop_check_cb: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """Run one upload using a deep-copied route snapshot.

    A response with ``code == 0`` is accepted even when ``data`` is empty, but
    it is reported as pending verification. Repeating it could submit the same
    run twice. Network failures are surfaced without an automatic duplicate.
    """
    try:
        _validate_upload_config(config)
    except (ValidationError, KeyError, TypeError, ValueError) as exc:
        return False, str(exc)

    snapshot = copy.deepcopy(dict(config))
    if snapshot.get("ROUTE") is not None:
        snapshot["ROUTE"] = clone_route(snapshot["ROUTE"])
    mode = str(snapshot.get("API_MODE", "real")).lower()
    log_output("开始处理路线快照。", callback=log_cb)
    if stop_check_cb and stop_check_cb():
        return False, "任务已停止。"

    try:
        if progress_callback:
            progress_callback(10, 100, "获取跑步规则...")
        if mode == "mock":
            auth_token = "mock-token"
            point_rules_data = copy.deepcopy(snapshot.get("MOCK_POINT_RULES", {
                "rules": {"id": snapshot.get("RULE_ID", 6)},
                "points": [],
            }))
            log_output("已使用离线模拟接口，不会访问学校服务器。", "info", log_cb)
        else:
            auth_token, point_rules_data = get_authorization_token_and_rules(
                snapshot, log_cb=log_cb, stop_check_cb=stop_check_cb
            )
    except SportsUploaderError as exc:
        log_output(f"获取规则失败: {exc}", "error", log_cb)
        return False, str(exc)
    except Exception as exc:
        log_output(f"获取规则时发生错误: {exc}", "error", log_cb)
        return False, "获取规则失败。"

    if stop_check_cb and stop_check_cb():
        return False, "任务已停止。"

    rules_meta = (point_rules_data or {}).get("rules", {})
    if rules_meta.get("spmin") is not None or rules_meta.get("spmax") is not None:
        log_output("已读取服务器配速范围；保持用户速度和手绘路线快照不变。", "info", log_cb)

    try:
        if progress_callback:
            progress_callback(40, 100, "生成轨迹...")
        payload, total_distance, total_duration = generate_running_data_payload(
            snapshot,
            required_signpoints=[],
            point_rules_data=point_rules_data,
            log_cb=log_cb,
            stop_check_cb=stop_check_cb,
        )
        track_count = len(payload[0].get("tracks", []))
        log_output(f"生成 {track_count} 笔轨迹，距离 {total_distance:.1f} 米，时长 {total_duration} 秒。", callback=log_cb)
        if payload[0].get("tracks"):
            first_ms = payload[0]["tracks"][0]["points"][0]["locatetime"]
            dt = datetime.datetime.fromtimestamp(first_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
            log_output(f"轨迹起始时间: {dt}", callback=log_cb)
    except (SportsUploaderError, ValidationError) as exc:
        log_output(f"轨迹生成失败: {exc}", "error", log_cb)
        return False, str(exc)
    except Exception as exc:
        log_output(f"轨迹生成时发生错误: {exc}", "error", log_cb)
        return False, "轨迹生成失败。"

    if stop_check_cb and stop_check_cb():
        return False, "任务已停止。"

    if progress_callback:
        progress_callback(70, 100, "准备上传...")
    should_wait = bool(snapshot.get("WAIT_BEFORE_UPLOAD", mode != "mock"))
    if should_wait and snapshot.get("START_TIME_EPOCH_MS") is None:
        log_output(f"等待轨迹时长 {total_duration} 秒后上传。", callback=log_cb)
        if not _interruptible_wait(total_duration, progress_callback, stop_check_cb):
            return False, "任务已停止。"
    elif snapshot.get("START_TIME_EPOCH_MS") is not None:
        log_output("使用历史开始时间，跳过等待。", callback=log_cb)

    try:
        if stop_check_cb and stop_check_cb():
            return False, "任务已停止。"
        if progress_callback:
            progress_callback(90, 100, "提交轨迹...")
        response = upload_running_data(
            snapshot,
            auth_token,
            payload,
            log_cb=log_cb,
            stop_check_cb=stop_check_cb,
        )
    except SportsUploaderError as exc:
        log_output(f"上传失败，未自动重复提交: {exc}", "error", log_cb)
        if progress_callback:
            progress_callback(100, 100, "上传失败")
        return False, str(exc)

    code = response.get("code")
    if code == 0:
        if response.get("data"):
            message = "上传已确认。"
        else:
            message = "服务器返回成功码，记录状态待核实；已避免重复提交。"
        log_output(message, "success" if response.get("data") else "warning", log_cb)
        if progress_callback:
            progress_callback(100, 100, "上传成功")
        return True, message

    message = f"上传被服务器拒绝，响应代码: {code!r}"
    log_output(message, "error", log_cb)
    if progress_callback:
        progress_callback(100, 100, "上传失败")
    return False, message
