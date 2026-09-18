"""School API integration boundary and deterministic offline mock."""

from __future__ import annotations

import copy
import json
from typing import Any, Callable, Mapping
from urllib.parse import quote

import requests

from src.route_model import route_endpoints, route_from_legacy_config
from src.utils import SportsUploaderError, log_output, redact_secrets


def make_request(
    method: str,
    url: str,
    headers: Mapping[str, str] | None,
    params: Mapping[str, Any] | None = None,
    data: Any = None,
    log_cb: Callable | None = None,
    stop_check_cb: Callable[[], bool] | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Perform one JSON request; the caller decides whether a retry is safe."""
    if stop_check_cb and stop_check_cb():
        raise SportsUploaderError("任务已停止。")
    response = None
    try:
        client = session or requests
        request_kwargs = {
            "headers": dict(headers or {}),
            "params": params,
            "data": data,
            "timeout": 15,
        }
        if method.upper() == "GET":
            request_kwargs.pop("data")
            response = client.get(url, **request_kwargs)
        elif method.upper() == "POST":
            response = client.post(url, **request_kwargs)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        if stop_check_cb and stop_check_cb():
            raise SportsUploaderError("任务已停止。")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise SportsUploaderError("服务器响应不是 JSON 对象。")
        return payload
    except SportsUploaderError:
        raise
    except requests.exceptions.HTTPError as exc:
        status = response.status_code if response is not None else "unknown"
        # Do not echo response bodies: an error payload may contain uid/token
        # fields that are not named consistently enough to redact safely.
        log_output(f"HTTP 请求失败 ({status})。", "error", log_cb)
        raise SportsUploaderError(f"HTTP Error: {status}") from exc
    except requests.exceptions.Timeout as exc:
        log_output("请求超时。", "error", log_cb)
        raise SportsUploaderError("Timeout Error") from exc
    except requests.exceptions.ConnectionError as exc:
        log_output("无法连接服务器。", "error", log_cb)
        raise SportsUploaderError("Connection Error") from exc
    except requests.exceptions.RequestException as exc:
        log_output(f"请求失败: {redact_secrets(exc)}", "error", log_cb)
        raise SportsUploaderError("Request Error") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        log_output("服务器响应不是有效 JSON。", "error", log_cb)
        raise SportsUploaderError("JSON Decode Error") from exc


def _route_location(config: Mapping[str, Any]) -> str:
    route = config.get("ROUTE")
    if route is None:
        route = route_from_legacy_config(config)
    start, _ = route_endpoints(route)
    if not start:
        raise SportsUploaderError("路线没有起点，无法请求跑步规则。")
    return f"{start['longitude']:.14f},{start['latitude']:.14f}"


def get_authorization_token_and_rules(
    config: Mapping[str, Any],
    log_cb: Callable | None = None,
    stop_check_cb: Callable[[], bool] | None = None,
    session: requests.Session | None = None,
) -> tuple[str, dict[str, Any]]:
    """Get the token and point rules for the explicitly enabled real API path."""
    common_headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=utf-8",
        "User-Agent": "SJTU-RunningMan/2.0",
        "Referer": "https://pe.sjtu.edu.cn/phone/",
        "Cookie": str(config.get("COOKIE", "")),
    }
    uid_url = str(config["UID_URL"])
    uid_response = make_request(
        "GET", uid_url, common_headers, log_cb=log_cb,
        stop_check_cb=stop_check_cb, session=session,
    )
    data = uid_response.get("data") or {}
    token = data.get("uid") if uid_response.get("code") == 0 else None
    if not token:
        raise SportsUploaderError("未能获取服务器授权信息。")

    # Keep the original warm-up request in the integration path, but it is not
    # required to construct the route and its response is deliberately ignored.
    try:
        make_request(
            "GET", str(config["MY_DATA_URL"]), common_headers,
            log_cb=log_cb, stop_check_cb=stop_check_cb, session=session,
        )
    except SportsUploaderError as exc:
        log_output(f"读取历史数据失败，继续请求规则: {exc}", "warning", log_cb)

    location = _route_location(config)
    point_headers = {
        "Accept": "application/json, text/plain, */*",
        "Authorization": str(token),
        "User-Agent": "SJTU-RunningMan/2.0",
        "Referer": f"{config['POINT_RULE_URL']}?location={quote(location, safe='')}",
    }
    rules_response = make_request(
        "GET", str(config["POINT_RULE_URL"]), point_headers,
        params={"location": location}, log_cb=log_cb,
        stop_check_cb=stop_check_cb, session=session,
    )
    rules = rules_response.get("data") or {}
    if not isinstance(rules, dict):
        raise SportsUploaderError("服务器规则响应格式无效。")
    return str(token), rules


def mock_upload_response(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a local response for tests and UI smoke runs."""
    behavior = str(config.get("MOCK_BEHAVIOR", "success")).lower()
    if behavior == "timeout":
        raise SportsUploaderError("模拟上传超时。")
    if behavior == "reject":
        return {"code": 1, "message": "mock rejected"}
    if behavior == "empty":
        return {"code": 0, "data": None}
    response = config.get("MOCK_RESPONSE")
    if isinstance(response, Mapping):
        return copy.deepcopy(dict(response))
    return {"code": 0, "data": {"accepted": True}}


def upload_running_data(
    config: Mapping[str, Any],
    auth_token: str,
    running_data: Any,
    log_cb: Callable | None = None,
    stop_check_cb: Callable[[], bool] | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    if stop_check_cb and stop_check_cb():
        raise SportsUploaderError("任务已停止。")
    if str(config.get("API_MODE", "real")).lower() == "mock":
        response = mock_upload_response(config)
        log_output(f"模拟上传响应: code={response.get('code', 'N/A')}", "info", log_cb)
        return response

    headers = {
        "Authorization": str(auth_token),
        "Content-Type": "application/json; charset=utf-8",
        "Accept-Encoding": "gzip",
        "User-Agent": "SJTU-RunningMan/2.0",
    }
    response = make_request(
        "POST", str(config["UPLOAD_URL"]), headers,
        data=json.dumps(running_data, ensure_ascii=False),
        log_cb=log_cb, stop_check_cb=stop_check_cb, session=session,
    )
    log_output(f"上传响应 code={response.get('code', 'N/A')}", "info", log_cb)
    return response
